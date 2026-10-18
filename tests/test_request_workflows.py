"""Public request workflows isolated by Django TestCase transactions."""

# Эти сценарии запускаются командой:
# `.venv/bin/python manage.py test tests.test_request_workflows --settings=fishsite.settings_test --noinput`.
# Django TestCase создаёт тестовую БД `test_fishsite` в отдельном изолированном
# PostgreSQL-кластере и оборачивает тестовые изменения в откатываемые транзакции;
# Django Client выполняет GET/POST к публичным URL и проверяет HTTP-ответы,
# сессии и контекст шаблонов. Тесты создают собственные записи каталога, заявок,
# редакций согласия и outbox. Формы отправляют связанные с сессией подписанные
# токены; фиктивные источники данных и OperationalError подставляются через mock.
# SMTP и внешние сервисы сценарии не вызывают.

from decimal import Decimal
import uuid
from unittest.mock import patch

from django.core import signing
from django.db import OperationalError
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from catalog.models import Category, Product
from catalog.pricing import change_product_price
from content.models import ConsentRevision
from inquiries.models import Inquiry
from integrations.models import NotificationOutbox
from orders.cart import CART_SESSION_KEY
from orders.models import Order


class PrimaryRequestWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.marker = uuid.uuid4().hex
        cls.category = Category.objects.create(
            name=f"Primary request category {cls.marker}",
            slug=f"primary-request-{cls.marker}",
        )
        cls.product = Product.objects.create(
            name=f"Primary request fish {cls.marker}",
            slug=f"primary-request-fish-{cls.marker}",
            category=cls.category,
            price_per_kg=Decimal("12.34"),
            minimum_weight=Decimal("0.100"),
            weight_step=Decimal("0.100"),
            is_published=True,
        )

    def add(self, client, product=None, weight="1"):
        response = client.post(
            reverse("orders:cart"),
            {
                "action": "add",
                "product_id": str((product or self.product).pk),
                "weight": weight,
            },
        )
        self.assertEqual(response.status_code, 302)
        return response

    def submission(self, client, url=None):
        response = client.get(url or reverse("orders:cart"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("submission", response.context)
        return response.context["submission"]

    @staticmethod
    def retail_data(submission, **overrides):
        return {
            "action": "submit",
            **submission,
            "name": "Primary Retail Contact",
            "phone": "+7 (921) 555-01-02",
            "email": "retail@example.test",
            "pickup_method": "delivery",
            "address": "Primary test delivery address",
            "comment": "Primary test comment",
            "consent": "yes",
            **overrides,
        }

    @staticmethod
    def wholesale_data(submission, **overrides):
        return {
            **submission,
            "contact_name": "Primary Wholesale Contact",
            "phone": "+7 (921) 555-03-04",
            "consent": "yes",
            **overrides,
        }

    def assert_no_retail_request(self, submission):
        self.assertFalse(Order.objects.filter(submission_key=submission["submission_key"]).exists())
        self.assertFalse(
            NotificationOutbox.objects.filter(
                order__submission_key=submission["submission_key"]
            ).exists()
        )

    @staticmethod
    def revision_id(submission):
        return submission["consent_url"].split("/revisions/", 1)[1].strip("/")

    # Два добавления без JavaScript складываются в 5 кг; POST с подменёнными
    # суммами создаёт доставку по серверной цене и сохраняет снимок товара,
    # согласие, одну outbox-запись и очищает корзину. Страница успеха показывает
    # принимает общим сообщением без внутренних данных и имеет noindex/no-store.
    def test_accumulated_cart_no_js_delivery_post_saves_decimal_snapshots_and_private_success(self):
        self.add(self.client, weight="2")
        self.add(self.client, weight="3")
        self.assertEqual(
            self.client.session[CART_SESSION_KEY],
            {str(self.product.pk): "5"},
        )

        cart = self.client.get(reverse("orders:cart"))
        self.assertContains(cart, 'id="retail-request-form"')
        self.assertContains(cart, 'name="submission_token"')
        self.assertContains(cart, 'name="consent"')
        self.assertNotContains(cart, "Приём заявок пока не подключён")
        submission = cart.context["submission"]

        response = self.client.post(
            reverse("orders:cart"),
            self.retail_data(
                submission,
                price_per_kg="0.01",
                line_total="0.00",
                total="0.00",
            ),
        )

        self.assertEqual(response.status_code, 302)
        order = Order.objects.get(submission_key=submission["submission_key"])
        self.assertEqual(response.url, reverse("orders:success", args=(order.success_token,)))
        self.assertEqual(order.fulfillment_method, Order.FulfillmentMethod.DELIVERY)
        self.assertEqual(order.delivery_address, "Primary test delivery address")
        self.assertTrue(order.privacy_consent)
        self.assertIsNotNone(order.privacy_consent_at)
        self.assertEqual(order.privacy_consent_revision.revision_id, self.revision_id(submission))
        items = list(order.items.all())
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].product_name_snapshot, self.product.name)
        self.assertEqual(items[0].price_per_kg_snapshot, Decimal("12.34"))
        self.assertEqual(items[0].desired_weight, Decimal("5"))
        self.assertEqual(items[0].line_total, Decimal("61.70"))
        self.assertEqual(NotificationOutbox.objects.filter(order=order).count(), 1)
        self.assertEqual(self.client.session.get(CART_SESSION_KEY, {}), {})

        change_product_price(
            product_id=self.product.pk,
            price=Decimal("99.99"),
            source=Product.PriceSource.MANUAL,
        )
        self.product.refresh_from_db()
        self.product.name = "Changed catalog name"
        self.product.is_published = False
        self.product.save()
        items[0].refresh_from_db()
        self.assertEqual(items[0].product_name_snapshot, f"Primary request fish {self.marker}")
        self.assertEqual(items[0].price_per_kg_snapshot, Decimal("12.34"))
        self.assertEqual(items[0].desired_weight, Decimal("5"))

        success = self.client.get(response.url)
        self.assertEqual(success.status_code, 200)
        self.assertContains(success, "Заявка получена")
        self.assertContains(success, "Менеджер свяжется с вами для подтверждения наличия, количества и стоимости товаров.")
        self.assertNotContains(success, order.order_number)
        self.assertNotIn("order", success.context)
        self.assertNotIn("order_number", success.context)
        for private_value in (
            "Primary Retail Contact",
            "+7 (921) 555-01-02",
            "retail@example.test",
            "Primary test delivery address",
            "Primary test comment",
        ):
            self.assertNotContains(success, private_value)
        self.assertEqual(success["X-Robots-Tag"], "noindex, nofollow")
        self.assertIn("no-store", success["Cache-Control"])

    # GET checkout без отдельной формы перенаправляет к форме корзины; отправка
    # самовывоза без адреса успешно создаёт заказ с пустым адресом.
    def test_pickup_needs_no_address_and_checkout_get_returns_to_cart_form(self):
        self.add(self.client)
        checkout = self.client.get(reverse("orders:checkout"))
        self.assertEqual(checkout.status_code, 302)
        self.assertEqual(checkout.url, reverse("orders:cart") + "#request")

        submission = self.submission(self.client)
        response = self.client.post(
            reverse("orders:checkout"),
            self.retail_data(
                submission,
                pickup_method="pickup",
                address="",
                email="",
                comment="",
            ),
        )
        self.assertEqual(response.status_code, 302)
        order = Order.objects.get(submission_key=submission["submission_key"])
        self.assertEqual(order.fulfillment_method, Order.FulfillmentMethod.PICKUP)
        self.assertEqual(order.delivery_address, "")

    # Оптовая форма не содержит поля потребности; минимальные контакт и согласие
    # создают Inquiry с пустыми необязательными полями и одной outbox-записью.
    # Страница успеха показывает номер, но не контактные данные.
    def test_wholesale_minimal_contact_and_consent_are_saved_without_need(self):
        page = self.client.get(reverse("inquiries:wholesale"))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'name="need"')
        self.assertNotContains(page, "Опишите потребность")
        submission = page.context["submission"]

        response = self.client.post(
            reverse("inquiries:wholesale"),
            self.wholesale_data(submission),
        )

        self.assertEqual(response.status_code, 302)
        inquiry = Inquiry.objects.get(submission_key=submission["submission_key"])
        self.assertEqual(inquiry.contact_name, "Primary Wholesale Contact")
        self.assertEqual(inquiry.phone, "+7 (921) 555-03-04")
        self.assertEqual(inquiry.company, "")
        self.assertEqual(inquiry.email, "")
        self.assertEqual(inquiry.need, "")
        self.assertTrue(inquiry.privacy_consent)
        self.assertIsNotNone(inquiry.privacy_consent_at)
        self.assertEqual(inquiry.privacy_consent_revision.revision_id, self.revision_id(submission))
        self.assertEqual(NotificationOutbox.objects.filter(inquiry=inquiry).count(), 1)

        success = self.client.get(response.url)
        self.assertEqual(success.status_code, 200)
        self.assertContains(success, "Заявка принята. Менеджер свяжется с вами")
        self.assertNotContains(success, inquiry.inquiry_number)
        self.assertNotIn("inquiry", success.context)
        self.assertNotIn("inquiry_number", success.context)
        self.assertNotContains(success, "Primary Wholesale Contact")
        self.assertNotContains(success, "+7 (921) 555-03-04")

    # Розничный POST после смены исходного текста согласия сохраняет редакцию,
    # связанную со старым подписанным токеном; вручную присланный ID её не меняет.
    # Архивная страница показывает старый текст, а новая форма получает новую
    # редакцию.
    def test_retail_post_keeps_the_signed_revision_when_the_source_changes(self):
        self.add(self.client)
        old_text = "# Согласие\n\nАрхивный текст редакции до изменения.\n"
        new_text = "# Согласие\n\nНовый текст после изменения Markdown.\n"
        with patch("content.consent_revisions.read_legal_document_source", return_value=old_text):
            page = self.client.get(reverse("orders:cart"))
        old_submission = page.context["submission"]
        old_revision_id = self.revision_id(old_submission)
        old_revision = ConsentRevision.objects.get(revision_id=old_revision_id)
        self.assertContains(page, f'href="{old_submission["consent_url"]}"')

        with patch("content.consent_revisions.read_legal_document_source", return_value=new_text):
            response = self.client.post(
                reverse("orders:cart"),
                self.retail_data(
                    old_submission,
                    pickup_method="pickup",
                    address="",
                    # A plain POST value has no authority; only the signed claim is used.
                    consent_revision_id=f"sha256:{'0' * 64}",
                ),
            )
            archived_page = self.client.get(old_submission["consent_url"])
            new_page = self.client.get(reverse("orders:cart"))

        self.assertEqual(response.status_code, 302)
        order = Order.objects.get(submission_key=old_submission["submission_key"])
        self.assertEqual(order.privacy_consent_revision_id, old_revision.pk)
        old_revision.refresh_from_db()
        self.assertEqual(old_revision.text, old_text)
        self.assertEqual(archived_page.status_code, 200)
        self.assertContains(archived_page, "Архивный текст редакции до изменения.")
        self.assertNotContains(archived_page, "Новый текст после изменения Markdown.")
        self.assertNotEqual(self.revision_id(new_page.context["submission"]), old_revision_id)

    # Два подписанных оптовых токена проверяют отсутствие ID редакции и
    # неизвестный ID: каждый POST получает 400, не создаёт Inquiry и возвращает
    # новую форму с новым ключом и незаполненным согласием.
    def test_missing_or_unknown_revision_in_signed_token_requires_new_consent(self):
        page = self.client.get(reverse("inquiries:wholesale"))
        submission = page.context["submission"]
        salt = "fishsite.public-form.wholesale"
        payload = signing.loads(submission["submission_token"], salt=salt)

        legacy_payload = dict(payload)
        legacy_payload.pop("consent_revision_id")
        legacy_submission = dict(
            submission,
            submission_token=signing.dumps(legacy_payload, salt=salt, compress=True),
        )
        legacy_response = self.client.post(
            reverse("inquiries:wholesale"),
            self.wholesale_data(legacy_submission),
        )
        self.assertEqual(legacy_response.status_code, 400)
        self.assertFalse(Inquiry.objects.filter(submission_key=submission["submission_key"]).exists())
        self.assertNotEqual(legacy_response.context["submission"]["submission_key"], submission["submission_key"])
        consent_control = legacy_response.content.decode().split('id="id_consent"', 1)[1].split(">", 1)[0]
        self.assertNotIn("checked", consent_control)

        unknown_payload = dict(payload, consent_revision_id=f"sha256:{'0' * 64}")
        unknown_submission = dict(
            submission,
            submission_token=signing.dumps(unknown_payload, salt=salt, compress=True),
        )
        unknown_response = self.client.post(
            reverse("inquiries:wholesale"),
            self.wholesale_data(unknown_submission),
        )
        self.assertEqual(unknown_response.status_code, 400)
        self.assertFalse(Inquiry.objects.filter(submission_key=submission["submission_key"]).exists())
        consent_control = unknown_response.content.decode().split('id="id_consent"', 1)[1].split(">", 1)[0]
        self.assertNotIn("checked", consent_control)

    # Пять subTest проверяют по отдельности пустые имя, телефон, согласие,
    # адрес доставки и некорректный email: каждый ответ 400, без заказа/outbox,
    # при этом позиция остаётся в сессионной корзине.
    def test_required_retail_contacts_consent_and_delivery_address_are_validated(self):
        self.add(self.client)
        invalid_overrides = (
            {"name": ""},
            {"phone": ""},
            {"consent": ""},
            {"address": ""},
            {"email": "not-an-email"},
        )
        for overrides in invalid_overrides:
            with self.subTest(overrides=overrides):
                submission = self.submission(self.client)
                response = self.client.post(
                    reverse("orders:cart"),
                    self.retail_data(submission, **overrides),
                )
                self.assertEqual(response.status_code, 400)
                self.assert_no_retail_request(submission)
                self.assertEqual(
                    self.client.session[CART_SESSION_KEY],
                    {str(self.product.pk): "1"},
                )

    # Четыре subTest проверяют пустые контактное лицо, телефон и согласие, а
    # также некорректный email: каждый ответ 400 и Inquiry не создаётся.
    def test_required_wholesale_contacts_consent_and_email_are_validated(self):
        invalid_overrides = (
            {"contact_name": ""},
            {"phone": ""},
            {"consent": ""},
            {"email": "not-an-email"},
        )
        for overrides in invalid_overrides:
            with self.subTest(overrides=overrides):
                submission = self.submission(self.client, reverse("inquiries:wholesale"))
                response = self.client.post(
                    reverse("inquiries:wholesale"),
                    self.wholesale_data(submission, **overrides),
                )
                self.assertEqual(response.status_code, 400)
                self.assertFalse(
                    Inquiry.objects.filter(submission_key=submission["submission_key"]).exists()
                )

    # Сессионная корзина со старым дробным весом 1.5 кг отклоняется с 400 без
    # округления и без создания заказа/outbox; исходное значение сохраняется.
    def test_fractional_legacy_cart_cannot_be_submitted_or_rounded(self):
        session = self.client.session
        session[CART_SESSION_KEY] = {str(self.product.pk): "1.5"}
        session.save()
        submission = self.submission(self.client)

        response = self.client.post(
            reverse("orders:cart"),
            self.retail_data(submission, pickup_method="pickup", address=""),
        )

        self.assertEqual(response.status_code, 400)
        self.assert_no_retail_request(submission)
        self.assertEqual(self.client.session[CART_SESSION_KEY], {str(self.product.pk): "1.5"})

    # Проверяются три отказа: пустая корзина, изменённая подпись токена и
    # токен одной сессии, отправленный из другой; все ответы 400 и заказ не создан.
    def test_empty_tampered_and_cross_session_retail_tokens_do_not_create_orders(self):
        empty_submission = self.submission(self.client)
        response = self.client.post(
            reverse("orders:cart"),
            self.retail_data(empty_submission, pickup_method="pickup", address=""),
        )
        self.assertEqual(response.status_code, 400)
        self.assert_no_retail_request(empty_submission)

        self.add(self.client)
        original = self.submission(self.client)
        tampered = dict(original, submission_token=original["submission_token"] + "x")
        response = self.client.post(
            reverse("orders:cart"),
            self.retail_data(tampered, pickup_method="pickup", address=""),
        )
        self.assertEqual(response.status_code, 400)
        self.assert_no_retail_request(original)

        other_session = Client()
        other_session.get(reverse("orders:cart"))
        response = other_session.post(
            reverse("orders:cart"),
            self.retail_data(original, pickup_method="pickup", address=""),
        )
        self.assertEqual(response.status_code, 400)
        self.assert_no_retail_request(original)

    # Устаревший снимок после добавления товара и смены цены получает 409 и
    # новый ключ; первая отправка со свежим токеном без подтверждения получает
    # 400, с явным подтверждением создаёт заказ по текущим составу и ценам,
    # сохраняя редакцию согласия исходной формы.
    def test_catalog_price_or_composition_changes_require_new_explicit_confirmation(self):
        second = Product.objects.create(
            name=f"Second request fish {self.marker}",
            slug=f"second-primary-request-fish-{self.marker}",
            category=self.category,
            price_per_kg=Decimal("7.89"),
            minimum_weight=Decimal("0.100"),
            weight_step=Decimal("0.100"),
            is_published=True,
        )
        self.add(self.client)
        stale_tab = self.submission(self.client)
        self.add(self.client, second, "2")
        Product.objects.filter(pk=self.product.pk).update(price_per_kg=Decimal("13.00"))

        response = self.client.post(
            reverse("orders:cart"),
            self.retail_data(
                stale_tab,
                pickup_method="pickup",
                address="",
                confirm_updated_cart="yes",
            ),
        )

        self.assertEqual(response.status_code, 409)
        self.assert_no_retail_request(stale_tab)
        fresh = response.context["submission"]
        self.assertNotEqual(fresh["submission_key"], stale_tab["submission_key"])
        self.assertEqual(fresh["consent_url"], stale_tab["consent_url"])
        self.assertContains(response, "изменились", status_code=409)

        unconfirmed = self.client.post(
            reverse("orders:cart"),
            self.retail_data(fresh, pickup_method="pickup", address=""),
        )
        self.assertEqual(unconfirmed.status_code, 400)
        self.assert_no_retail_request(fresh)

        confirmed = self.client.post(
            reverse("orders:cart"),
            self.retail_data(
                fresh,
                pickup_method="pickup",
                address="",
                confirm_updated_cart="yes",
            ),
        )
        self.assertEqual(confirmed.status_code, 302)
        order = Order.objects.get(submission_key=fresh["submission_key"])
        self.assertEqual(order.privacy_consent_revision.revision_id, self.revision_id(stale_tab))
        self.assertEqual(
            list(order.items.values_list("product_name_snapshot", "price_per_kg_snapshot", "desired_weight")),
            [
                (self.product.name, Decimal("13.00"), Decimal("1")),
                (second.name, Decimal("7.89"), Decimal("2")),
            ],
        )

    # Если товар корзины снят с публикации, отправка устаревшего снимка даёт
    # 409, не создаёт заказ, не показывает товар в форме и удаляет его из корзины.
    def test_unpublished_cart_product_is_removed_and_never_submitted(self):
        self.add(self.client)
        stale = self.submission(self.client)
        Product.objects.filter(pk=self.product.pk).update(is_published=False)

        response = self.client.post(
            reverse("orders:cart"),
            self.retail_data(stale, confirm_updated_cart="yes"),
        )

        self.assertEqual(response.status_code, 409)
        self.assert_no_retail_request(stale)
        self.assertNotContains(response, self.product.name, status_code=409)
        self.assertEqual(self.client.session.get(CART_SESSION_KEY, {}), {})

    # Случайный неизвестный UUID-токен страницы успеха для заказа и оптовой
    # заявки не раскрывает запись: оба URL отвечают 404.
    def test_success_uuid_is_unguessable_and_unknown_tokens_return_404(self):
        unknown = uuid.uuid4()
        self.assertEqual(
            self.client.get(reverse("orders:success", args=(unknown,))).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(reverse("inquiries:success", args=(unknown,))).status_code,
            404,
        )

    # Honeypot в первом POST отклоняется без заявки; корректный POST принимается,
    # повтор того же ключа возвращает прежний success даже после очистки корзины
    # и не дублирует outbox. Новый ключ после двух попыток получает 429.
    @override_settings(PUBLIC_FORM_RATE_LIMIT=2)
    def test_honeypot_is_rejected_and_same_key_retry_bypasses_rate_limit_after_success(self):
        self.client.defaults["REMOTE_ADDR"] = f"primary-retry-{uuid.uuid4()}"
        self.add(self.client)
        submission = self.submission(self.client)
        data = self.retail_data(submission, pickup_method="pickup", address="")

        trapped = self.client.post(reverse("orders:cart"), data | {"website": "filled-in"})
        self.assertEqual(trapped.status_code, 400)
        self.assert_no_retail_request(submission)
        self.assertEqual(self.client.session[CART_SESSION_KEY], {str(self.product.pk): "1"})

        accepted = self.client.post(reverse("orders:cart"), data)
        self.assertEqual(accepted.status_code, 302)
        self.add(self.client, weight="4")
        duplicate = self.client.post(reverse("orders:cart"), data)
        self.assertEqual(duplicate.status_code, 302)
        self.assertEqual(duplicate.url, accepted.url)
        self.assertEqual(
            self.client.session[CART_SESSION_KEY],
            {str(self.product.pk): "4"},
        )
        order = Order.objects.get(submission_key=submission["submission_key"])
        self.assertEqual(Order.objects.filter(submission_key=submission["submission_key"]).count(), 1)
        self.assertEqual(order.privacy_consent_revision.revision_id, self.revision_id(submission))
        self.assertEqual(NotificationOutbox.objects.filter(order=order).count(), 1)

        next_submission = self.submission(self.client)
        limited = self.client.post(
            reverse("orders:cart"),
            self.retail_data(next_submission, pickup_method="pickup", address=""),
        )
        self.assertEqual(limited.status_code, 429)
        self.assert_no_retail_request(next_submission)

    # Для розничной и оптовой форм Django Client с CSRF-проверкой отклоняет POST
    # без токена с 403 и принимает повтор с cookie-токеном с перенаправлением.
    def test_csrf_is_required_for_retail_and_wholesale_submissions(self):
        retail = Client(enforce_csrf_checks=True)
        session = retail.session
        session[CART_SESSION_KEY] = {str(self.product.pk): "1"}
        session.save()
        cart_page = retail.get(reverse("orders:cart"))
        retail_submission = cart_page.context["submission"]
        retail_payload = self.retail_data(
            retail_submission,
            pickup_method="pickup",
            address="",
        )
        denied = retail.post(reverse("orders:cart"), retail_payload)
        self.assertEqual(denied.status_code, 403)
        allowed = retail.post(
            reverse("orders:cart"),
            retail_payload | {"csrfmiddlewaretoken": cart_page.cookies["csrftoken"].value},
        )
        self.assertEqual(allowed.status_code, 302)

        wholesale = Client(enforce_csrf_checks=True)
        wholesale_page = wholesale.get(reverse("inquiries:wholesale"))
        wholesale_payload = self.wholesale_data(wholesale_page.context["submission"])
        denied = wholesale.post(reverse("inquiries:wholesale"), wholesale_payload)
        self.assertEqual(denied.status_code, 403)
        allowed = wholesale.post(
            reverse("inquiries:wholesale"),
            wholesale_payload | {"csrfmiddlewaretoken": wholesale_page.cookies["csrftoken"].value},
        )
        self.assertEqual(allowed.status_code, 302)

    # Ошибки БД при сохранении заявки и чтении корзины/навигации проверяются
    # отдельно для розницы и опта: обработчик не выбрасывает OperationalError,
    # возвращает 503 без текста ошибки и не создаёт заявку.
    def test_public_database_error_fallback_does_not_raise_or_render_raw_error(self):
        self.add(self.client)
        retail_submission = self.submission(self.client)
        retail_payload = self.retail_data(retail_submission)
        with (
            patch(
                "orders.request_flow.handlers.submit_retail_order",
                side_effect=OperationalError("private primary database detail"),
            ),
            patch(
                "orders.cart.presentation.get_cart_state",
                side_effect=OperationalError("private cart read failure"),
            ),
        ):
            try:
                retail_response = self.client.post(reverse("orders:cart"), retail_payload)
            except OperationalError as error:
                self.fail(f"Retail database-error fallback raised {error!r} instead of returning a response")
        self.assertEqual(retail_response.status_code, 503)
        self.assertNotContains(retail_response, "private primary database detail", status_code=503)
        self.assertNotContains(retail_response, "private cart read failure", status_code=503)
        self.assert_no_retail_request(retail_submission)

        wholesale_submission = self.submission(self.client, reverse("inquiries:wholesale"))
        with (
            patch(
                "inquiries.request_flow.handlers.submit_wholesale_inquiry",
                side_effect=OperationalError("private wholesale database detail"),
            ),
            patch(
                "catalog.models.Category.objects.filter",
                side_effect=OperationalError("private navigation query failure"),
            ),
        ):
            try:
                wholesale_response = self.client.post(
                    reverse("inquiries:wholesale"),
                    self.wholesale_data(wholesale_submission),
                )
            except OperationalError as error:
                self.fail(f"Wholesale database-error fallback raised {error!r} instead of returning a response")
        self.assertEqual(wholesale_response.status_code, 503)
        self.assertNotContains(wholesale_response, "private wholesale database detail", status_code=503)
        self.assertNotContains(wholesale_response, "private navigation query failure", status_code=503)
        self.assertFalse(
            Inquiry.objects.filter(submission_key=wholesale_submission["submission_key"]).exists()
        )

    # Ошибка БД при чтении nonce сессии проверяется для отправки розничной и
    # оптовой форм: оба запроса безопасно отвечают 503 без деталей исключения.
    def test_database_error_while_reading_session_nonce_returns_safe_503(self):
        self.add(self.client)
        retail_submission = self.submission(self.client)
        with patch(
            "integrations.public_forms.get_session_nonce",
            side_effect=OperationalError("private session database detail"),
        ):
            retail_response = self.client.post(
                reverse("orders:cart"),
                self.retail_data(retail_submission),
            )
        self.assertEqual(retail_response.status_code, 503)
        self.assertNotContains(retail_response, "private session database detail", status_code=503)

        wholesale_page = self.client.get(reverse("inquiries:wholesale"))
        with patch(
            "integrations.public_forms.get_session_nonce",
            side_effect=OperationalError("private session database detail"),
        ):
            wholesale_response = self.client.post(
                reverse("inquiries:wholesale"),
                self.wholesale_data(wholesale_page.context["submission"]),
            )
        self.assertEqual(wholesale_response.status_code, 503)
        self.assertNotContains(wholesale_response, "private session database detail", status_code=503)
