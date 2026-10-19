"""Behavior and PostgreSQL integrity checks for the data-layer milestone."""
# Тесты этого файла запускаются командой:
# .venv/bin/python manage.py test tests.test_data_layer --settings=fishsite.settings_test --noinput
# Django создаёт отдельную БД test_fishsite в изолированном PostgreSQL-кластере
# fishsite-tests (127.0.0.1:15432, роль fishsite_tester), применяет миграции,
# затем удаляет БД. Нужны подготовленное тестовое окружение и запущенный кластер;
# обычная БД и внешняя сеть не используются. TestCase даёт транзакционные
# откаты и общие начальные данные через setUpTestData. Сценарии проверяют модели,
# ORM и ограничения БД, включая отдельные atomic-блоки и SQL через cursor;
# HTTP-поведение проверяется Django Client, а миграция контента вызывается
# напрямую. assertNumQueries фиксирует число SQL-запросов для страниц.
import importlib
import uuid
from decimal import Decimal
from types import SimpleNamespace

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.core.exceptions import FieldDoesNotExist
from django.db import IntegrityError, connection, transaction
from django.db.models.deletion import ProtectedError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from catalog.models import Category, Product, ProductImage
from catalog.pricing import change_product_price
from content.consent_revisions import get_current_consent_revision
from content.models import ContentPage, ContentSection, HOME_SECTION_KEYS
from inquiries.models import Inquiry
from integrations.models import NotificationOutbox
from orders.models import Order, OrderItem
from orders.money import calculate_line_total
from tests.backoffice_helpers import login_verified_admin


class ModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.category = Category.objects.create(name="Тестовая категория", slug="test-category")
        cls.product = Product.objects.create(name="Тестовый товар", slug="test-product", category=cls.category, price_per_kg=Decimal("10.05"))
        cls.consent_revision = get_current_consent_revision()

    def order(self, **overrides):
        values = dict(name="Тест", phone="000000000", fulfillment_method="pickup", privacy_consent=True, privacy_consent_at=timezone.now(), privacy_consent_revision=self.consent_revision)
        return Order.objects.create(**(values | overrides))

    def inquiry(self, **overrides):
        values = dict(contact_name="Тест", phone="000000000", need="Тестовая потребность", privacy_consent=True, privacy_consent_at=timezone.now(), privacy_consent_revision=self.consent_revision)
        return Inquiry.objects.create(**(values | overrides))

    def item(self, **overrides):
        values = dict(order=self.order(), product=self.product, product_name_snapshot=self.product.name, price_per_kg_snapshot=self.product.price_per_kg, desired_weight=Decimal("0.1"))
        return OrderItem.objects.create(**(values | overrides))

    def reject_db(self, callback):
        with self.assertRaises(IntegrityError), transaction.atomic():
            callback()

    # Проверить, что integrity-сценарии действительно запущены на PostgreSQL.
    def test_real_postgresql(self):
        self.assertEqual(connection.vendor, "postgresql", "These integrity tests must run on PostgreSQL.")

    # Создать вторую независимую категорию; проверить FK товара, отсутствие
    # поля parent, разрешённое удаление пустой категории и защиту категории товара.
    def test_categories_are_independent_and_products_keep_category(self):
        another = Category.objects.create(name="Другая категория", slug="other-category")
        self.assertEqual(self.product.category_id, self.category.pk)
        self.assertEqual(
            list(Category.objects.order_by("pk").values_list("pk", flat=True)),
            [self.category.pk, another.pk],
        )
        with self.assertRaises(FieldDoesNotExist):
            Category._meta.get_field("parent")
        another.delete()
        with self.assertRaises(ProtectedError):
            self.category.delete()

    # Через QuerySet.update обойти model validation и убедиться, что БД
    # отклоняет повтор slug отдельно для категории и товара.
    def test_catalog_slug_uniqueness_in_database(self):
        another = Category.objects.create(name="Иная", slug="another")
        self.reject_db(lambda: Category.objects.filter(pk=another.pk).update(slug=self.category.slug))
        product = Product.objects.create(name="Другой", slug="other-product", category=another, price_per_kg=Decimal("1"))
        self.reject_db(lambda: Product.objects.filter(pk=product.pk).update(slug=self.product.slug))

    # Проверить Decimal-типы и defaults веса, пустую выборку неопубликованных
    # товаров, затем включить публикацию и получить единственный товар.
    def test_product_decimal_defaults_and_publication_query(self):
        self.product.refresh_from_db()
        self.assertEqual(self.product.minimum_weight, Decimal("0.100"))
        self.assertEqual(self.product.weight_step, Decimal("0.100"))
        self.assertIsInstance(self.product.price_per_kg, Decimal)
        self.assertEqual(Product.objects.count(), 1)
        self.assertFalse(Product.objects.published().exists())
        self.product.is_published = True
        self.product.save()
        self.assertEqual(list(Product.objects.published()), [self.product])

    # Для price_per_kg, minimum_weight и weight_step проверить значения 0 и -1:
    # save() должен дать ValidationError, а bulk update — IntegrityError.
    # Дополнительно проверить ValidationError для нечисловой цены при save().
    def test_positive_decimal_fields_validate_and_database_rejects_bulk_bypass(self):
        for field in ("price_per_kg", "minimum_weight", "weight_step"):
            for invalid in (Decimal("0"), Decimal("-1")):
                with self.subTest(field=field, invalid=invalid):
                    setattr(self.product, field, invalid)
                    with self.assertRaises(ValidationError):
                        self.product.save()
                    self.product.refresh_from_db()
                    self.reject_db(lambda: Product.objects.filter(pk=self.product.pk).update(**{field: invalid}))
        self.product.price_per_kg = "not-a-number"
        with self.assertRaises(ValidationError):
            self.product.save()

    # Проверить None без фото, выбор фото с минимальным display_order (при
    # равенстве — по порядку), приоритет явно основного фото, запрет второго
    # основного фото ограничением БД и fallback на оставшееся фото после удаления.
    def test_primary_photo_constraint_and_deterministic_fallback(self):
        self.assertIsNone(self.product.primary_photo)
        a = ProductImage.objects.create(product=self.product, image="tests/a.png", display_order=4)
        b = ProductImage.objects.create(product=self.product, image="tests/b.png", display_order=1)
        c = ProductImage.objects.create(product=self.product, image="tests/c.png", display_order=1)
        self.assertEqual(self.product.primary_photo.pk, b.pk)
        a.is_primary = True
        a.save()
        self.assertEqual(self.product.primary_photo.pk, a.pk)
        self.reject_db(lambda: ProductImage.objects.filter(pk=c.pk).update(is_primary=True))
        a.delete()
        self.assertEqual(self.product.primary_photo.pk, b.pk)

    # Самовывоз допускает пустой адрес; доставка отвергает пустую и состоящую
    # из пробельных символов строки, а БД запрещает такую строку и неизвестный
    # способ получения. Проверить ограничения согласия в БД и валидацию имени,
    # телефона и согласия при создании; корректная доставка сохраняется.
    def test_order_address_and_consent(self):
        self.order(fulfillment_method="pickup", delivery_address="")
        for address in ("", " \n\t"):
            with self.assertRaises(ValidationError):
                self.order(fulfillment_method="delivery", delivery_address=address)
        delivery = self.order(fulfillment_method="delivery", delivery_address="Тестовый адрес")
        self.reject_db(lambda: Order.objects.filter(pk=delivery.pk).update(delivery_address=" \n\t"))
        self.reject_db(lambda: Order.objects.filter(pk=delivery.pk).update(fulfillment_method="unknown"))
        self.reject_db(lambda: Order.objects.filter(pk=delivery.pk).update(privacy_consent=False))
        self.reject_db(lambda: Order.objects.filter(pk=delivery.pk).update(privacy_consent_at=None))
        for values in ({"name": " "}, {"phone": " "}, {"privacy_consent": False}):
            with self.assertRaises(ValidationError):
                self.order(**values)

    # Проверить UUIDv4 и уникальность success_token у двух заказов; для заказа
    # проверить уникальность submission_key, success_token и order_number,
    # для двух оптовых запросов — submission_key и inquiry_number.
    def test_unique_submission_success_token_and_numbers(self):
        a, b = self.order(), self.order()
        self.assertIsInstance(a.success_token, uuid.UUID)
        self.assertEqual(a.success_token.version, 4)
        self.assertNotEqual(a.success_token, b.success_token)
        for field in ("submission_key", "success_token", "order_number"):
            self.reject_db(lambda: Order.objects.filter(pk=b.pk).update(**{field: getattr(a, field)}))
        ia, ib = self.inquiry(), self.inquiry()
        for field in ("submission_key", "inquiry_number"):
            self.reject_db(lambda: Inquiry.objects.filter(pk=ib.pk).update(**{field: getattr(ia, field)}))

    # Проверить округление 10.05 × 0.1 до 1.01; при создании позиции сервер
    # должен заменить подставленную сумму 999.99 и рассчитать сумму из снимков.
    # Нечисловой desired_weight должен приводить к ValidationError.
    def test_money_rounding_and_server_computed_amount(self):
        self.assertEqual(calculate_line_total(Decimal("10.05"), Decimal("0.1")), Decimal("1.01"))
        item = self.item(line_total=Decimal("999.99"))
        self.assertEqual(item.line_total, Decimal("1.01"))
        item = self.item(price_per_kg_snapshot="10.05", desired_weight="0.1")
        self.assertEqual(item.line_total, Decimal("1.01"))
        with self.assertRaises(ValidationError):
            self.item(desired_weight="bad")

    # После изменения названия, цены, весовых параметров и публикации товара
    # снимок позиции должен проходить full_clean; удаление товара обнуляет FK,
    # сохраняя снимки названия/цены, вес и рассчитанную сумму без изменений.
    def test_history_survives_catalog_changes_and_product_deletion(self):
        item = self.item()
        before = (item.product_name_snapshot, item.price_per_kg_snapshot, item.desired_weight, item.line_total)
        change_product_price(
            product_id=self.product.pk,
            price=Decimal("20"),
            source=Product.PriceSource.MANUAL,
        )
        self.product.refresh_from_db()
        self.product.name = "Изменённый товар"
        self.product.minimum_weight = Decimal("2")
        self.product.weight_step = Decimal("0.5")
        self.product.is_published = False
        self.product.save()
        item.refresh_from_db()
        item.full_clean()  # History is not validated against current weight settings.
        self.product.delete()
        item.refresh_from_db()
        self.assertIsNone(item.product_id)
        self.assertEqual((item.product_name_snapshot, item.price_per_kg_snapshot, item.desired_weight, item.line_total), before)

    # Запретить изменение исторического снимка через save, update, bulk_update,
    # bulk_create с конфликтным обновлением (оба варианта аргументов), повторный
    # save существующего pk и замену записи; исходный снимок в БД остаётся прежним.
    def test_snapshots_immutable_in_supported_orm_paths(self):
        item = self.item()
        item.product_name_snapshot = "Подмена"
        with self.assertRaises(ValidationError):
            item.save()
        with self.assertRaises(ValidationError):
            OrderItem.objects.filter(pk=item.pk).update(product_name_snapshot="Подмена")
        with self.assertRaises(ValidationError):
            OrderItem.objects.bulk_update([item], ["product_name_snapshot"])
        with self.assertRaises(ValidationError):
            OrderItem.objects.bulk_create([item], update_conflicts=True, update_fields=["product_name_snapshot"], unique_fields=["pk"])
        with self.assertRaises(ValidationError):
            OrderItem.objects.bulk_create([item], None, False, True, ["product_name_snapshot"], ["pk"])
        replacement = OrderItem(pk=item.pk, order=item.order, product_name_snapshot="Подмена", price_per_kg_snapshot=Decimal("1"), desired_weight=Decimal("1"))
        with self.assertRaises(ValidationError):
            replacement.save()
        item.refresh_from_db()
        self.assertEqual(item.product_name_snapshot, self.product.name)

    # Прямыми UPDATE через cursor обойти ORM и проверить CHECK-ограничения БД:
    # desired_weight=0, price_per_kg_snapshot=0 и line_total=-1 дают IntegrityError.
    def test_item_database_positive_constraints(self):
        item = self.item()
        # SQL deliberately bypasses supported immutable APIs to verify CHECKs.
        for field, value in (("desired_weight", "0"), ("price_per_kg_snapshot", "0"), ("line_total", "-1")):
            with self.assertRaises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(f'UPDATE orders_orderitem SET {field} = %s WHERE id = %s', [value, item.pk])

    # Пустые контактное лицо и телефон, а также отсутствие согласия должны
    # отклоняться валидацией; пустая потребность допустима, но БД запрещает
    # снять согласие через update. У Inquiry нет атрибута items.
    def test_wholesale_contact_and_consent_are_required_but_need_is_optional(self):
        for values in ({"contact_name": " "}, {"phone": " "}, {"privacy_consent": False}):
            with self.assertRaises(ValidationError):
                self.inquiry(**values)
        inquiry = self.inquiry(need="")
        self.reject_db(lambda: Inquiry.objects.filter(pk=inquiry.pk).update(privacy_consent=False))
        self.assertFalse(hasattr(inquiry, "items"))

    # Проверить валидацию при отсутствии родителя и при двух родителях;
    # CHECK-ограничения БД должны отклонять удаление единственной ссылки,
    # вторую ссылку, несовместимые статусы/lease и отрицательные попытки.
    # Затем проверить допустимые переходы pending→processing→sent, отклонение
    # второго родителя для другой записи и защиту заказа от удаления.
    def test_outbox_parent_and_state_integrity(self):
        order, inquiry = self.order(), self.inquiry()
        with self.assertRaises(ValidationError):
            NotificationOutbox.objects.create()
        with self.assertRaises(ValidationError):
            NotificationOutbox.objects.create(order=order, inquiry=inquiry)
        outbox = NotificationOutbox.objects.create(order=order)
        self.reject_db(lambda: NotificationOutbox.objects.filter(pk=outbox.pk).update(order=None))
        self.reject_db(lambda: NotificationOutbox.objects.filter(pk=outbox.pk).update(inquiry=inquiry))
        self.reject_db(lambda: NotificationOutbox.objects.filter(pk=outbox.pk).update(status="processing"))
        self.reject_db(lambda: NotificationOutbox.objects.filter(pk=outbox.pk).update(status="sent"))
        self.reject_db(lambda: NotificationOutbox.objects.filter(pk=outbox.pk).update(lease_token=uuid.uuid4()))
        self.reject_db(lambda: NotificationOutbox.objects.filter(pk=outbox.pk).update(attempt_count=-1))
        self.reject_db(lambda: NotificationOutbox.objects.filter(pk=outbox.pk).update(status="unknown"))
        outbox.status = "processing"
        outbox.lease_token = uuid.uuid4()
        outbox.lease_expires_at = timezone.now() + timezone.timedelta(minutes=2)
        outbox.save()
        outbox.status = "sent"
        outbox.sent_at = timezone.now()
        outbox.lease_token = outbox.lease_expires_at = None
        outbox.save()
        other = NotificationOutbox.objects.create(inquiry=inquiry)
        self.reject_db(lambda: NotificationOutbox.objects.filter(pk=other.pk).update(order=order, inquiry=None))
        with self.assertRaises(ProtectedError):
            order.delete()


