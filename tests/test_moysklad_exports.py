"""PHASE B2 acceptance scenarios for transactional MoySklad order export.

Run with:
    .venv/bin/python manage.py test tests.test_moysklad_exports --settings=fishsite.settings_test --noinput

The API is represented only by a deterministic fake client. These tests use
validated local orders with retained consent revisions and synthetic UUIDs.
"""

import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from catalog.models import Category, Product
from content.consent_revisions import get_current_consent_revision
from integrations.models import NotificationOutbox
from orders.cart import CART_SESSION_KEY
from orders.models import Order, OrderItem
from tests.moysklad_helpers import (
    ACCOUNT_ID,
    AGENT_ID,
    CURRENCY_ID,
    ENVIRONMENT,
    INITIAL_STATE_ID,
    MOYSKLAD_TEST_CONFIG,
    ORGANIZATION_ID,
    NAMESPACE,
    UNIT_KG_ID,
    UNIT_PIECE_ID,
    product_payload,
)


def customer_order_response(payload, external_id, sync_id):
    """Return a complete synthetic customerorder matching the sent snapshot."""
    return {
        "id": str(external_id),
        "syncId": str(sync_id),
        "accountId": str(ACCOUNT_ID),
        "rate": payload["rate"],
        "sum": sum(position["price"] * position["quantity"] for position in payload["positions"]),
        "vatEnabled": False,
        "positions": [
            {
                "assortment": position["assortment"],
                "price": position["price"],
                "quantity": position["quantity"],
                "discount": position["discount"],
                "reserve": position["reserve"],
                "vat": position["vat"],
                "vatEnabled": position["vatEnabled"],
            }
            for position in payload["positions"]
        ],
    }


