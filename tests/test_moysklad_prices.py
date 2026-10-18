"""PHASE B1 acceptance scenarios for MoySklad catalog, prices and webhooks.

Run with:
    .venv/bin/python manage.py test tests.test_moysklad_prices --settings=fishsite.settings_test --noinput

Tests use the project's isolated PostgreSQL profile. The MoySklad API is always
represented by a fake client; no live credentials or external requests are used.
"""

import json
import uuid
from decimal import Decimal
from datetime import timedelta
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from catalog.models import Category, Product, ProductImage
from tests.moysklad_helpers import (
    ACCOUNT_ID, API_BASE, ENVIRONMENT, MOYSKLAD_TEST_CONFIG, UNIT_PIECE_ID, WEBHOOK_SECRET,
    product_payload, webhook_event,
)


def make_category():
    return Category.objects.create(name="Рыба", slug=f"fish-{uuid.uuid4().hex}")


def make_owner():
    return get_user_model().objects.create_superuser(
        username=f"owner-{uuid.uuid4().hex}", email="owner@example.test", password="test-password",
    )


@override_settings(MOYSKLAD=MOYSKLAD_TEST_CONFIG)
class MoySkladCatalogPriceTests(TestCase):
    def setUp(self):
        self.category = make_category()
        self.owner = make_owner()

    # Повторная выгрузка обновляет учётные поля той же внешней записи и не
    # перезаписывает публичное оформление карточки сайта.
    def test_upsert_deduplicates_external_uuid_and_preserves_site_fields(self):
        from integrations.moysklad.catalog_sync import upsert_product

        product = Product.objects.create(
            name="Моя треска", slug=f"my-cod-{uuid.uuid4().hex}", category=self.category,
            short_description="Описание владельца", description="Текст владельца",
            price_per_kg=Decimal("99.00"), is_published=True,
        )
        original_slug = product.slug
        image = ProductImage.objects.create(product=product, image="tests/cod.png", alt_text="Фото владельца")
        external_id = uuid.uuid4()
        first = upsert_product(product_payload(external_id))
        from integrations.moysklad.catalog_sync import link_product
        link_product(product_id=product.pk, external_product_id=external_id, actor=self.owner)
        second = upsert_product(product_payload(external_id, name="Новое учётное имя", code="SKU-2"))

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(type(first).objects.filter(external_id=external_id).count(), 1)
        product.refresh_from_db()
        self.assertEqual((product.name, product.slug, product.category_id), ("Моя треска", original_slug, self.category.pk))
        self.assertEqual((product.short_description, product.description, product.price_per_kg),
                         ("Описание владельца", "Текст владельца", Decimal("99.00")))
        self.assertEqual(product.images.get().pk, image.pk)

    # Подтверждение связи атомарно увеличивает редакцию сопоставления и ставит
    # принудительную проверку цены, сохраняя всю редакционную карточку.
    def test_link_product_preserves_card_and_requests_forced_price(self):
        from integrations.moysklad.catalog_sync import link_product, upsert_product
        from integrations.models import MoySkladTask

        product = Product.objects.create(
            name="Название сайта", slug=f"site-{uuid.uuid4().hex}", category=self.category,
            short_description="Коротко", description="Подробно", price_per_kg=Decimal("90.00"),
            is_published=True, is_popular=True,
        )
        ProductImage.objects.create(product=product, image="tests/site.png", alt_text="Сайт")
        external = upsert_product(product_payload())
        before_revision = getattr(product, "mapping_revision", 0)

        linked = link_product(product_id=product.pk, external_product_id=external.external_id, actor=self.owner)

        product.refresh_from_db()
        self.assertEqual(linked.product_id, product.pk)
        self.assertEqual(product.mapping_revision, before_revision + 1)
        self.assertEqual((product.name, product.slug, product.category_id, product.short_description,
                          product.description, product.price_per_kg, product.is_published, product.is_popular),
                         ("Название сайта", product.slug, self.category.pk, "Коротко", "Подробно",
                          Decimal("90.00"), True, True))
        self.assertEqual(product.images.get().alt_text, "Сайт")
        price_task = MoySkladTask.objects.get(kind="price", object_id=str(external.external_id))
        self.assertTrue(price_task.force_apply)

    # Одна внешняя позиция не может быть связана с двумя карточками, а одна
    # карточка не может получить вторую внешнюю связь.
    def test_link_rejects_duplicate_external_or_site_mapping(self):
        from integrations.moysklad.catalog_sync import link_product, upsert_product

        first = Product.objects.create(name="Первый", slug=f"one-{uuid.uuid4().hex}", category=self.category,
                                       price_per_kg="10.00")
        second = Product.objects.create(name="Второй", slug=f"two-{uuid.uuid4().hex}", category=self.category,
                                        price_per_kg="11.00")
        external = upsert_product(product_payload())
        link_product(product_id=first.pk, external_product_id=external.external_id, actor=self.owner)
        with self.assertRaises((ValidationError, ValueError)):
            link_product(product_id=second.pk, external_product_id=external.external_id, actor=self.owner)
        other_external = upsert_product(product_payload())
        with self.assertRaises((ValidationError, ValueError)):
            link_product(product_id=first.pk, external_product_id=other_external.external_id, actor=self.owner)

    # Позиции с непроверенной или неподходящей единицей остаются кандидатами
    # без связи; коды 00077 и 00126 нельзя автоматически публиковать.
    def test_link_rejects_invalid_unit_and_deferred_start_items(self):
        from integrations.moysklad.catalog_sync import link_product, upsert_product

        for code, unit in (("SKU-OTHER", UNIT_PIECE_ID), ("00077", uuid.uuid4()), ("00126", UNIT_PIECE_ID)):
            product = Product.objects.create(name=code, slug=f"{code}-{uuid.uuid4().hex}", category=self.category,
                                             price_per_kg="10.00")
            from integrations.moysklad.config import get_config
            external = upsert_product(product_payload(name=code, code=code, unit_id=unit), config=get_config())
            with self.subTest(code=code), self.assertRaises((ValidationError, ValueError)):
                link_product(product_id=product.pk, external_product_id=external.external_id, actor=self.owner)

    # Оформление кандидата использует проверенную внешнюю цену, создаёт
    # неопубликованную карточку с уникальным slug и не заменяет занятый slug.
    def test_create_candidate_uses_source_price_and_collision_safe_slug(self):
        from integrations.moysklad.catalog_sync import create_product_from_candidate, upsert_product

        upserted = upsert_product(product_payload(name="Судак", price="234.56"))
        Product.objects.create(name="Судак", slug="sudak", category=self.category, price_per_kg="10.00")

        created = create_product_from_candidate(
            external_product_id=upserted.external_id, category_id=self.category.pk, actor=self.owner,
        )

        self.assertEqual(created.price_per_kg, Decimal("234.56"))
        self.assertFalse(created.is_published)
        self.assertNotEqual(created.slug, "sudak")
        self.assertEqual(created.category_id, self.category.pk)

    # Операции сопоставления и разблокировки требуют права владельца; обычный
    # активный сотрудник не может менять внешнюю идентичность карточек.
    def test_catalog_mutations_require_owner_permission(self):
        from integrations.moysklad.catalog_sync import link_product, upsert_product

        staff = get_user_model().objects.create_user(username=f"staff-{uuid.uuid4().hex}", is_staff=True)
        product = Product.objects.create(name="Товар", slug=f"item-{uuid.uuid4().hex}", category=self.category,
                                         price_per_kg="10.00")
        external = upsert_product(product_payload())
        with self.assertRaises((PermissionDenied, ValidationError)):
            link_product(product_id=product.pk, external_product_id=external.external_id, actor=staff)

    # Заблокированная карточка исключается из публичного queryset и detail URL,
    # сохраняя решение владельца is_published для последующего восстановления.
    def test_blocked_product_is_hidden_from_public_catalog_and_detail(self):
        product = Product.objects.create(name="Архив", slug=f"archived-{uuid.uuid4().hex}", category=self.category,
                                        price_per_kg="10.00", is_published=True)
        if not hasattr(product, "integration_block_reason"):
            self.fail("Product.integration_block_reason is required for blocked catalog entries")
        Product.objects.filter(pk=product.pk).update(integration_block_reason="archived")
        product.refresh_from_db()
        self.assertTrue(product.is_published)
        self.assertFalse(Product.objects.published().filter(pk=product.pk).exists())
        response = Client().get(reverse("catalog:product", args=(product.slug,)))
        self.assertEqual(response.status_code, 404)
        cart_response = Client().post(reverse("orders:cart"), {
            "action": "add", "product_id": str(product.pk), "weight": "1",
        })
        self.assertEqual(cart_response.status_code, 404)

    # Возврат действительного источника снимает только интеграционную
    # блокировку по явному действию владельца, сохраняя is_published.
    def test_unblock_requires_valid_restored_source_and_keeps_manual_publication(self):
        from integrations.moysklad.catalog_sync import link_product, unblock_product, upsert_product

        product = Product.objects.create(name="Архив", slug=f"restored-{uuid.uuid4().hex}", category=self.category,
                                         price_per_kg="10.00", is_published=True)
        external_id = uuid.uuid4()
        external = upsert_product(product_payload(external_id))
        link_product(product_id=product.pk, external_product_id=external.external_id, actor=self.owner)
        upsert_product(product_payload(external_id, archived=True))
        product.refresh_from_db()
        self.assertTrue(product.integration_block_reason)
        upsert_product(product_payload(external_id, archived=False))
        product.refresh_from_db()
        self.assertTrue(product.integration_block_reason, "restoration must not automatically republish the card")
        unblock_product(product_id=product.pk, actor=self.owner)
        product.refresh_from_db()
        self.assertTrue(product.is_published)
        self.assertFalse(product.integration_block_reason)

    # Повторный запрос цены объединяется с активной задачей и сохраняет
    # принудительный флаг; каждая новая причина продвигает request revision.
    def test_price_requests_coalesce_and_force_flag_is_sticky(self):
        from integrations.moysklad.price_sync import request_price
        from integrations.models import MoySkladTask

        external_id = uuid.uuid4()
        source = self._linked_source(external_id)
        task1 = request_price(external_product_id=external_id, force_apply=False)
        source.refresh_from_db()
        requested_revision = source.price_requested_revision
        task2 = request_price(external_product_id=external_id, force_apply=True)
        source.refresh_from_db()
        task1.refresh_from_db()
        self.assertEqual(task1.pk, task2.pk)
        self.assertTrue(task1.force_apply)
        self.assertEqual(source.price_requested_revision, requested_revision + 1)
        self.assertEqual(MoySkladTask.objects.filter(kind="price", object_id=str(external_id),
                                                     status__in=("pending", "processing")).count(), 1)

    # Исполнитель читает товар по фиксированному UUID через клиент и не
    # применяет ответ, если во время HTTP изменились цена сайта или связь.
    def test_price_task_discards_stale_http_response_and_requests_fresh_read(self):
        from catalog.pricing import change_product_price
        from integrations.moysklad.price_sync import process_price_task, request_price
        from integrations.moysklad.queue import claim_task

        external_id = uuid.uuid4()
        source = self._linked_source(external_id)
        initial_client = Mock()
        initial_client.get.return_value = product_payload(external_id, price="125.50")
        process_price_task(claim_task(), client=initial_client)
        change_product_price(product_id=source.product_id, price="111.00", actor=self.owner)
        task = request_price(external_product_id=external_id, force_apply=False)
        task = claim_task()
        source.refresh_from_db()
        previous_checked_price = source.last_checked_price
        previous_checked_at = source.price_checked_at
        previous_processed_revision = source.price_processed_revision
        client = Mock()
        client.get.return_value = product_payload(external_id, price="125.50")

        def edit_during_read(*args, **kwargs):
            change_product_price(product_id=source.product_id, price="112.00", actor=self.owner)
            return client.get.return_value

        client.get.side_effect = edit_during_read
        process_price_task(task, client=client)
        source.refresh_from_db()
        self.assertEqual(source.product.price_per_kg, Decimal("112.00"))
        self.assertEqual(source.last_checked_price, previous_checked_price)
        self.assertEqual(source.price_checked_at, previous_checked_at)
        self.assertEqual(source.price_processed_revision, previous_processed_revision)
        task.refresh_from_db()
        self.assertEqual(task.status, "pending")
        self.assertEqual(client.get.call_count, 1)

        retry = claim_task(kinds=["price"])
        retry_client = Mock()
        retry_client.get.return_value = product_payload(external_id, price="125.50")
        process_price_task(retry, client=retry_client)
        source.product.refresh_from_db()
        self.assertEqual(source.product.price_per_kg, Decimal("112.00"))
        self.assertEqual(retry_client.get.call_args.args[0], f"entity/product/{external_id}")

    # Неизменившаяся внешняя цена сохраняет ручное изменение сайта; изменившаяся
    # выбранная цена применяется, а принудительная сверка заменяет ручную цену.
    def test_selected_price_manual_override_change_and_forced_reconciliation(self):
        from catalog.pricing import change_product_price
        from integrations.moysklad.price_sync import process_price_task, request_price
        from integrations.moysklad.queue import claim_task

        external_id = uuid.uuid4()
        source = self._linked_source(external_id)
        client = Mock()
        client.get.return_value = product_payload(external_id, price="125.50")
        process_price_task(claim_task(), client=client)
        source.refresh_from_db()
        self.assertEqual(source.product.price_per_kg, Decimal("125.50"))

        change_product_price(product_id=source.product_id, price="111.00", actor=self.owner)
        request_price(external_product_id=external_id, force_apply=False)
        process_price_task(claim_task(), client=client)
        source.product.refresh_from_db()
        self.assertEqual(source.product.price_per_kg, Decimal("111.00"))

        client.get.return_value = product_payload(external_id, price="130.25")
        request_price(external_product_id=external_id, force_apply=False)
        process_price_task(claim_task(), client=client)
        source.product.refresh_from_db()
        self.assertEqual(source.product.price_per_kg, Decimal("130.25"))

        change_product_price(product_id=source.product_id, price="112.00", actor=self.owner)
        request_price(external_product_id=external_id, force_apply=True)
        process_price_task(claim_task(), client=client)
        source.product.refresh_from_db()
        self.assertEqual(source.product.price_per_kg, Decimal("130.25"))

    # Неверная валюта и нулевая цена сохраняют действующую цену сайта и
    # отмечают ошибку источника вместо некорректного применения.
    def test_wrong_currency_and_nonpositive_price_preserve_site_price(self):
        from integrations.moysklad.price_sync import process_price_task, request_price
        from integrations.moysklad.queue import claim_task

        external_id = uuid.uuid4()
        source = self._linked_source(external_id)
        Product.objects.filter(pk=source.product_id).update(price_per_kg="87.00")
        invalid_payloads = (
            product_payload(external_id, price="125.50", currency_id=uuid.uuid4()),
            product_payload(external_id, price="-1.00"),
            product_payload(external_id, price="0.00"),
        )
        for payload in invalid_payloads:
            with self.subTest(price=payload["salePrices"][0]["value"]):
                request_price(external_product_id=external_id, force_apply=True)
                client = Mock()
                client.get.return_value = payload
                process_price_task(claim_task(), client=client)
                source.product.refresh_from_db()
                source.refresh_from_db()
                self.assertEqual(source.product.price_per_kg, Decimal("87.00"))
                self.assertTrue(source.last_error_code)

    # Старая редакция внешней записи не перезаписывает уже проверенное более
    # новое значение и не откатывает цену сайта.
    def test_older_source_updated_timestamp_cannot_overwrite_newer_price(self):
        from integrations.moysklad.price_sync import process_price_task, request_price
        from integrations.moysklad.queue import claim_task

        external_id = uuid.uuid4()
        source = self._linked_source(external_id)
        client = Mock()
        client.get.return_value = product_payload(external_id, price="140.00", updated="2026-10-15 10:00:00.000")
        process_price_task(claim_task(), client=client)
        source.refresh_from_db()
        checked_source_time = source.source_updated_at
        client.get.return_value = product_payload(external_id, price="80.00", updated="2026-10-14 10:00:00.000")
        request_price(external_product_id=external_id, force_apply=True)
        process_price_task(claim_task(), client=client)
        source.refresh_from_db()
        source.product.refresh_from_db()
        self.assertEqual(source.source_updated_at, checked_source_time)
        self.assertEqual(source.product.price_per_kg, Decimal("140.00"))

    # Новое событие во время GET продвигает request revision и force_apply;
    # завершение старого ответа оставляет следующую проверку ожидающей.
    def test_event_during_get_preserves_pending_revision_and_force(self):
        from integrations.moysklad.price_sync import process_price_task, request_price
        from integrations.moysklad.queue import claim_task

        external_id = uuid.uuid4()
        source = self._linked_source(external_id)
        request_price(external_product_id=external_id, force_apply=False)
        claimed = claim_task()
        source.refresh_from_db()
        claimed_revision = source.price_requested_revision
        client = Mock()

        def add_newer_request(*args, **kwargs):
            request_price(external_product_id=external_id, force_apply=True)
            return product_payload(external_id, price="125.50")

        client.get.side_effect = add_newer_request
        process_price_task(claimed, client=client)
        source.refresh_from_db()
        claimed.refresh_from_db()
        self.assertEqual(source.price_requested_revision, claimed_revision + 1)
        self.assertGreater(source.price_requested_revision, source.price_processed_revision)
        self.assertTrue(claimed.force_apply)

    # API-путь всегда содержит UUID проверенной внешней позиции, а не href,
    # полученный в запросе или в ответе API.
    def test_price_task_uses_fixed_uuid_resource_path(self):
        from integrations.moysklad.price_sync import process_price_task, request_price

        external_id = uuid.uuid4()
        self._linked_source(external_id)
        request_price(external_product_id=external_id, force_apply=False)
        from integrations.moysklad.queue import claim_task
        task = claim_task()
        client = Mock()
        client.get.return_value = product_payload(external_id)
        process_price_task(task, client=client)
        self.assertEqual(client.get.call_args.args[0], f"entity/product/{external_id}")

    # Два одновременных запуска полного каталога возвращают один ручной run и
    # одну активную задачу, чтобы повторное нажатие не дублировало обход.
    def test_catalog_sync_start_deduplicates_active_manual_run(self):
        from integrations.models import MoySkladSyncRun, MoySkladTask
        from integrations.moysklad.catalog_sync import start_catalog_sync

        first = start_catalog_sync(actor=self.owner)
        second = start_catalog_sync(actor=self.owner)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.kind, MoySkladSyncRun.Kind.CATALOG)
        self.assertEqual(first.status, MoySkladSyncRun.Status.RUNNING)
        self.assertEqual(MoySkladTask.objects.filter(sync_run=first, kind="catalog_sync", status="pending").count(), 1)

    # Ошибка следующей страницы не подтверждает глобальное исчезновение:
    # прежняя внешняя запись остаётся активной, а ручной run завершается ошибкой.
    def test_incomplete_catalog_pagination_does_not_mark_missing_products_deleted(self):
        from integrations.moysklad.catalog_sync import process_catalog_task, start_catalog_sync, upsert_product
        from integrations.models import MoySkladSyncRun, MoySkladTask
        from integrations.moysklad.queue import claim_task

        missing_id = uuid.uuid4()
        previous = upsert_product(product_payload(missing_id, name="Есть в прошлой сверке"))
        run = start_catalog_sync(actor=self.owner)
        task = claim_task(kinds=["catalog_sync"])
        self.assertEqual(task.sync_run_id, run.pk)
        page_item = uuid.uuid4()
        client = Mock()
        client.get.side_effect = [
            {"rows": [product_payload(page_item)], "meta": {
                "size": 2, "limit": 100, "offset": 0,
                "nextHref": f"{API_BASE}entity/product?limit=100&offset=1",
            }},
            TimeoutError("temporary read failure"),
        ]

        self.assertEqual(process_catalog_task(task, client=client), "pending")
        previous.refresh_from_db()
        self.assertIsNone(previous.deleted_at)
        run.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(run.status, MoySkladSyncRun.Status.RUNNING)
        self.assertEqual(task.status, MoySkladTask.Status.PENDING)
        self.assertEqual(run.cursor["offset"], 1)
        self.assertEqual(client.get.call_count, 1)

        task.next_attempt_at = timezone.now() - timedelta(seconds=1)
        task.save(update_fields=("next_attempt_at",))
        next_page_task = claim_task(kinds=["catalog_sync"])
        self.assertEqual(next_page_task.pk, task.pk)
        self.assertEqual(process_catalog_task(next_page_task, client=client), "failed")
        previous.refresh_from_db()
        self.assertIsNone(previous.deleted_at)
        run.refresh_from_db()
        task.refresh_from_db()
        self.assertIn(run.status, (MoySkladSyncRun.Status.FAILED, MoySkladSyncRun.Status.PARTIAL))
        self.assertEqual(task.status, MoySkladTask.Status.FAILED)
        self.assertEqual(client.get.call_count, 2)

    # Восстановительные сигналы цен объединяются в один активный проход,
    # включая суточный сигнал, пока текущая сверка не завершилась.
    def test_price_reconciliation_recovery_and_schedule_coalesce(self):
        from integrations.moysklad.price_sync import start_price_reconciliation
        from integrations.models import MoySkladSyncRun, MoySkladTask

        recovery = start_price_reconciliation(reason="recovery", actor=self.owner)
        scheduled = start_price_reconciliation(reason="scheduled", actor=self.owner)
        self.assertEqual(recovery.pk, scheduled.pk)
        self.assertEqual(recovery.kind, MoySkladSyncRun.Kind.PRICES)
        self.assertEqual(recovery.status, MoySkladSyncRun.Status.RUNNING)
        self.assertEqual(
            MoySkladTask.objects.filter(sync_run=recovery, kind="price_reconciliation", status="pending").count(), 1,
        )

    # Автоматическая сверка ставит принудительные задачи только связанным
    # UUID; новый несвязанный кандидат не попадает в очередь или целевые чтения.
    def test_price_reconciliation_targets_mapped_products_only_and_forces_price(self):
        from integrations.moysklad.price_sync import process_price_reconciliation, request_price, start_price_reconciliation
        from integrations.moysklad.queue import claim_task

        mapped_id, candidate_id = uuid.uuid4(), uuid.uuid4()
        mapped = self._linked_source(mapped_id)
        from integrations.moysklad.catalog_sync import upsert_product
        candidate = upsert_product(product_payload(candidate_id, name="Новый кандидат"))
        mapped.refresh_from_db()
        requested_revision = mapped.price_requested_revision
        candidate_revision = candidate.price_requested_revision
        run = start_price_reconciliation(reason="recovery")
        task = claim_task(kinds=["price_reconciliation"])
        process_price_reconciliation(task, client=Mock())

        mapped.refresh_from_db()
        candidate.refresh_from_db()
        self.assertEqual(mapped.price_requested_revision, requested_revision + 1)
        self.assertEqual(candidate.price_requested_revision, candidate_revision)
        mapped_task = mapped.tasks.get(kind="price", status__in=("pending", "processing"))
        self.assertTrue(mapped_task.force_apply)
        self.assertFalse(candidate.tasks.filter(kind="price").exists())
        run.refresh_from_db()
        self.assertNotEqual(run.status, "success", "a run waits for the requested price revision to finish")

    # После перезапуска проход продолжает сохранённые цели, не создавая
    # повторные принудительные запросы для уже обработанной позиции.
    def test_price_reconciliation_resumes_after_partial_progress(self):
        from integrations.moysklad.price_sync import process_price_reconciliation, start_price_reconciliation
        from integrations.moysklad.queue import claim_task
        from integrations.models import MoySkladTask

        first_id, second_id = uuid.uuid4(), uuid.uuid4()
        first = self._linked_source(first_id)
        second = self._linked_source(second_id)
        run = start_price_reconciliation(reason="recovery")
        task = claim_task(kinds=["price_reconciliation"])
        client = Mock()
        process_price_reconciliation(task, client=client)

        task.refresh_from_db()
        run.refresh_from_db()
        self.assertTrue(run.cursor)
        self.assertNotEqual(task.status, MoySkladTask.Status.DONE)
        self.assertEqual(run.status, "running")
        first.refresh_from_db()
        second.refresh_from_db()
        requested = {
            str(first.external_id): first.price_requested_revision,
            str(second.external_id): second.price_requested_revision,
        }
        if task.status == MoySkladTask.Status.PROCESSING:
            MoySkladTask.objects.filter(pk=task.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
        else:
            MoySkladTask.objects.filter(pk=task.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        resumed = claim_task(kinds=["price_reconciliation"])
        self.assertEqual(resumed.pk, task.pk)
        process_price_reconciliation(resumed, client=Mock())

        first.refresh_from_db()
        second.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual(first.price_requested_revision, requested[str(first.external_id)])
        self.assertEqual(second.price_requested_revision, requested[str(second.external_id)])
        self.assertTrue(first.tasks.filter(kind="price", force_apply=True).exists())
        self.assertTrue(second.tasks.filter(kind="price", force_apply=True).exists())

    def _linked_source(self, external_id):
        from integrations.moysklad.catalog_sync import link_product, upsert_product

        product = Product.objects.create(name="Товар сайта", slug=f"site-{uuid.uuid4().hex}", category=self.category,
                                         price_per_kg="90.00", is_published=True)
        source = upsert_product(product_payload(external_id))
        link_product(product_id=product.pk, external_product_id=external_id, actor=self.owner)
        source.refresh_from_db()
        return source


@override_settings(MOYSKLAD=MOYSKLAD_TEST_CONFIG)
class MoySkladPriceWebhookTests(TestCase):
    # Приём валидного уведомления сохраняет inbox и ровно одну задачу до 204;
    # тело аудита удаляется, а приёмник не делает сетевой запрос.
    def test_valid_event_persists_inbox_and_task_without_api_call(self):
        from integrations.models import MoySkladTask, MoySkladWebhookInbox

        request_id = uuid.uuid4()
        external_id = uuid.uuid4()
        event = webhook_event(external_id, auditContext={"uid": "private"})
        body = {"events": [event]}
        response = Client().post(
            reverse("integrations:moysklad-price-webhook", kwargs={"secret": WEBHOOK_SECRET}) + f"?requestId={request_id}",
            data=json.dumps(body), content_type="application/json",
        )
        self.assertEqual(response.status_code, 204)
        inbox = MoySkladWebhookInbox.objects.get(request_id=request_id)
        self.assertNotIn("auditContext", inbox.events[0])
        self.assertEqual(MoySkladTask.objects.filter(kind="webhook", object_id=str(inbox.pk)).count(), 1)

    # Тот же requestId и содержимое идемпотентны; другое содержимое становится
    # конфликтом и не создаёт вторую inbox-задачу.
    def test_same_request_id_is_idempotent_and_different_body_conflicts(self):
        from integrations.models import MoySkladTask, MoySkladWebhookInbox

        request_id = uuid.uuid4()
        external_id = uuid.uuid4()
        path = reverse("integrations:moysklad-price-webhook", kwargs={"secret": WEBHOOK_SECRET})
        body = {"events": [webhook_event(external_id)]}
        payload = json.dumps(body)
        self.assertEqual(Client().post(f"{path}?requestId={request_id}", payload,
                                       content_type="application/json").status_code, 204)
        self.assertEqual(Client().post(f"{path}?requestId={request_id}", payload,
                                       content_type="application/json").status_code, 204)
        body["events"][0]["updatedFields"] = ["name"]
        self.assertEqual(Client().post(f"{path}?requestId={request_id}", json.dumps(body),
                                       content_type="application/json").status_code, 409)
        self.assertEqual(MoySkladWebhookInbox.objects.filter(request_id=request_id).count(), 1)
        self.assertEqual(MoySkladTask.objects.filter(kind="webhook").count(), 1)

    # Неизвестный секрет, другой аккаунт, неверный тип события, malformed JSON,
    # превышение 1 МиБ и внешний href не доходят до durable inbox.
    def test_invalid_webhook_requests_are_rejected_without_inbox(self):
        from integrations.models import MoySkladWebhookInbox

        request_id = uuid.uuid4()
        valid_event = webhook_event(uuid.uuid4())
        cases = [
            ("b" * 64, json.dumps({"events": [valid_event]})),
            (WEBHOOK_SECRET, json.dumps({"events": [{**valid_event, "accountId": str(uuid.uuid4())}]})),
            (WEBHOOK_SECRET, json.dumps({"events": [{**valid_event, "action": "DELETE"}]})),
            (WEBHOOK_SECRET, "{"),
            (WEBHOOK_SECRET, '{"events":[],"padding":"' + "x" * (1024 * 1024) + '"}'),
            (WEBHOOK_SECRET, json.dumps({"events": [{**valid_event, "meta": {
                "href": "https://evil.example/entity/product/" + str(uuid.uuid4()), "type": "product",
            }}]})),
        ]
        for secret, body in cases:
            url = reverse("integrations:moysklad-price-webhook", kwargs={"secret": secret}) + f"?requestId={request_id}"
            with self.subTest(secret=secret[:1], body_prefix=body[:20]):
                self.assertNotEqual(Client().post(url, body, content_type="application/json").status_code, 204)
        self.assertFalse(MoySkladWebhookInbox.objects.exists())

    # Уведомления через inbox продвигают запрошенную редакцию для связанного
    # товара, пропускают неизвестный UUID и сохраняются до полного cursor.
    def test_inbox_processing_advances_price_requests_and_skips_unlinked_uuid(self):
        from integrations.moysklad.webhooks import process_inbox
        from integrations.moysklad.queue import claim_task, enqueue_task
        from integrations.models import MoySkladWebhookInbox, MoySkladTask

        linked_id = uuid.uuid4()
        unlinked_id = uuid.uuid4()
        with self.captureOnCommitCallbacks(execute=True):
            inbox = MoySkladWebhookInbox.objects.create(
                account_id=str(ACCOUNT_ID), environment=ENVIRONMENT, request_id=uuid.uuid4(),
                payload_hash="e" * 64,
                events=[
                    {"external_id": str(linked_id), "updated_fields": ["salePrices"]},
                    {"external_id": str(unlinked_id), "updated_fields": ["salePrices"]},
                ],
            )
            enqueue_task(kind="webhook", dedupe_key=f"inbox:{inbox.pk}", account_id=str(ACCOUNT_ID),
                         environment=ENVIRONMENT, object_id=str(inbox.pk))
        source = self._linked_source_for_inbox(linked_id)
        task = claim_task()
        self.assertEqual(task.kind, "webhook")
        original_revision = source.price_requested_revision
        process_inbox(task)
        source.refresh_from_db()
        self.assertEqual(source.price_requested_revision, original_revision + 1)
        self.assertEqual(MoySkladTask.objects.filter(kind="price", object_id=str(linked_id)).count(), 1)
        inbox.refresh_from_db()
        self.assertTrue(inbox.processed_at)

    def _linked_source_for_inbox(self, external_id):
        from integrations.moysklad.catalog_sync import link_product, upsert_product

        category = make_category()
        owner = make_owner()
        product = Product.objects.create(name="Товар вебхука", slug=f"webhook-{uuid.uuid4().hex}", category=category,
                                         price_per_kg="90.00", is_published=True)
        source = upsert_product(product_payload(external_id))
        link_product(product_id=product.pk, external_product_id=external_id, actor=owner)
        source.refresh_from_db()
        return source

    # При database failure endpoint возвращает 503, поэтому отправитель может
    # повторить уведомление после восстановления БД.
    def test_database_failure_is_not_acknowledged(self):
        body = {"events": []}
        url = reverse("integrations:moysklad-price-webhook", kwargs={"secret": WEBHOOK_SECRET}) + f"?requestId={uuid.uuid4()}"
        with patch("integrations.moysklad.webhooks.receive_webhook", side_effect=RuntimeError("db unavailable")):
            response = Client().post(url, json.dumps(body), content_type="application/json")
        self.assertEqual(response.status_code, 503)

    # Worker registration обновляет собственный сохранённый webhook UUID и не
    # меняет чужие регистрации в аккаунте.
    def test_configure_webhook_updates_only_saved_registration(self):
        from django.conf import settings
        from integrations.models import MoySkladSyncState
        from integrations.moysklad.webhooks import configure_webhook

        own_id = uuid.uuid4()
        foreign_id = uuid.uuid4()
        sync_state = MoySkladSyncState.objects.create(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, namespace=MOYSKLAD_TEST_CONFIG["namespace"],
            webhook_id=own_id,
        )
        client = Mock()
        client.get.return_value = {"rows": [
            {"id": str(own_id), "accountId": str(ACCOUNT_ID), "url": "https://fish.example.test/old",
             "entityType": "product", "action": "UPDATE", "diffType": "FIELDS"},
            {"id": str(foreign_id), "accountId": str(ACCOUNT_ID), "url": "https://other.example.test/hook",
             "entityType": "product", "action": "UPDATE", "diffType": "FIELDS"},
        ]}
        configure_webhook(client=client)
        client.put.assert_called_once()
        path, = client.put.call_args.args
        self.assertTrue(path.endswith(str(own_id)))
        payload = client.put.call_args.kwargs["json"]
        self.assertEqual(payload["url"], settings.MOYSKLAD["webhook_url"])
        self.assertEqual(payload["entityType"], "product")
        self.assertEqual(payload["action"], "UPDATE")
        self.assertEqual(payload["diffType"], "FIELDS")
        client.post.assert_not_called()
        self.assertNotIn(str(foreign_id), path)
        sync_state.refresh_from_db()
        self.assertEqual(sync_state.webhook_id, own_id)

    # При пустом сохранённом ID точная регистрация нашего URL повторно
    # используется и сохраняется; вызов не создаёт дубликат и не трогает чужой.
    def test_configure_webhook_reuses_exact_owned_registration(self):
        from django.conf import settings
        from integrations.models import MoySkladSyncState
        from integrations.moysklad.webhooks import configure_webhook

        matching_id = uuid.uuid4()
        foreign_id = uuid.uuid4()
        state = MoySkladSyncState.objects.create(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, namespace=MOYSKLAD_TEST_CONFIG["namespace"],
        )
        client = Mock()
        client.get.return_value = {"rows": [
            {"id": str(matching_id), "accountId": str(ACCOUNT_ID), "url": settings.MOYSKLAD["webhook_url"],
             "entityType": "product", "action": "UPDATE", "diffType": "FIELDS"},
            {"id": str(foreign_id), "accountId": str(ACCOUNT_ID), "url": "https://other.example.test/hook",
             "entityType": "product", "action": "UPDATE", "diffType": "FIELDS"},
        ]}
        configure_webhook(client=client)
        client.post.assert_not_called()
        client.put.assert_not_called()
        state.refresh_from_db()
        self.assertEqual(state.webhook_id, matching_id)
