"""Интегрированные регрессии очереди и общей доступности МойСклад.

Run with: .venv/bin/python manage.py test tests.test_moysklad_integrated --settings=fishsite.settings_test --noinput
"""

import hashlib
import logging
import logging.handlers
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.db.models.query import QuerySet
from django.test import TestCase, override_settings
from django.utils import timezone

from catalog.models import Category, Product
from content.models import ConsentRevision
from integrations.models import (
    MoySkladApiState,
    MoySkladIncident,
    MoySkladOrderLink,
    MoySkladProduct,
    MoySkladSyncRun,
    MoySkladSyncState,
    MoySkladTask,
    NotificationOutbox,
)
from integrations.moysklad.config import get_config
from integrations.moysklad.incidents import (
    close_incident,
    close_recovered_catalog_incidents,
    close_recovered_order_incidents,
    monitor_integration,
    open_incident,
)
from orders.models import Order, OrderItem
from tests.moysklad_helpers import ACCOUNT_ID, ENVIRONMENT, NAMESPACE, moysklad_config, product_payload


@override_settings(MOYSKLAD=moysklad_config())
class MoySkladIntegratedRegressionTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="Тестовая рыба", slug=f"integrated-{uuid.uuid4().hex}")
        self.owner = get_user_model().objects.create_superuser(
            username=f"integrated-{uuid.uuid4().hex}",
            email=f"{uuid.uuid4().hex}@example.test",
            password="test-password",
        )

    def linked_source(self, external_id=None):
        from integrations.moysklad.catalog_sync import link_product, upsert_product

        external_id = external_id or uuid.uuid4()
        product = Product.objects.create(
            name="Цена сайта", slug=f"integrated-product-{uuid.uuid4().hex}",
            category=self.category, price_per_kg=Decimal("87.00"), is_published=True,
        )
        source = upsert_product(product_payload(external_id, price="125.50"))
        link_product(product_id=product.pk, external_product_id=external_id, actor=self.owner)
        source.refresh_from_db()
        return source

    def accepted_export_link(self, external_id, *, revision=None):
        from content.consent_revisions import get_current_consent_revision
        from integrations.moysklad.catalog_sync import link_product, upsert_product
        from integrations.moysklad.controls import start_export
        from integrations.moysklad.order_export import request_order_export

        state = start_export(actor=self.owner)
        if revision is None:
            revision = get_current_consent_revision()
        product = Product.objects.create(
            name="Снимок заявки", slug=f"snapshot-{uuid.uuid4().hex}",
            category=self.category, price_per_kg=Decimal("125.50"), is_published=True,
        )
        source = upsert_product(product_payload(external_id, price="125.50"))
        link_product(product_id=product.pk, external_product_id=source.external_id, actor=self.owner)
        order = Order.objects.create(
            name="Покупатель", phone="+7 900 000-00-00", email="buyer@example.test",
            fulfillment_method=Order.FulfillmentMethod.DELIVERY, delivery_address="Калининград",
            privacy_consent=True, privacy_consent_at=timezone.now(), privacy_consent_revision=revision,
        )
        OrderItem.objects.create(
            order=order, product=product, product_name_snapshot=product.name,
            price_per_kg_snapshot=Decimal("125.50"), desired_weight=Decimal("2"),
            line_total=Decimal("251.00"), external_account_id_snapshot=ACCOUNT_ID,
            external_entity_type_snapshot="product", external_id_snapshot=external_id,
            unit_id_snapshot=uuid.UUID(moysklad_config()["unit_id"]), price_revision_snapshot=1,
            price_source_snapshot="moysklad", price_checked_at_snapshot=timezone.now(),
        )
        link = request_order_export(order.pk, actor=self.owner)
        link.refresh_from_db()
        self.assertEqual(link.config_snapshot["namespace"], str(state.namespace))
        return link, product

    def notification_requests(self, *, include_retail=True, include_wholesale=True):
        from content.consent_revisions import get_current_consent_revision
        from inquiries.models import Inquiry

        revision = get_current_consent_revision()
        outboxes = []
        if include_retail:
            order = Order.objects.create(
                name="Synthetic retail contact",
                phone="+7 900 000-10-10",
                email="retail-monitor@example.test",
                fulfillment_method=Order.FulfillmentMethod.PICKUP,
                privacy_consent=True,
                privacy_consent_at=timezone.now(),
                privacy_consent_revision=revision,
            )
            outboxes.append(NotificationOutbox.objects.create(order=order))
        if include_wholesale:
            inquiry = Inquiry.objects.create(
                contact_name="Synthetic wholesale contact",
                company="Synthetic wholesale company",
                phone="+7 900 000-20-20",
                email="wholesale-monitor@example.test",
                privacy_consent=True,
                privacy_consent_at=timezone.now(),
                privacy_consent_revision=revision,
            )
            outboxes.append(NotificationOutbox.objects.create(inquiry=inquiry))
        return outboxes

    @staticmethod
    def run_worker_capturing_warnings(worker):
        from django.core.exceptions import ImproperlyConfigured

        logger = logging.getLogger("integrations.notifications.worker")
        handler = logging.handlers.BufferingHandler(capacity=32)
        handler.setLevel(logging.WARNING)
        logger.addHandler(handler)
        result = None
        configuration_error = None
        try:
            try:
                result = worker.run_once()
            except ImproperlyConfigured as exc:
                configuration_error = exc
        finally:
            logger.removeHandler(handler)
        return result, configuration_error, tuple(handler.buffer)

    def assert_safe_monitor_warning(self, records, *, account_marker, token_marker, secret_marker):
        warnings = [record for record in records if record.levelno >= logging.WARNING]
        self.assertEqual(len(warnings), 1)
        record = warnings[0]
        self.assertEqual(record.notification_event, "integration_monitor_failure")
        self.assertEqual(record.error_code, "monitor_unavailable")
        self.assertFalse(record.exc_info)
        message = record.getMessage()
        for marker in (account_marker, token_marker, secret_marker):
            self.assertNotIn(marker, message)
        self.assertNotIn("ImproperlyConfigured", message)
        self.assertNotIn("MOYSKLAD.account_id", message)

    @staticmethod
    def broken_moysklad_config():
        account_marker = "BROKEN_ACCOUNT_ID_MUST_NOT_BE_LOGGED"
        token_marker = "synthetic-monitor-token-must-not-be-logged"
        secret_marker = "b" * 64
        config = moysklad_config()
        config.update(
            account_id=account_marker,
            api_token=token_marker,
            webhook_secret=secret_marker,
            webhook_url=(
                "https://fish.example.test/integrations/moysklad/webhooks/prices/"
                f"{secret_marker}/"
            ),
        )
        return config, account_marker, token_marker, secret_marker

    # Тайм-аут цены сохраняет задачу и цену сайта, а уведомление появляется
    # только после локального мониторинга длительного сбоя.
    def test_price_timeout_retries_without_immediate_catalog_incident(self):
        from integrations.moysklad.price_sync import process_price_task, request_price
        from integrations.moysklad.queue import claim_task

        source = self.linked_source()
        task = request_price(external_product_id=source.external_id)
        claimed = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("price",))
        self.assertEqual(claimed.pk, task.pk)

        result = process_price_task(claimed, client=Mock(get=Mock(side_effect=TimeoutError)))

        source.refresh_from_db()
        source.product.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(result, "pending")
        self.assertEqual(task.status, MoySkladTask.Status.PENDING)
        self.assertIsNotNone(task.first_error_at)
        self.assertEqual(source.product.price_per_kg, Decimal("87.00"))
        self.assertFalse(MoySkladIncident.objects.filter(account_id=ACCOUNT_ID).exists())
        self.assertFalse(NotificationOutbox.objects.filter(incident__isnull=False).exists())

    # Ответ 401/403 при чтении цены переводит аккаунт в общую паузу и
    # объединяет ошибку в один access-инцидент без catalog-инцидента.
    def test_price_access_failure_creates_one_account_incident(self):
        from integrations.moysklad.contracts import MoySkladError
        from integrations.moysklad.price_sync import process_price_task, request_price
        from integrations.moysklad.queue import claim_task

        source = self.linked_source()
        task = request_price(external_product_id=source.external_id)
        claimed = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("price",))
        self.assertEqual(claimed.pk, task.pk)
        client = Mock(get=Mock(side_effect=MoySkladError("access_denied", status_code=401)))

        process_price_task(claimed, client=client)

        self.assertTrue(MoySkladApiState.objects.filter(account_id=ACCOUNT_ID).exists())
        state = MoySkladApiState.objects.get(account_id=ACCOUNT_ID)
        task.refresh_from_db()
        self.assertEqual(state.access_error_code, "access_denied")
        self.assertEqual(task.status, MoySkladTask.Status.PENDING)
        self.assertEqual(
            list(MoySkladIncident.objects.filter(account_id=ACCOUNT_ID, closed_at__isnull=True)
                 .values_list("scope", "code")),
            [("access", "access_denied")],
        )
        self.assertEqual(NotificationOutbox.objects.filter(incident__isnull=False).count(), 1)

    # Успешное чтение восстанавливает источник и закрывает его инцидент;
    # новый такой же сбой после восстановления создаёт новое уведомление.
    def test_price_source_recovery_closes_then_reopens_incident(self):
        from integrations.moysklad.price_sync import process_price_task, request_price
        from integrations.moysklad.queue import claim_task

        source = self.linked_source()
        invalid = product_payload(source.external_id, unit_id=uuid.uuid4())
        request_price(external_product_id=source.external_id, force_apply=True)
        first_task = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("price",))
        process_price_task(first_task, client=Mock(get=Mock(return_value=invalid)))
        first = MoySkladIncident.objects.get(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, scope="catalog", code="invalid_unit",
            closed_at__isnull=True,
        )

        request_price(external_product_id=source.external_id, force_apply=True)
        recovery = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("price",))
        process_price_task(recovery, client=Mock(get=Mock(return_value=product_payload(source.external_id))))
        source.refresh_from_db()
        first.refresh_from_db()
        self.assertEqual(source.last_error_code, "")
        self.assertIsNotNone(first.closed_at)

        request_price(external_product_id=source.external_id, force_apply=True)
        second_failure = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("price",))
        process_price_task(second_failure, client=Mock(get=Mock(return_value=invalid)))
        incidents = MoySkladIncident.objects.filter(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, scope="catalog", code="invalid_unit",
        ).order_by("opened_at")
        self.assertEqual(incidents.count(), 2)
        self.assertIsNotNone(incidents.first().closed_at)
        self.assertIsNone(incidents.last().closed_at)
        self.assertEqual(NotificationOutbox.objects.filter(incident__in=incidents).count(), 2)

    # Ошибка ручного обхода каталога сообщает владельцу сразу, даже если
    # сбой случился на первой странице до обработки хотя бы одного товара.
    def test_manual_catalog_timeout_on_first_page_opens_incident(self):
        from integrations.moysklad.catalog_sync import process_catalog_task, start_catalog_sync
        from integrations.moysklad.queue import claim_task

        run = start_catalog_sync(actor=self.owner)
        task = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("catalog_sync",))
        self.assertEqual(task.sync_run_id, run.pk)

        result = process_catalog_task(task, client=Mock(get=Mock(side_effect=TimeoutError)))

        run.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(result, "failed")
        self.assertEqual(task.status, MoySkladTask.Status.FAILED)
        self.assertEqual(run.status, MoySkladSyncRun.Status.FAILED)
        self.assertTrue(MoySkladIncident.objects.filter(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, scope="catalog", code="network_error",
            closed_at__isnull=True,
        ).exists())
        self.assertEqual(NotificationOutbox.objects.filter(incident__isnull=False).count(), 1)

    # Ошибка страницы после уже сохранённой части ручного каталога тоже
    # открывает немедленный инцидент и не теряет подтверждённый прогресс.
    def test_manual_catalog_timeout_after_partial_page_opens_incident(self):
        from integrations.moysklad.catalog_sync import process_catalog_task, start_catalog_sync
        from integrations.moysklad.queue import claim_task

        run = start_catalog_sync(actor=self.owner)
        first = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("catalog_sync",))
        item_id = uuid.uuid4()
        page = {"rows": [product_payload(item_id)], "meta": {
            "size": 2, "limit": 100, "offset": 0,
            "nextHref": f"https://api.moysklad.ru/api/remap/1.2/entity/product?limit=100&offset=1",
        }}
        self.assertEqual(process_catalog_task(first, client=Mock(get=Mock(return_value=page))), "pending")

        second = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("catalog_sync",))
        self.assertEqual(process_catalog_task(second, client=Mock(get=Mock(side_effect=TimeoutError))), "failed")

        run.refresh_from_db()
        self.assertEqual(run.processed_count, 1)
        self.assertEqual(run.status, MoySkladSyncRun.Status.PARTIAL)
        self.assertTrue(MoySkladProduct.objects.filter(account_id=ACCOUNT_ID, external_id=item_id).exists())
        self.assertTrue(MoySkladIncident.objects.filter(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, scope="catalog", code="network_error",
            closed_at__isnull=True,
        ).exists())

    # Полный листинг может пропустить UUID, но успешный индивидуальный GET
    # всё равно должен принудительно применить актуальную цену связанному товару.
    def test_catalog_missing_uuid_probe_reconciles_linked_price(self):
        from catalog.pricing import change_product_price
        from integrations.moysklad.catalog_sync import process_catalog_task, start_catalog_sync
        from integrations.moysklad.queue import claim_task

        source = self.linked_source()
        change_product_price(product_id=source.product_id, price="80.00", actor=self.owner)
        run = start_catalog_sync(actor=self.owner)
        first = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("catalog_sync",))
        empty_listing = {"rows": [], "meta": {"size": 0, "limit": 100, "offset": 0}}
        self.assertEqual(process_catalog_task(first, client=Mock(get=Mock(return_value=empty_listing))), "pending")

        second = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("catalog_sync",))
        self.assertEqual(second.sync_run_id, run.pk)
        client = Mock(get=Mock(return_value=product_payload(source.external_id, price="132.45")))
        process_catalog_task(second, client=client)

        source.product.refresh_from_db()
        self.assertEqual(source.product.price_per_kg, Decimal("132.45"))
        self.assertEqual(client.get.call_count, 1)
        self.assertEqual(client.get.call_args.args[0], f"entity/product/{source.external_id}")

    # Пока GET старого UUID выполняется, менеджер может перепривязать карточку;
    # устаревший ответ не блокирует новую связь и не скрывает товар.
    def test_export_preflight_old_uuid_error_does_not_block_concurrent_remap(self):
        from integrations.moysklad.catalog_sync import remap_product, upsert_product
        from integrations.moysklad.order_export import process_export_task
        from integrations.moysklad.queue import claim_task

        old_external_id, new_external_id = uuid.uuid4(), uuid.uuid4()
        link, product = self.accepted_export_link(old_external_id)
        new_source = upsert_product(product_payload(new_external_id, price="125.50"))
        task = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("export_order",))

        def old_uuid_is_now_archived(path, *args, **kwargs):
            self.assertEqual(path, f"entity/product/{old_external_id}")
            remap_product(product_id=product.pk, external_product_id=new_source.external_id, actor=self.owner)
            return product_payload(old_external_id, archived=True)

        client = Mock(get=Mock(side_effect=old_uuid_is_now_archived))
        process_export_task(task, client=client)

        product.refresh_from_db()
        old_source = MoySkladProduct.objects.get(external_id=old_external_id)
        new_source = MoySkladProduct.objects.get(external_id=new_external_id)
        self.assertEqual(old_source.product_id, None)
        self.assertEqual(new_source.product_id, product.pk)
        self.assertEqual(product.integration_block_reason, "")
        self.assertTrue(Product.objects.published().filter(pk=product.pk).exists())
        self.assertEqual(product.price_per_kg, Decimal("125.50"))
        client.post.assert_not_called()

    # Публичная форма может быть открыта до обновления юридического текста:
    # экспорт сохраняет старую редакцию, но требует ручного решения и не делает HTTP.
    def test_order_with_old_consent_revision_stays_local_after_export_starts(self):
        from content.consent_revisions import get_current_consent_revision
        from integrations.moysklad.controls import start_export
        from integrations.moysklad.order_export import process_export_task
        from integrations.moysklad.queue import claim_task

        state = start_export(actor=self.owner)
        approved_revision = get_current_consent_revision()
        state.refresh_from_db()
        self.assertEqual(getattr(state, "export_consent_revision_id", None), approved_revision.pk)

        text = "Старая редакция, уже показанная в открытой форме"
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        old_revision = ConsentRevision.objects.create(
            revision_id=f"sha256:{digest}", sha256=digest, text=text,
        )
        external_id = uuid.uuid4()
        link, _product = self.accepted_export_link(external_id, revision=old_revision)
        self.assertEqual(link.config_snapshot.get("consent_revision_id"), old_revision.pk)
        self.assertFalse(link.config_snapshot.get("consent_approved", True))
        task = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("export_order",))
        client = Mock(get=Mock(return_value=product_payload(external_id)),
                      post=Mock(return_value={"id": str(uuid.uuid4())}))

        process_export_task(task, client=client)

        task.refresh_from_db()
        self.assertEqual(task.status, MoySkladTask.Status.MANUAL_REVIEW)
        self.assertEqual(task.last_error_code, "consent_revision_not_approved")
        client.get.assert_not_called()
        client.post.assert_not_called()

        from integrations.moysklad.order_export import request_order_retry
        request_order_retry(link.pk, actor=self.owner, reason="Повторная проверка согласия")
        retry = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("export_order",))
        process_export_task(retry, client=client)
        retry.refresh_from_db()
        self.assertEqual(retry.status, MoySkladTask.Status.MANUAL_REVIEW)
        self.assertEqual(retry.last_error_code, "consent_revision_not_approved")
        client.get.assert_not_called()
        client.post.assert_not_called()

    # Успешное восстановление закрывает прежний health-инцидент; повторный
    # долгий сбой после закрытия получает новый incident и отдельную outbox.
    def test_incident_reopens_as_new_notification_after_healthy_recovery(self):
        now = timezone.now()
        state = MoySkladSyncState.objects.create(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, namespace=NAMESPACE,
            prices_enabled=True, export_enabled=False, catalog_enabled=False,
            statuses_enabled=False, heartbeat_at=now,
        )
        old_task = MoySkladTask.objects.create(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, kind="price",
            dedupe_key=f"old-failure:{uuid.uuid4()}", object_id=str(uuid.uuid4()),
            status=MoySkladTask.Status.PENDING, last_error_code="network_error",
            first_error_at=now - timedelta(minutes=20),
        )
        first = open_incident(scope="api", code="temporary_failure", account_id=ACCOUNT_ID,
                              environment=ENVIRONMENT, object_ids=[old_task.pk])
        monitor_integration(now=now)
        first.refresh_from_db()
        self.assertIsNone(first.closed_at)

        MoySkladTask.objects.filter(pk=old_task.pk).update(status=MoySkladTask.Status.DONE, last_error_code="")
        state.heartbeat_at = now
        state.save(update_fields=("heartbeat_at", "updated_at"))
        monitor_integration(now=now + timedelta(minutes=1))
        first.refresh_from_db()
        self.assertIsNotNone(first.closed_at)

        replacement = MoySkladTask.objects.create(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, kind="price",
            dedupe_key=f"new-failure:{uuid.uuid4()}", object_id=str(uuid.uuid4()),
            status=MoySkladTask.Status.PENDING, last_error_code="network_error",
            first_error_at=now - timedelta(minutes=20),
        )
        monitor_integration(now=now + timedelta(minutes=2))
        incidents = MoySkladIncident.objects.filter(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, scope="api", code="temporary_failure",
        ).order_by("opened_at")
        self.assertEqual(incidents.count(), 2)
        self.assertEqual(NotificationOutbox.objects.filter(incident__in=incidents).count(), 2)
        self.assertTrue(incidents.last().object_ids)

    # Ошибка нового UUID, добавленная после health-SELECT, не должна попасть
    # под завершение восстановления старой registry-записи.
    def test_catalog_recovery_does_not_close_incident_extended_after_health_select(self):
        source_a = self.linked_source()
        source_b = self.linked_source()
        incident = open_incident(
            scope="catalog", code="invalid_unit", object_ids=[source_a.external_id],
            account_id=ACCOUNT_ID, environment=ENVIRONMENT,
        )
        original_exists = QuerySet.exists
        injected = False

        def add_concurrent_source_error(queryset):
            nonlocal injected
            healthy = original_exists(queryset)
            if queryset.model is MoySkladProduct and not injected:
                injected = True
                MoySkladProduct.objects.filter(pk=source_b.pk).update(last_error_code="invalid_unit")
                open_incident(
                    scope="catalog", code="invalid_unit", object_ids=[source_b.external_id],
                    account_id=ACCOUNT_ID, environment=ENVIRONMENT,
                )
            return healthy

        with patch.object(QuerySet, "exists", new=add_concurrent_source_error):
            close_recovered_catalog_incidents(get_config())

        incident.refresh_from_db()
        self.assertTrue(injected)
        self.assertIsNone(incident.closed_at)
        self.assertEqual(set(incident.object_ids), {str(source_a.external_id), str(source_b.external_id)})
        self.assertEqual(NotificationOutbox.objects.filter(incident=incident).count(), 1)

    # Ошибка новой заявки, пришедшая после health-SELECT, сохраняет общую
    # order_export-запись открытой и прикрепляет обе заявки к ней.
    def test_order_recovery_does_not_close_incident_extended_after_health_select(self):
        link_a, _product_a = self.accepted_export_link(uuid.uuid4())
        link_b, _product_b = self.accepted_export_link(uuid.uuid4())
        incident = open_incident(
            scope="order_export", code="response_mismatch",
            object_ids=[link_a.order.order_number], account_id=ACCOUNT_ID, environment=ENVIRONMENT,
        )
        original_exists = QuerySet.exists
        injected = False

        def add_concurrent_order_error(queryset):
            nonlocal injected
            healthy = original_exists(queryset)
            if queryset.model is MoySkladOrderLink and not injected:
                injected = True
                MoySkladOrderLink.objects.filter(pk=link_b.pk).update(last_error_code="response_mismatch")
                open_incident(
                    scope="order_export", code="response_mismatch",
                    object_ids=[link_b.order.order_number], account_id=ACCOUNT_ID, environment=ENVIRONMENT,
                )
            return healthy

        with patch.object(QuerySet, "exists", new=add_concurrent_order_error):
            close_recovered_order_incidents(account_id=ACCOUNT_ID, environment=ENVIRONMENT)

        incident.refresh_from_db()
        self.assertTrue(injected)
        self.assertIsNone(incident.closed_at)
        self.assertEqual(set(incident.object_ids), {
            link_a.order.order_number, link_b.order.order_number,
        })
        self.assertEqual(NotificationOutbox.objects.filter(incident=incident).count(), 1)

    # Если инцидент закрыли после поиска, новый opener повторно создаёт открытую
    # запись и второе письмо, сохраняя завершённый инцидент закрытым.
    def test_open_incident_retries_when_found_record_closes_before_lock(self):
        original = open_incident(
            scope="access", code="access_denied", account_id=ACCOUNT_ID, environment=ENVIRONMENT,
        )
        original_get_or_create = QuerySet.get_or_create
        closed_during_lookup = False

        def close_existing_before_lock(queryset, defaults=None, **kwargs):
            nonlocal closed_during_lookup
            result = original_get_or_create(queryset, defaults=defaults, **kwargs)
            incident, created = result
            if queryset.model is MoySkladIncident and not created and not closed_during_lookup:
                closed_during_lookup = True
                close_incident(scope="access", code="access_denied",
                               account_id=ACCOUNT_ID, environment=ENVIRONMENT)
            return result

        with patch.object(QuerySet, "get_or_create", new=close_existing_before_lock):
            open_incident(
                scope="access", code="access_denied", object_ids=["new-observation"],
                account_id=ACCOUNT_ID, environment=ENVIRONMENT,
            )

        original.refresh_from_db()
        incidents = MoySkladIncident.objects.filter(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, scope="access", code="access_denied",
        )
        self.assertTrue(closed_during_lookup)
        self.assertIsNotNone(original.closed_at)
        self.assertEqual(incidents.count(), 2)
        self.assertEqual(incidents.filter(closed_at__isnull=True).count(), 1)
        self.assertEqual(NotificationOutbox.objects.filter(incident__in=incidents).count(), 2)

    # Сбой, зарегистрированный после чтения здорового состояния аккаунта,
    # остаётся открытым после монитора и следующего цикла без второго письма.
    def test_monitor_does_not_close_access_pause_added_after_api_state_read(self):
        state = MoySkladSyncState.objects.create(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, namespace=NAMESPACE,
            prices_enabled=False, export_enabled=False, catalog_enabled=False,
            statuses_enabled=False, heartbeat_at=timezone.now(),
        )
        original_first = QuerySet.first
        injected = False

        def pause_after_read(queryset):
            nonlocal injected
            result = original_first(queryset)
            if queryset.model is MoySkladApiState and not injected:
                injected = True
                from integrations.moysklad.throttling import ApiThrottle

                ApiThrottle().pause_access()
                open_incident(scope="access", code="access_denied",
                              account_id=ACCOUNT_ID, environment=ENVIRONMENT)
            return result

        with patch.object(QuerySet, "first", new=pause_after_read):
            monitor_integration()
        monitor_integration()

        incidents = MoySkladIncident.objects.filter(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, scope="access", code="access_denied",
        )
        self.assertTrue(injected)
        self.assertEqual(incidents.count(), 1)
        self.assertEqual(incidents.filter(closed_at__isnull=True).count(), 1)
        self.assertEqual(NotificationOutbox.objects.filter(incident__in=incidents).count(), 1)
        self.assertEqual(MoySkladApiState.objects.get(account_id=ACCOUNT_ID).access_error_code, "access_denied")

    # Пауза экспорта, добавленная после чтения ApiState, тоже не теряется и
    # не порождает повторное уведомление в следующем monitor-цикле.
    def test_monitor_does_not_close_document_pause_added_after_api_state_read(self):
        MoySkladSyncState.objects.create(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, namespace=NAMESPACE,
            prices_enabled=False, export_enabled=False, catalog_enabled=False,
            statuses_enabled=False, heartbeat_at=timezone.now(),
        )
        original_first = QuerySet.first
        injected = False

        def pause_after_read(queryset):
            nonlocal injected
            result = original_first(queryset)
            if queryset.model is MoySkladApiState and not injected:
                injected = True
                from integrations.moysklad.throttling import ApiThrottle

                ApiThrottle().pause_export()
                open_incident(scope="order_export", code="document_limit",
                              account_id=ACCOUNT_ID, environment=ENVIRONMENT)
            return result

        with patch.object(QuerySet, "first", new=pause_after_read):
            monitor_integration()
        monitor_integration()

        incidents = MoySkladIncident.objects.filter(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT,
            scope="order_export", code="document_limit",
        )
        self.assertTrue(injected)
        self.assertEqual(incidents.count(), 1)
        self.assertEqual(incidents.filter(closed_at__isnull=True).count(), 1)
        self.assertEqual(NotificationOutbox.objects.filter(incident__in=incidents).count(), 1)
        self.assertTrue(MoySkladApiState.objects.get(account_id=ACCOUNT_ID).export_paused)

    # Ошибка, обнаруженная одновременно с успешным readonly probe, должна
    # сохранить новую access-паузу и её incident после завершения старого probe.
    def test_successful_access_probe_does_not_clear_pause_added_during_connection_check(self):
        from integrations.moysklad.queue import claim_task, enqueue_task
        from integrations.moysklad.throttling import ApiThrottle
        from integrations.moysklad.worker import MoySkladWorker

        MoySkladSyncState.objects.create(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, namespace=NAMESPACE,
            prices_enabled=False, export_enabled=False, catalog_enabled=False,
            statuses_enabled=False, heartbeat_at=timezone.now(),
        )
        queued = enqueue_task(kind="check_access", dedupe_key="access", account_id=ACCOUNT_ID,
                              environment=ENVIRONMENT)
        task = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("check_access",))
        self.assertEqual(task.pk, queued.pk)
        worker = MoySkladWorker()

        def connection_check(_client, _config):
            ApiThrottle().pause_access()
            open_incident(scope="access", code="access_denied",
                          account_id=ACCOUNT_ID, environment=ENVIRONMENT)
            return {"code": "connection_verified"}

        with patch("integrations.moysklad.worker.check_connection", side_effect=connection_check), \
                patch("integrations.moysklad.client.MoySkladClient") as client_class:
            client_class.return_value.__enter__.return_value = Mock()
            worker._check_access(task)

        state = MoySkladApiState.objects.get(account_id=ACCOUNT_ID)
        incident = MoySkladIncident.objects.get(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, scope="access", code="access_denied",
        )
        self.assertEqual(state.access_error_code, "access_denied")
        self.assertIsNone(incident.closed_at)
        self.assertEqual(NotificationOutbox.objects.filter(incident=incident).count(), 1)
        client_class.assert_called_once()
        client_class.return_value.__enter__.return_value.get.assert_not_called()

    # Первый запуск цен и устаревший heartbeat дают один восстановительный
    # проход; повторный healthy-цикл его не дублирует и не запускает каталог.
    def test_worker_coalesces_initial_price_recovery_and_never_schedules_catalog(self):
        from integrations.moysklad.worker import MoySkladWorker

        state = MoySkladSyncState.objects.create(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, namespace=NAMESPACE,
            prices_enabled=True, catalog_enabled=True,
            heartbeat_at=timezone.now() - timedelta(minutes=3),
        )
        worker = MoySkladWorker()
        try:
            with patch("integrations.moysklad.worker.process_price_reconciliation") as process_reconciliation:
                self.assertTrue(worker.run_once())
                state.refresh_from_db()
                first_next_at = state.prices_next_at
                self.assertFalse(worker.run_once())

            state.refresh_from_db()
            self.assertEqual(state.prices_next_at, first_next_at)
            self.assertEqual(
                MoySkladSyncRun.objects.filter(account_id=ACCOUNT_ID, environment=ENVIRONMENT,
                                               kind="prices", status="running").count(),
                1,
            )
            self.assertEqual(
                MoySkladTask.objects.filter(account_id=ACCOUNT_ID, environment=ENVIRONMENT,
                                            kind="price_reconciliation").count(),
                1,
            )
            self.assertFalse(MoySkladTask.objects.filter(account_id=ACCOUNT_ID, kind="catalog_sync").exists())
            self.assertEqual(process_reconciliation.call_count, 1)
        finally:
            worker.close()

    # Открытая пауза доступа аккаунта оставляет цену ожидающей: worker не
    # захватывает её и не создаёт HTTP-клиент до отдельной проверки доступа.
    def test_worker_does_not_claim_price_while_account_access_is_paused(self):
        from integrations.moysklad.worker import MoySkladWorker

        now = timezone.now()
        MoySkladSyncState.objects.create(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, namespace=NAMESPACE,
            prices_enabled=True, prices_enabled_at=now, prices_next_at=now + timedelta(hours=1),
            heartbeat_at=now,
        )
        MoySkladApiState.objects.create(account_id=ACCOUNT_ID, access_error_code="access_denied")
        task = MoySkladTask.objects.create(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, kind="price",
            dedupe_key=f"paused-price:{uuid.uuid4()}", object_id=str(uuid.uuid4()),
        )
        worker = MoySkladWorker()
        with patch("integrations.moysklad.client.MoySkladClient") as client_class:
            try:
                self.assertFalse(worker.run_once())
            finally:
                worker.close()

        task.refresh_from_db()
        self.assertEqual(task.status, MoySkladTask.Status.PENDING)
        self.assertEqual(task.attempt_count, 0)
        client_class.assert_not_called()

    # Невалидная конфигурация МойСклад не останавливает отправку розничной и
    # оптовой заявок; ошибка монитора записывается один раз и без её деталей.
    def test_notification_worker_delivers_retail_and_wholesale_when_monitor_config_is_invalid(self):
        from integrations.notifications.contracts import DeliveryResult
        from integrations.notifications.queue import NotificationQueue
        from integrations.notifications.worker import NotificationWorker

        outboxes = self.notification_requests()
        broken, account_marker, token_marker, secret_marker = self.broken_moysklad_config()
        fixed_now = [timezone.now()]
        clock = lambda: fixed_now[0]
        queue = NotificationQueue(now=clock)
        backend = Mock()
        backend.backend_name = "test-in-memory"
        backend.send.return_value = DeliveryResult()
        worker = NotificationWorker(backend, queue=queue, now=clock)

        with patch(
            "integrations.moysklad.incidents.monitor_integration",
            wraps=monitor_integration,
        ) as monitor:
            with override_settings(MOYSKLAD=broken):
                processed, configuration_error, records = self.run_worker_capturing_warnings(worker)

        self.assertIsNone(
            configuration_error,
            "MoySklad monitoring configuration must not abort ordinary outbox delivery.",
        )
        self.assertEqual(processed, 2)
        self.assertEqual(monitor.call_count, 1, "A failed monitor check should be throttled for 60 seconds.")
        self.assert_safe_monitor_warning(
            records,
            account_marker=account_marker,
            token_marker=token_marker,
            secret_marker=secret_marker,
        )
        self.assertCountEqual(
            [call.args[0].kind for call in backend.send.call_args_list],
            ["retail", "wholesale"],
        )
        for outbox in outboxes:
            outbox.refresh_from_db()
            self.assertEqual(outbox.status, NotificationOutbox.Status.SENT)
            self.assertEqual(outbox.attempt_count, 1)

    # Ошибка URL-парсера монитора также не мешает двум обычным письмам и не
    # раскрывает адрес, реквизиты или текст исключения в предупреждении.
    def test_notification_worker_delivers_retail_and_wholesale_when_monitor_url_is_malformed(self):
        from integrations.notifications.contracts import DeliveryResult
        from integrations.notifications.queue import NotificationQueue
        from integrations.notifications.worker import NotificationWorker

        outboxes = self.notification_requests()
        broken = moysklad_config()
        broken.update(
            api_token="synthetic-malformed-url-token-must-not-be-logged",
            webhook_secret="c" * 64,
            webhook_url="https://[invalid",
        )
        fixed_now = timezone.now()
        clock = lambda: fixed_now
        queue = NotificationQueue(now=clock)
        backend = Mock()
        backend.backend_name = "test-in-memory"
        backend.send.return_value = DeliveryResult()
        worker = NotificationWorker(backend, queue=queue, now=clock)

        with patch(
            "integrations.moysklad.incidents.monitor_integration",
            wraps=monitor_integration,
        ) as monitor:
            with override_settings(MOYSKLAD=broken):
                processed, configuration_error, records = self.run_worker_capturing_warnings(worker)

        self.assertIsNone(configuration_error)
        self.assertEqual(processed, 2)
        self.assertEqual(monitor.call_count, 1)
        self.assert_safe_monitor_warning(
            records,
            account_marker=broken["account_id"],
            token_marker=broken["api_token"],
            secret_marker=broken["webhook_secret"],
        )
        for marker in (broken["webhook_url"], "ValueError", "Invalid IPv6 URL"):
            self.assertNotIn(marker, str(records[0].__dict__))
        self.assertCountEqual(
            [call.args[0].kind for call in backend.send.call_args_list],
            ["retail", "wholesale"],
        )
        for outbox in outboxes:
            outbox.refresh_from_db()
            self.assertEqual(outbox.status, NotificationOutbox.Status.SENT)
            self.assertEqual(outbox.attempt_count, 1)

    # После cooldown worker заново запускает реальный монитор с исправленной
    # конфигурацией и продолжает доставку следующего обычного уведомления.
    def test_notification_worker_retries_monitor_after_cooldown_and_keeps_delivering(self):
        from integrations.notifications.contracts import DeliveryResult
        from integrations.notifications.queue import NotificationQueue
        from integrations.notifications.worker import NotificationWorker

        first_outbox = self.notification_requests(include_wholesale=False)[0]
        broken, _account_marker, _token_marker, _secret_marker = self.broken_moysklad_config()
        fixed_now = [timezone.now()]
        clock = lambda: fixed_now[0]
        queue = NotificationQueue(now=clock)
        backend = Mock()
        backend.backend_name = "test-in-memory"
        backend.send.return_value = DeliveryResult()
        worker = NotificationWorker(backend, queue=queue, now=clock)

        with patch(
            "integrations.moysklad.incidents.monitor_integration",
            wraps=monitor_integration,
        ) as monitor:
            with override_settings(MOYSKLAD=broken):
                first_processed, first_error, first_records = self.run_worker_capturing_warnings(worker)

            self.assertIsNone(first_error, "Broken MoySklad configuration must not block the first delivery.")
            self.assertEqual(first_processed, 1)
            self.assert_safe_monitor_warning(
                first_records,
                account_marker="BROKEN_ACCOUNT_ID_MUST_NOT_BE_LOGGED",
                token_marker="synthetic-monitor-token-must-not-be-logged",
                secret_marker="b" * 64,
            )
            first_outbox.refresh_from_db()
            self.assertEqual(first_outbox.status, NotificationOutbox.Status.SENT)

            second_outbox = self.notification_requests(include_retail=False)[0]
            fixed_now[0] += timedelta(seconds=61)
            second_processed, second_error, second_records = self.run_worker_capturing_warnings(worker)

        self.assertIsNone(second_error)
        self.assertEqual(second_processed, 1)
        self.assertEqual(monitor.call_count, 2, "The local monitor should retry after its 60-second cooldown.")
        self.assertEqual(second_records, ())
        second_outbox.refresh_from_db()
        self.assertEqual(second_outbox.status, NotificationOutbox.Status.SENT)
        self.assertEqual(second_outbox.attempt_count, 1)
        self.assertEqual(backend.send.call_count, 2)