@override_settings(MOYSKLAD=MOYSKLAD_TEST_CONFIG)
class MoySkladOrderExportTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="Экспорт", slug=f"export-{uuid.uuid4().hex}")
        self.product = Product.objects.create(
            name="Форель сайта",
            slug=f"trout-{uuid.uuid4().hex}",
            category=self.category,
            price_per_kg=Decimal("1250.50"),
            is_published=True,
        )
        self.external_id = uuid.UUID("20000000-0000-4000-8000-000000000001")
        self.revision = get_current_consent_revision()
        self.owner = get_user_model().objects.create_superuser(
            username=f"export-owner-{uuid.uuid4().hex}",
            email=f"export-owner-{uuid.uuid4().hex}@example.test",
            password="test-password",
        )
        from integrations.models import MoySkladSyncState

        self.sync_state, _ = MoySkladSyncState.objects.get_or_create(
            account_id=ACCOUNT_ID,
            environment=ENVIRONMENT,
            defaults={"namespace": NAMESPACE},
        )
        self.sync_state.export_started_at = timezone.now() - timedelta(days=1)
        self.sync_state.export_consent_revision = self.revision
        self.sync_state.save(update_fields=("export_started_at", "export_consent_revision"))

    def map_product(self, *, external_id=None):
        """Create the accepted account/product mapping through catalog services."""
        from integrations.moysklad.catalog_sync import link_product, upsert_product
        from integrations.moysklad.price_sync import process_price_task
        from integrations.moysklad.queue import claim_task

        external_id = external_id or self.external_id
        upsert_product(product_payload(external_id, price="1250.50"))
        link_product(product_id=self.product.pk, external_product_id=external_id, actor=self.owner)
        process_price_task(claim_task(), client=Mock(get=Mock(return_value=product_payload(external_id, price="1250.50"))))

    def submit_retail(self, *, comment="Комментарий заявки", address="Москва"):
        """Submit through the signed public cart workflow, returning response."""
        self.client.post(
            reverse("orders:cart"),
            {"action": "add", "product_id": str(self.product.pk), "weight": "3"},
        )
        cart = self.client.get(reverse("orders:cart"))
        submission = cart.context["submission"]
        return self.client.post(
            reverse("orders:cart"),
            {
                "action": "submit",
                **submission,
                "name": "Анна Тест",
                "phone": "+7 900 000-00-00",
                "email": "anna@example.test",
                "pickup_method": "delivery",
                "address": address,
                "comment": comment,
                "consent": "yes",
            },
        )

    def accepted_order(
        self, *, name="Анна Тест", comment="Комментарий заявки", address="Москва",
        phone="+7 900 000-00-00", email="anna@example.test", with_external_snapshots=True,
    ):
        """Create valid, schema-checked order data and immutable item snapshots."""
        order = Order.objects.create(
            name=name,
            phone=phone,
            email=email,
            fulfillment_method=Order.FulfillmentMethod.DELIVERY,
            delivery_address=address,
            comment=comment,
            privacy_consent=True,
            privacy_consent_at=timezone.now(),
            privacy_consent_revision=self.revision,
        )
        snapshots = {}
        if with_external_snapshots:
            snapshots = {
                "external_account_id_snapshot": ACCOUNT_ID,
                "external_entity_type_snapshot": "product",
                "external_id_snapshot": self.external_id,
                "unit_id_snapshot": UNIT_KG_ID,
                "price_revision_snapshot": 7,
                "price_source_snapshot": "moysklad",
                "price_checked_at_snapshot": timezone.now(),
            }
        OrderItem.objects.create(
            order=order,
            product=self.product,
            product_name_snapshot=self.product.name,
            price_per_kg_snapshot=Decimal("1250.50"),
            desired_weight=3,
            line_total=Decimal("3751.50"),
            **snapshots,
        )
        return order

    # Повторная постановка одного принятого заказа возвращает ту же связь и
    # задачу; внутренний снимок заявки остаётся источником экспорта.
    def test_enqueue_is_idempotent_for_one_accepted_order(self):
        from integrations.models import MoySkladOrderLink, MoySkladTask
        from integrations.moysklad.order_export import enqueue_order_export

        order = self.accepted_order()
        first = enqueue_order_export(order)
        second = enqueue_order_export(order)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(MoySkladOrderLink.objects.filter(order=order).count(), 1)
        self.assertEqual(
            MoySkladTask.objects.filter(kind="export_order", order_link=first).count(), 1,
        )
        self.assertEqual(first.order.items.get().price_per_kg_snapshot, Decimal("1250.50"))

    # Новые локальные заявки до запуска экспорта сохраняются и отправляют
    # письмо продавцу, но не подхватываются интеграцией задним числом.
    def test_retail_submission_before_export_start_is_not_backfilled(self):
        from integrations.models import MoySkladOrderLink, MoySkladTask

        self.sync_state.export_started_at = None
        self.sync_state.save(update_fields=("export_started_at",))
        self.map_product()

        response = self.submit_retail()

        self.assertEqual(response.status_code, 302)
        order = Order.objects.get()
        self.assertTrue(order.privacy_consent)
        self.assertEqual(NotificationOutbox.objects.filter(order=order).count(), 1)
        self.assertFalse(MoySkladOrderLink.objects.filter(order=order).exists())
        self.assertFalse(MoySkladTask.objects.filter(kind="export_order").exists())

    # После регистрации экспорта флаг паузы не блокирует атомарную постановку:
    # принятую заявку, email и задачу можно обработать после возобновления.
    @override_settings(MOYSKLAD=MOYSKLAD_TEST_CONFIG | {"export_enabled": False})
    def test_paused_export_still_registers_task_with_retail_submission(self):
        from integrations.models import MoySkladOrderLink, MoySkladTask

        self.map_product()
        response = self.submit_retail()

        self.assertEqual(response.status_code, 302)
        order = Order.objects.get()
        link = MoySkladOrderLink.objects.get(order=order)
        self.assertEqual(NotificationOutbox.objects.filter(order=order).count(), 1)
        task = MoySkladTask.objects.get(kind="export_order", order_link=link)
        self.assertEqual(task.status, "pending")

    # Оптовое обращение остаётся локальным процессом даже после запуска
    # розничного экспорта и никогда не ставит задачу customerorder.
    def test_wholesale_submission_never_enqueues_order_export(self):
        from integrations.models import MoySkladTask

        page = self.client.get(reverse("inquiries:wholesale"))
        submission = page.context["submission"]
        response = self.client.post(
            reverse("inquiries:wholesale"),
            {
                **submission,
                "contact_name": "Оптовый клиент",
                "phone": "+7 900 000-00-01",
                "consent": "yes",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(MoySkladTask.objects.filter(kind="export_order").exists())

    # Ошибка постановки задачи откатывает всю транзакцию принятия розничной
    # заявки, включая сам заказ и email-outbox.
    def test_enqueue_exception_rolls_back_order_email_and_task(self):
        from integrations.models import MoySkladTask

        self.map_product()
        self.client.post(
            reverse("orders:cart"),
            {"action": "add", "product_id": str(self.product.pk), "weight": "3"},
        )
        submission = self.client.get(reverse("orders:cart")).context["submission"]
        data = {
            "action": "submit",
            **submission,
            "name": "Анна Тест",
            "phone": "+7 900 000-00-00",
            "email": "anna@example.test",
            "pickup_method": "delivery",
            "address": "Москва",
            "comment": "Тест транзакции",
            "consent": "yes",
        }

        with patch("integrations.moysklad.order_export.enqueue_order_export", side_effect=RuntimeError("enqueue failed")):
            with self.assertRaises(RuntimeError):
                self.client.post(reverse("orders:cart"), data)

        self.assertFalse(Order.objects.filter(submission_key=submission["submission_key"]).exists())
        self.assertFalse(NotificationOutbox.objects.exists())
        self.assertFalse(MoySkladTask.objects.filter(kind="export_order").exists())

    # Смена редакции сопоставления после показа корзины требует нового
    # подтверждения и не создаёт заявку со старой внешней идентичностью.
    def test_changed_mapping_after_cart_display_requires_confirmation(self):
        from django.db.models import F

        self.map_product()
        self.client.post(
            reverse("orders:cart"),
            {"action": "add", "product_id": str(self.product.pk), "weight": "3"},
        )
        submission = self.client.get(reverse("orders:cart")).context["submission"]
        Product.objects.filter(pk=self.product.pk).update(mapping_revision=F("mapping_revision") + 1)
        data = {
            "action": "submit",
            **submission,
            "name": "Анна Тест",
            "phone": "+7 900 000-00-00",
            "email": "anna@example.test",
            "pickup_method": "delivery",
            "address": "Москва",
            "comment": "Проверка новой связи",
            "consent": "yes",
        }

        response = self.client.post(reverse("orders:cart"), data)

        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "изменились", status_code=409)
        self.assertFalse(Order.objects.exists())
        self.assertEqual(self.client.session[CART_SESSION_KEY], {str(self.product.pk): "3"})

    # Цена в документе берётся из принятого снимка: последующее изменение
    # карточки и внешней связи не меняет цену, UUID или количество.
    def test_export_payload_uses_frozen_item_snapshot_and_exact_money_units(self):
        from integrations.moysklad.order_export import process_export_task
        from integrations.moysklad.queue import claim_task

        order = self.accepted_order(
            comment="\nКомментарий с точной строкой\n" + "текст " * 400,
            address="А" * 255,
        )
        from integrations.moysklad.order_export import enqueue_order_export
        link = enqueue_order_export(order)
        task = claim_task()
        from catalog.pricing import change_product_price
        change_product_price(product_id=self.product.pk, price="9999.99", actor=self.owner)

        client = Mock()
        client.get.return_value = product_payload(self.external_id, price="1250.50")
        client.post.side_effect = lambda path, json: customer_order_response(
            json, uuid.UUID("30000000-0000-4000-8000-000000000001"), link.sync_id,
        )

        process_export_task(task, client=client)

        link.refresh_from_db()
        payload = client.post.call_args.kwargs["json"]
        self.assertEqual(payload["positions"][0]["price"], 125050)
        self.assertEqual(payload["positions"][0]["quantity"], 3)
        self.assertEqual(payload["positions"][0]["reserve"], 0)
        self.assertEqual(payload["positions"][0]["discount"], 0)
        self.assertEqual(payload["positions"][0]["vat"], 0)
        self.assertIs(payload["positions"][0]["vatEnabled"], False)
        self.assertEqual(
            payload["positions"][0]["price"] * payload["positions"][0]["quantity"],
            375150,
        )
        self.assertEqual(payload["organization"]["meta"]["href"].rsplit("/", 1)[-1], str(ORGANIZATION_ID))
        self.assertEqual(payload["agent"]["meta"]["href"].rsplit("/", 1)[-1], str(AGENT_ID))
        self.assertEqual(payload["state"]["meta"]["href"].rsplit("/", 1)[-1], str(INITIAL_STATE_ID))
        self.assertEqual(payload["rate"]["currency"]["meta"]["href"].rsplit("/", 1)[-1], str(CURRENCY_ID))
        self.assertEqual(payload["shipmentAddress"], "А" * 255)
        self.assertIn("\nКомментарий с точной строкой\n", payload["description"])
        self.assertEqual(link.prepared_payload, payload)
        self.assertTrue(link.payload_hash)

    # Максимальные допустимые новые контакты и текст комментария сохраняются
    # в описании без изменения строк и остаются внутри лимита API.
    def test_description_preserves_maximum_new_contact_fields_and_comment(self):
        from integrations.moysklad.order_export import enqueue_order_export, process_export_task
        from integrations.moysklad.queue import claim_task

        email = "a" * 62 + "@" + "b" * 63 + "." + "c" * 63 + "." + "d" * 58 + ".test"
        self.assertEqual(len(email), 254)
        name = "Н" * 160
        phone = "7" * 40
        address = "А" * 255
        prefix, suffix = "первая строка\n", "\nпоследняя строка"
        comment = prefix + "К" * (3000 - len(prefix) - len(suffix)) + suffix
        order = self.accepted_order(
            name=name, phone=phone, email=email, address=address, comment=comment,
        )
        link = enqueue_order_export(order)
        client = Mock(
            get=Mock(return_value=product_payload(self.external_id, price="1250.50")),
            post=Mock(side_effect=lambda path, json: customer_order_response(
                json, uuid.UUID("30000000-0000-4000-8000-000000000010"), link.sync_id,
            )),
        )

        process_export_task(claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("export_order",)), client=client)

        description = client.post.call_args.kwargs["json"]["description"]
        expected = (
            f"Имя: {name}\nТелефон: {phone}\nEmail: {email}\n"
            f"Получение: Доставка\nКомментарий:\n{comment}"
        )
        self.assertEqual(description, expected)
        self.assertLessEqual(len(description), 4096)
        self.assertEqual(client.post.call_args.kwargs["json"]["shipmentAddress"], address)

    # У исторической заявки с текстом, превышающим лимит описания, задача
    # требует ручной проверки; исходный комментарий не обрезается для отправки.
    def test_historic_overlong_description_moves_to_manual_review_without_truncation(self):
        from integrations.models import MoySkladTask
        from integrations.moysklad.order_export import enqueue_order_export, process_export_task
        from integrations.moysklad.queue import claim_task

        comment = "исторический комментарий " * 250
        order = self.accepted_order(comment=comment)
        link = enqueue_order_export(order)
        task = MoySkladTask.objects.get(kind="export_order", order_link=link)
        client = Mock(get=Mock(return_value=product_payload(self.external_id, price="1250.50")))

        if task.status == "pending":
            process_export_task(
                claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("export_order",)),
                client=client,
            )

        link.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(task.status, "manual_review")
        client.post.assert_not_called()
        if link.prepared_payload:
            self.assertIn(comment, link.prepared_payload["description"])
            self.assertGreater(len(link.prepared_payload["description"]), 4096)

    # Архивный товар и изменившаяся единица не создают документ и не продвигают
    # сохранённую цену или время её проверки на карточке сайта.
    def test_archived_or_wrong_unit_product_stops_before_create_without_price_apply(self):
        from integrations.models import MoySkladProduct, MoySkladTask
        from integrations.moysklad.order_export import enqueue_order_export, process_export_task
        from integrations.moysklad.queue import claim_task

        self.map_product()
        source = MoySkladProduct.objects.get(account_id=ACCOUNT_ID, external_id=self.external_id)
        initial_checked = (source.last_checked_price, source.price_checked_at)
        before_site_price = self.product.price_per_kg

        for case, archived, unit_id in (
            ("archived", True, UNIT_KG_ID),
            ("unit_changed", False, UNIT_PIECE_ID),
        ):
            with self.subTest(case=case):
                order = self.accepted_order()
                link = enqueue_order_export(order)
                task = MoySkladTask.objects.get(kind="export_order", order_link=link)
                client = Mock(
                    get=Mock(return_value=product_payload(
                        self.external_id, price="9999.99", archived=archived, unit_id=unit_id,
                    )),
                )

                process_export_task(
                    claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("export_order",)),
                    client=client,
                )

                task.refresh_from_db()
                source.refresh_from_db()
                self.product.refresh_from_db()
                self.assertEqual(task.status, "manual_review")
                self.assertIsNone(link.external_id)
                self.assertEqual((source.last_checked_price, source.price_checked_at), initial_checked)
                self.assertEqual(self.product.price_per_kg, before_site_price)
                client.post.assert_not_called()

    # Таймаут после отправки требует сначала поискать syncId, включая удалённые
    # документы; найденный документ связывается без второго POST.
    def test_timeout_recovers_existing_sync_id_without_second_create(self):
        from integrations.moysklad.order_export import enqueue_order_export, process_export_task
        from integrations.moysklad.queue import claim_task
        from integrations.moysklad.contracts import MoySkladError

        order = self.accepted_order()
        link = enqueue_order_export(order)
        task = claim_task()
        client = Mock()

        def lookup_after_timeout(path, params=None):
            if "customerorder" in str(path).lower():
                self.assertEqual(params["filter"], f"syncId={link.sync_id};isDeleted=true;isDeleted=false")
                link.refresh_from_db()
                found = customer_order_response(
                    link.prepared_payload, uuid.UUID("30000000-0000-4000-8000-000000000001"), link.sync_id,
                )
                return {"rows": [found], "meta": {"size": 1, "offset": 0, "limit": 100}}
            return product_payload(self.external_id, price="1250.50")

        client.get.side_effect = lookup_after_timeout
        client.post.side_effect = MoySkladError("timeout", retryable=True)

        process_export_task(task, client=client)

        self.assertEqual(client.post.call_count, 1)
        self.assertTrue(any(
            call.kwargs.get("params", {}).get("filter")
            == f"syncId={link.sync_id};isDeleted=true;isDeleted=false"
            for call in client.get.call_args_list
        ))
        link.refresh_from_db()
        self.assertEqual(link.external_id, uuid.UUID("30000000-0000-4000-8000-000000000001"))

    # Если первый timeout не подтверждил создание, повтор использует тот же
    # сохранённый payload и ссылки при изменениях в том же аккаунте.
    def test_retry_keeps_frozen_payload_after_same_account_configuration_changes(self):
        from integrations.models import MoySkladTask
        from integrations.moysklad.contracts import MoySkladError
        from integrations.moysklad.order_export import enqueue_order_export, process_export_task
        from integrations.moysklad.queue import claim_task

        order = self.accepted_order()
        link = enqueue_order_export(order)
        first_task = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("export_order",))
        client = Mock()

        def fake_get(path, params=None):
            if "customerorder" in str(path).lower():
                self.assertEqual(params["filter"], f"syncId={link.sync_id};isDeleted=true;isDeleted=false")
                return {"rows": [], "meta": {"size": 0, "offset": 0, "limit": 100}}
            return product_payload(self.external_id, price="1250.50")

        client.get.side_effect = fake_get
        def timeout_then_create(path, json):
            if client.post.call_count == 1:
                raise MoySkladError("timeout", retryable=True)
            return customer_order_response(
                json, uuid.UUID("30000000-0000-4000-8000-000000000006"), link.sync_id,
            )

        client.post.side_effect = timeout_then_create

        process_export_task(first_task, client=client)
        link.refresh_from_db()
        frozen_payload = link.prepared_payload
        self.assertTrue(frozen_payload)
        from catalog.pricing import change_product_price
        change_product_price(product_id=self.product.pk, price="9999.99", actor=self.owner)

        changed_config = MOYSKLAD_TEST_CONFIG | {
            "organization_id": str(uuid.UUID("40000000-0000-4000-8000-000000000002")),
            "agent_id": str(uuid.UUID("40000000-0000-4000-8000-000000000003")),
            "initial_state_id": str(uuid.UUID("40000000-0000-4000-8000-000000000004")),
        }
        with override_settings(MOYSKLAD=changed_config):
            MoySkladTask.objects.filter(pk=first_task.pk).update(next_attempt_at=timezone.now())
            retry_task = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("export_order",))
            process_export_task(retry_task, client=client)

        sent_payloads = [call.kwargs["json"] for call in client.post.call_args_list]
        self.assertEqual(sent_payloads, [frozen_payload, frozen_payload])
        link.refresh_from_db()
        self.assertEqual(link.account_id, ACCOUNT_ID)
        self.assertEqual(link.config_snapshot["account_id"], str(ACCOUNT_ID))
        self.assertEqual(link.config_snapshot["organization_id"], str(ORGANIZATION_ID))

    # Старый task сохраняет исходный account/environment и при смене активной
    # учётной записи уходит в ручной разбор без сетевых запросов.
    def test_old_task_is_not_sent_to_changed_account_or_environment(self):
        from integrations.models import MoySkladTask
        from integrations.moysklad.order_export import enqueue_order_export, process_export_task
        from integrations.moysklad.queue import claim_task

        link = enqueue_order_export(self.accepted_order())
        task = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("export_order",))
        replacement = MOYSKLAD_TEST_CONFIG | {
            "account_id": str(uuid.UUID("40000000-0000-4000-8000-000000000011")),
            "environment": "replacement-test",
        }
        client = Mock()

        with override_settings(MOYSKLAD_TEST_MODE=False, MOYSKLAD=replacement):
            process_export_task(task, client=client)

        task.refresh_from_db()
        link.refresh_from_db()
        self.assertEqual(task.status, "manual_review")
        self.assertEqual(link.account_id, ACCOUNT_ID)
        client.get.assert_not_called()
        client.post.assert_not_called()

    # Пауза оставляет уже поставленную задачу в очереди без обращения к API;
    # владелец может продолжить её после включения экспорта.
    def test_paused_worker_does_not_send_registered_task(self):
        from integrations.models import MoySkladTask
        from integrations.moysklad.order_export import enqueue_order_export, process_export_task
        from integrations.moysklad.queue import claim_task

        link = enqueue_order_export(self.accepted_order())
        task = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("export_order",))
        client = Mock()

        with override_settings(MOYSKLAD=MOYSKLAD_TEST_CONFIG | {"export_enabled": False}):
            process_export_task(task, client=client)

        task.refresh_from_db()
        self.assertEqual(task.status, "pending")
        self.assertEqual(task.order_link_id, link.pk)
        client.get.assert_not_called()
        client.post.assert_not_called()

    # Потерявший lease исполнитель не сохраняет полученный внешний UUID или
    # результат задачи после HTTP-ответа.
    def test_expired_lease_response_cannot_persist_external_document(self):
        from integrations.moysklad.order_export import enqueue_order_export, process_export_task
        from integrations.models import MoySkladTask
        from integrations.moysklad.queue import claim_task

        order = self.accepted_order()
        link = enqueue_order_export(order)
        task = claim_task()
        client = Mock()

        def expire_lease(*args, **kwargs):
            MoySkladTask.objects.filter(pk=task.pk).update(
                lease_expires_at=timezone.now() - timedelta(seconds=1),
            )
            return {
                "id": "30000000-0000-4000-8000-000000000003",
                "syncId": str(link.sync_id),
            }

        client.get.return_value = product_payload(self.external_id, price="1250.50")
        client.post.side_effect = expire_lease

        process_export_task(task, client=client)

        link.refresh_from_db()
        self.assertIsNone(link.external_id)
        self.assertIsNone(link.state_id)
        self.assertEqual(client.post.call_count, 1)

    # Историческая принятая заявка без внешних снимков остаётся на сайте и
    # переводится в ручной разбор без частичного документа.
    def test_missing_external_snapshot_moves_export_to_manual_review(self):
        from integrations.models import MoySkladTask
        from integrations.moysklad.order_export import enqueue_order_export, process_export_task
        from integrations.moysklad.queue import claim_task

        order = self.accepted_order(with_external_snapshots=False)
        link = enqueue_order_export(order)
        task = MoySkladTask.objects.get(kind="export_order", order_link=link)
        client = Mock()

        if task.status == "pending":
            process_export_task(
                claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("export_order",)),
                client=client,
            )

        order.refresh_from_db()
        task.refresh_from_db()
        self.assertTrue(Order.objects.filter(pk=order.pk).exists())
        self.assertEqual(task.status, "manual_review")
        self.assertIsNone(link.external_id)
        client.post.assert_not_called()

    # Чтение внешнего статуса обновляет только статус и время проверки; данные
    # принятой заявки, позиция и редакция согласия остаются исходными.
    def test_status_sync_preserves_accepted_order_and_item(self):
        from integrations.moysklad.order_export import enqueue_order_export
        from integrations.moysklad.queue import claim_task, complete_task, enqueue_task
        from integrations.moysklad.contracts import MoySkladError
        from integrations.moysklad.status_sync import process_status_task

        order = self.accepted_order()
        original = (order.name, order.comment, order.privacy_consent_revision_id)
        item = order.items.get()
        original_item = (item.product_name_snapshot, item.price_per_kg_snapshot, item.desired_weight)
        link = enqueue_order_export(order)
        export_task = claim_task()
        complete_task(export_task)
        link.external_id = uuid.UUID("30000000-0000-4000-8000-000000000002")
        link.save(update_fields=("external_id",))
        task = enqueue_task(
            kind="status_sync", dedupe_key=f"status:{link.pk}", account_id=ACCOUNT_ID,
            environment=ENVIRONMENT, order_link=link,
        )
        claimed = claim_task()
        client = Mock()
        client.get.return_value = {
            "id": str(link.external_id),
            "state": {"id": str(INITIAL_STATE_ID), "name": "Завершён", "stateType": {"name": "Successful"}},
        }

        process_status_task(claimed, client=client)

        link.refresh_from_db()
        order.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(link.state_id, INITIAL_STATE_ID)
        self.assertEqual(link.state_name, "Завершён")
        self.assertEqual(link.state_type, "Successful")
        self.assertIsNotNone(link.status_checked_at)
        self.assertEqual((order.name, order.comment, order.privacy_consent_revision_id), original)
        self.assertEqual((item.product_name_snapshot, item.price_per_kg_snapshot, item.desired_weight), original_item)
        self.assertEqual(task.pk, claimed.pk)

        previous_state = (link.state_id, link.state_name, link.state_type)
        previous_checked_at = link.status_checked_at
        retry_task = enqueue_task(
            kind="status_sync", dedupe_key=f"status-retry:{link.pk}", account_id=ACCOUNT_ID,
            environment=ENVIRONMENT, order_link=link,
        )
        retry_claim = claim_task()
        client.get.side_effect = MoySkladError("access_denied", status_code=403, retryable=True)
        process_status_task(retry_claim, client=client)
        link.refresh_from_db()
        self.assertEqual((link.state_id, link.state_name, link.state_type), previous_state)
        self.assertEqual(link.status_checked_at, previous_checked_at)
        self.assertEqual(retry_task.pk, retry_claim.pk)

    # Удалённый учётный заказ открывает инцидент и не восстанавливается
    # автоматическим POST или PUT.
    def test_deleted_external_document_opens_incident_without_recreation(self):
        from integrations.models import MoySkladIncident
        from integrations.moysklad.contracts import MoySkladError
        from integrations.moysklad.order_export import enqueue_order_export
        from integrations.moysklad.queue import claim_task, complete_task, enqueue_task
        from integrations.moysklad.status_sync import process_status_task

        order = self.accepted_order()
        link = enqueue_order_export(order)
        export_task = claim_task()
        complete_task(export_task)
        link.external_id = uuid.UUID("30000000-0000-4000-8000-000000000004")
        link.save(update_fields=("external_id",))
        enqueue_task(
            kind="status_sync", dedupe_key=f"status:{link.pk}", account_id=ACCOUNT_ID,
            environment=ENVIRONMENT, order_link=link,
        )
        task = claim_task()
        client = Mock()
        client.get.side_effect = MoySkladError("not_found", status_code=404)

        process_status_task(task, client=client)

        client.post.assert_not_called()
        self.assertTrue(MoySkladIncident.objects.filter(closed_at__isnull=True).exists())
        link.refresh_from_db()
        self.assertEqual(link.external_id, uuid.UUID("30000000-0000-4000-8000-000000000004"))

    # Повторяющаяся ошибка одного назначения обновляет существующий инцидент;
    # после закрытия новая ошибка создаёт отдельное уведомление.
    def test_same_open_incident_coalesces_and_reopens_after_close(self):
        from integrations.models import MoySkladIncident, NotificationOutbox
        from integrations.moysklad.incidents import close_incident, open_incident

        first = open_incident(scope="order_export", code="response_mismatch", object_ids=["R-ONE"])
        first_seen = first.last_seen_at
        repeated = open_incident(scope="order_export", code="response_mismatch", object_ids=["R-TWO"])

        self.assertEqual(first.pk, repeated.pk)
        self.assertGreaterEqual(repeated.last_seen_at, first_seen)
        self.assertCountEqual(repeated.object_ids, ["R-ONE", "R-TWO"])
        self.assertEqual(
            MoySkladIncident.objects.filter(scope="order_export", code="response_mismatch", closed_at__isnull=True).count(),
            1,
        )
        self.assertEqual(NotificationOutbox.objects.count(), 1)

        close_incident(scope="order_export", code="response_mismatch")
        reopened = open_incident(scope="order_export", code="response_mismatch", object_ids=["R-THREE"])
        self.assertNotEqual(first.pk, reopened.pk)
        self.assertEqual(NotificationOutbox.objects.count(), 2)