class ContentTests(TestCase):
    def page(self, key):
        return ContentPage.objects.get(key=key)

    # Проверить фиксированный набор пяти страниц, точный набор блоков главной
    # и отсутствие блоков с непустым body после начального наполнения.
    def test_seeded_fixed_pages_and_empty_blocks(self):
        self.assertEqual(set(ContentPage.objects.values_list("key", flat=True)), {"home", "about", "delivery", "contacts", "privacy"})
        self.assertEqual(set(self.page("home").sections.values_list("key", flat=True)), set(HOME_SECTION_KEYS))
        self.assertFalse(ContentSection.objects.exclude(body="").exists())

    # Изменить заголовок и текст редактора, удалить блок categories и дважды
    # вызвать seed-функцию миграции; пользовательский текст должен сохраниться,
    # а число блоков главной восстановиться до HOME_SECTION_KEYS.
    def test_seed_is_repeatable_without_overwriting_editor_content(self):
        page = self.page("home")
        page.title = "Сохранённый заголовок"
        page.save()
        hero = page.sections.get(key="hero")
        hero.body = "Сохранённый текст"
        hero.save()
        page.sections.filter(key="categories").delete()
        migration = importlib.import_module("content.migrations.0002_seed_fixed_pages")
        migration.seed_fixed_content(apps, SimpleNamespace(connection=connection))
        migration.seed_fixed_content(apps, SimpleNamespace(connection=connection))
        page.refresh_from_db()
        hero.refresh_from_db()
        self.assertEqual(page.title, "Сохранённый заголовок")
        self.assertEqual(hero.body, "Сохранённый текст")
        self.assertEqual(page.sections.count(), len(HOME_SECTION_KEYS))

    # Отклонить произвольный ключ страницы валидацией и CHECK БД, смену ключа
    # существующей страницы ограничением уникальности, дубликат блока через
    # создание и bulk_create, а также переименование сохранённого блока.
    def test_fixed_keys_and_unique_section_key(self):
        page = self.page("about")
        with self.assertRaises(ValidationError):
            ContentPage.objects.create(key="arbitrary")
        with self.assertRaises(IntegrityError), transaction.atomic():
            ContentPage.objects.filter(pk=page.pk).update(key="arbitrary")
        with self.assertRaises(IntegrityError), transaction.atomic():
            ContentPage.objects.filter(pk=page.pk).update(key="home")
        section = ContentSection.objects.create(page=page, key="test", body="Тест")
        with self.assertRaises(ValidationError):
            ContentSection.objects.create(page=page, key="test")
        with self.assertRaises(IntegrityError), transaction.atomic():
            ContentSection.objects.bulk_create([ContentSection(page=page, key="test")])
        section.key = "changed"
        with self.assertRaises(ValidationError):
            section.save()

    # Единый текст страницы сохраняет абзацы и экранирование HTML; прежние
    # блоки не участвуют в показе. Страница с SEO укладывается в 3 запроса.
    def test_info_page_content_order_seo_and_safe_paragraphs(self):
        page = self.page("about")
        page.title = "Тестовая страница"
        page.seo_title = "Тестовый SEO-заголовок"
        page.seo_description = 'Описание "тест" <script>'
        page.body = 'Абзац один\n\n<script>alert("x")</script>'
        page.save()
        ContentSection.objects.create(page=page, key="late", title="Поздний блок", body="Поздний текст", display_order=200)
        ContentSection.objects.create(page=page, key="early", title="Первый блок", body='Абзац один\n\n<script>alert("x")</script>', display_order=100)
        with self.assertNumQueries(3):
            response = self.client.get("/about/")
        self.assertContains(response, "Тестовая страница")
        self.assertContains(response, "Тестовый SEO-заголовок")
        self.assertContains(response, "Описание &quot;тест&quot; &lt;script&gt;")
        self.assertContains(response, "<p>Абзац один</p>", html=True)
        self.assertNotContains(response, '<script>alert("x")</script>')
        self.assertContains(response, "&lt;script&gt;alert")
        self.assertNotContains(response, "Первый блок")
        self.assertNotContains(response, "Поздний блок")

    # Для home, about, delivery, contacts и privacy записать отдельный заголовок;
    # для каждого маршрута проверить HTTP 200, показ заголовка и фиксированный
    # предел: 6 запросов для главной, 4 для контактов, 3 для остальных.
    def test_all_content_routes_read_database_and_remain_bounded(self):
        for key, path in (("home", "/"), ("about", "/about/"), ("delivery", "/delivery/"), ("contacts", "/contacts/"), ("privacy", "/privacy/")):
            with self.subTest(key=key):
                page = self.page(key)
                page.title = f"Проверка страницы {key}"
                page.save()
                # Info routes load page + shared catalog navigation.
                # Home also reads featured products, categories and readiness.
                expected_queries = 6 if path == "/" else (4 if path == "/contacts/" else 3)
                with self.assertNumQueries(expected_queries):
                    response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, page.title)

    # Удалить страницы как явную подготовку сценария и запросить главную плюс
    # about, delivery, contacts, privacy: все ответы 200, текст advantage_1
    # отсутствует; лимит запросов — 5 для главной и 3 для остальных.
    def test_missing_pages_and_sections_have_safe_empty_states(self):
        ContentPage.objects.all().delete()  # Explicit destructive test setup only.
        for path in ("/", "/about/", "/delivery/", "/contacts/", "/privacy/"):
            # Missing info pages still render shared navigation. Home renders
            # its published catalog sections even without editable content.
            expected_queries = 6 if path == "/" else (4 if path == "/contacts/" else 3)
            with self.subTest(path=path), self.assertNumQueries(expected_queries):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "advantage_1")

    # Старые настройки блоков не меняют готовые подписи и шаги главной.
    # Единственный редактируемый текст появляется в секции «О компании».
    def test_home_uses_single_body_and_ignores_archived_blocks(self):
        page = self.page("home")
        page.body = "Единый текст главной\n\nВторой абзац"
        page.save()
        section = page.sections.get(key="step_1")
        section.title = "Устаревший шаг"
        section.save()
        with self.assertNumQueries(6):
            response = self.client.get("/")
        self.assertContains(response, "Единый текст главной", count=1)
        self.assertContains(response, "<p>Второй абзац</p>", html=True)
        self.assertNotContains(response, "Устаревший шаг")
        self.assertContains(response, "Выберите товары")


class AdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.editor = get_user_model().objects.create_user(username="test-editor", is_staff=True)
        cls.editor.user_permissions.set(Permission.objects.filter(content_type__app_label="content"))
        cls.consent_revision = get_current_consent_revision()

    def payload(self, page):
        return {"title": page.title, "seo_title": page.seo_title,
                "seo_description": page.seo_description, "body": page.body}

    # Владелец сохраняет единый текст главной без inline-форм; анонимный
    # посетитель видит оба абзаца и изменённый заголовок.
    def test_staff_updates_home_text_then_anonymous_sees_changes(self):
        login_verified_admin(self.client, self.editor)
        page = ContentPage.objects.get(key="home")
        url = reverse("admin:content_contentpage_change", args=[page.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        data = self.payload(page)
        data.update(body="Проверка сохранения из админки\n\nВторой абзац",
                    title="Проверка главной", seo_title="Проверка SEO")
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302, response.context and str(response.context.get("errors")))
        public = Client().get("/")
        self.assertContains(public, "Проверка сохранения из админки")
        self.assertContains(public, "Второй абзац")
        self.assertContains(public, "Проверка главной")

    # Сохранить единый текст доставки без inline management form; публичная
    # страница сразу показывает новое содержимое.
    def test_staff_updates_information_page_and_sees_saved_text(self):
        login_verified_admin(self.client, self.editor)
        page = ContentPage.objects.get(key="delivery")
        data = {"title": page.title, "seo_title": page.seo_title,
                "seo_description": page.seo_description, "body": "Тестовый текст доставки"}
        response = self.client.post(reverse("admin:content_contentpage_change", args=[page.pk]), data)
        self.assertEqual(response.status_code, 302)
        self.assertContains(Client().get("/delivery/"), "Тестовый текст доставки")

    # Подделать в POST ключ главной страницы, технический ключ hero и DELETE;
    # после успешного redirect ключ страницы остаётся home, а набор блоков
    # остаётся равным HOME_SECTION_KEYS.
    def test_forged_structural_key_delete_and_page_key_are_ignored(self):
        login_verified_admin(self.client, self.editor)
        page = ContentPage.objects.get(key="home")
        data = self.payload(page)
        data.update({"sections-TOTAL_FORMS": "1", "sections-INITIAL_FORMS": "0",
                     "sections-0-key": "changed", "sections-0-body": "Подмена",
                     "sections-0-DELETE": "on"})
        data["key"] = "privacy"
        response = self.client.post(reverse("admin:content_contentpage_change", args=[page.pk]), data)
        self.assertEqual(response.status_code, 302)
        page.refresh_from_db()
        self.assertEqual(page.key, "home")
        self.assertEqual(set(page.sections.values_list("key", flat=True)), set(HOME_SECTION_KEYS))

    # Анонимный POST в редактирование админки должен перенаправить на login;
    # пустые POST checkout и wholesale должны дать 400 и не создать Order/Inquiry.
    def test_anonymous_cannot_edit_admin_and_invalid_public_posts_create_no_requests(self):
        page = ContentPage.objects.get(key="home")
        response = self.client.post(reverse("admin:content_contentpage_change", args=[page.pk]), {"title": "Подмена"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response.url)
        for path in ("/checkout/", "/wholesale/"):
            self.assertEqual(self.client.post(path, {}).status_code, 400)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(Inquiry.objects.count(), 0)

    # В админском inline отметить основным первое фото при сохранении раньше
    # бывшего основного: POST успешен и primary_photo переключается. Затем
    # отметить два фото одновременно: форма отвечает 200 с ошибкой и в БД
    # остаётся ровно одно основное фото.
    def test_photo_inline_switches_primary_before_saving_earlier_row(self):
        self.editor.user_permissions.add(*Permission.objects.filter(content_type__app_label="catalog"))
        login_verified_admin(self.client, self.editor)
        category = Category.objects.create(name="Тест", slug="admin-test")
        product = Product.objects.create(name="Тест", slug="admin-product", category=category, price_per_kg=Decimal("10"))
        first = ProductImage.objects.create(product=product, image="tests/first.png", display_order=0)
        old_primary = ProductImage.objects.create(product=product, image="tests/old.png", display_order=10, is_primary=True)
        url = reverse("admin:catalog_product_change", args=[product.pk])
        form_response = self.client.get(url)
        self.assertEqual(form_response.status_code, 200)
        product_form = form_response.context["adminform"].form
        data = {name: product_form[name].value() for name in product_form.fields}
        data.update({"images-TOTAL_FORMS": 2, "images-INITIAL_FORMS": 2, "images-MIN_NUM_FORMS": 0, "images-MAX_NUM_FORMS": 1000})
        for i, image in enumerate((first, old_primary)):
            data.update({f"images-{i}-id": image.pk, f"images-{i}-product": product.pk, f"images-{i}-alt_text": "Тест", f"images-{i}-display_order": image.display_order})
        data["images-0-is_primary"] = "on"
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(product.primary_photo.pk, first.pk)
        data["images-1-is_primary"] = "on"
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Отметьте основным только одно фото товара.")
        self.assertEqual(product.images.filter(is_primary=True).count(), 1)

    # Суперпользователь не должен видеть поле снимка в форме заказа; поддельный
    # POST с изменением/удалением позиции отклоняется и оставляет заявку неизменной,
    # а удаление самого заказа через админку запрещено ответом 403.
    def test_request_snapshots_are_read_only_in_admin(self):
        user = get_user_model().objects.create_superuser(username="test-admin", email="")
        login_verified_admin(self.client, user)
        order = Order.objects.create(name="Тест", phone="000000000", fulfillment_method="pickup", privacy_consent=True, privacy_consent_at=timezone.now(), privacy_consent_revision=self.consent_revision)
        item = OrderItem.objects.create(order=order, product_name_snapshot="Исторический товар", price_per_kg_snapshot=Decimal("10"), desired_weight=Decimal("1"))
        url = reverse("admin:orders_order_change", args=[order.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="items-0-product_name_snapshot"')
        data = {"name": "Подмена", "items-TOTAL_FORMS": 1, "items-INITIAL_FORMS": 1, "items-MIN_NUM_FORMS": 0, "items-MAX_NUM_FORMS": 0, "items-0-id": item.pk, "items-0-order": order.pk, "items-0-product_name_snapshot": "Подмена", "items-0-DELETE": "on"}
        self.assertEqual(self.client.post(url, data).status_code, 403)
        item.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(item.product_name_snapshot, "Исторический товар")
        self.assertEqual(order.name, "Тест")
        self.assertEqual(self.client.post(reverse("admin:orders_order_delete", args=[order.pk]), {"post": "yes"}).status_code, 403)