@override_settings(MOYSKLAD=MOYSKLAD_TEST_CONFIG)
class MoySkladExportHttpBoundaryTests(TransactionTestCase):
    # HTTP проверки выполняются без открытой БД-транзакции. TransactionTestCase
    # не добавляет оборачивающую транзакцию вокруг тестового метода.
    def test_export_api_calls_run_outside_database_transactions(self):
        from integrations.models import MoySkladSyncState
        from integrations.moysklad.order_export import enqueue_order_export, process_export_task
        from integrations.moysklad.queue import claim_task

        category = Category.objects.create(name="Boundary", slug=f"boundary-{uuid.uuid4().hex}")
        product = Product.objects.create(
            name="Проверка границы", slug=f"boundary-fish-{uuid.uuid4().hex}", category=category,
            price_per_kg=Decimal("1250.50"), is_published=True,
        )
        revision = get_current_consent_revision()
        order = Order.objects.create(
            name="Boundary", phone="+7 900 000-00-00", fulfillment_method=Order.FulfillmentMethod.PICKUP,
            privacy_consent=True, privacy_consent_at=timezone.now(), privacy_consent_revision=revision,
        )
        OrderItem.objects.create(
            order=order, product=product, product_name_snapshot=product.name,
            price_per_kg_snapshot=Decimal("1250.50"), desired_weight=3,
            line_total=Decimal("3751.50"), external_account_id_snapshot=ACCOUNT_ID,
            external_entity_type_snapshot="product", external_id_snapshot=uuid.UUID("20000000-0000-4000-8000-000000000005"),
            unit_id_snapshot=UNIT_KG_ID,
            price_revision_snapshot=1, price_source_snapshot="moysklad",
            price_checked_at_snapshot=timezone.now(),
        )
        state, _ = MoySkladSyncState.objects.get_or_create(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, defaults={"namespace": NAMESPACE},
        )
        state.export_started_at = timezone.now() - timedelta(days=1)
        state.export_consent_revision = revision
        state.save(update_fields=("export_started_at", "export_consent_revision"))
        link = enqueue_order_export(order)
        task = claim_task()
        external_id = uuid.UUID("30000000-0000-4000-8000-000000000005")

        def checked_get(path, params=None):
            self.assertFalse(connection.in_atomic_block, f"GET {path} ran in a database transaction")
            return product_payload(uuid.UUID("20000000-0000-4000-8000-000000000005"), price="1250.50")

        def checked_post(path, json=None):
            self.assertFalse(connection.in_atomic_block, f"POST {path} ran in a database transaction")
            return customer_order_response(json, external_id, link.sync_id)

        client = Mock(get=checked_get, post=checked_post)
        process_export_task(task, client=client)

        self.assertEqual(Order.objects.get(pk=order.pk).order_number, order.order_number)
