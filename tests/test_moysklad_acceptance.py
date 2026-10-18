"""End-to-end acceptance gaps from the MoySklad integration plan.

Run with: `.venv/bin/python manage.py test tests.test_moysklad_acceptance --settings=fishsite.settings_test --noinput`
All provider and mail interactions below use deterministic in-memory doubles.
"""

import hashlib
import json
import threading
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from catalog.models import Category, Product
from content.consent_revisions import get_current_consent_revision
from integrations.models import (
    MoySkladIncident,
    MoySkladProduct,
    MoySkladSyncRun,
    MoySkladSyncState,
    MoySkladTask,
    MoySkladWebhookInbox,
    NotificationOutbox,
)
from integrations.notifications.contracts import DeliveryResult, TemporaryDeliveryError
from integrations.notifications.queue import NotificationQueue
from integrations.notifications.worker import NotificationWorker
from orders.models import Order, OrderItem
from tests.moysklad_helpers import (
    ACCOUNT_ID,
    API_BASE,
    ENVIRONMENT,
    NAMESPACE,
    UNIT_KG_ID,
    moysklad_config,
    product_payload,
)


def _customer_order_document(payload, identifier, sync_id):
    return {
        "id": str(identifier),
        "syncId": str(sync_id),
        "accountId": str(ACCOUNT_ID),
        "rate": payload["rate"],
        "sum": sum(row["price"] * row["quantity"] for row in payload["positions"]),
        "vatEnabled": False,
        "positions": [
            {
                key: row[key]
                for key in ("assortment", "quantity", "price", "discount", "reserve", "vat", "vatEnabled")
            }
            for row in payload["positions"]
        ],
    }


class _InMemoryMailBackend:
    backend_name = "acceptance-memory"

    def __init__(self, *, temporary_failures=0):
        self.temporary_failures = temporary_failures
        self.calls = []

    def send(self, notification, *, delivery_key):
        self.calls.append((notification, delivery_key))
        if len(self.calls) <= self.temporary_failures:
            raise TemporaryDeliveryError("smtp_busy")
        return DeliveryResult()


class _OrderProvider:
    """Provider store survives worker instances while every call stays local."""

    def __init__(self, products, *, fail_posts=False, documents=None):
        self.products = products
        self.fail_posts = fail_posts
        self.documents = documents if documents is not None else {}
        self.get_calls = []
        self.post_calls = []

    def get(self, path, *, params=None):
        self.get_calls.append((path, params))
        if path.startswith("entity/product/"):
            identifier = str(uuid.UUID(path.rsplit("/", 1)[1]))
            return self.products[identifier]
        if path == "entity/customerorder":
            query = (params or {}).get("filter", "")
            sync_id = query.removeprefix("syncId=").split(";", 1)[0]
            document = self.documents.get(sync_id)
            rows = [document] if document is not None else []
            return {"rows": rows, "meta": {"size": len(rows)}}
        raise AssertionError(f"Unexpected provider read: {path}")

    def post(self, path, *, json):
        if path != "entity/customerorder":
            raise AssertionError(f"Unexpected provider write: {path}")
        self.post_calls.append((path, json))
        if self.fail_posts:
            from integrations.moysklad.contracts import MoySkladError

            raise MoySkladError("network_error", retryable=True)
        sync_id = json["syncId"]
        document = _customer_order_document(json, uuid.uuid4(), sync_id)
        self.documents[sync_id] = document
        return document

    def close(self):
        pass


@override_settings(MOYSKLAD=moysklad_config())
class MoySkladPlanAcceptanceTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.category = Category.objects.create(
            name="Проверка МойСклад", slug=f"moysklad-acceptance-{uuid.uuid4().hex}",
        )
        self.owner = get_user_model().objects.create_superuser(
            username=f"moysklad-owner-{uuid.uuid4().hex}",
            email=f"{uuid.uuid4().hex}@example.test",
            password="test-password",
        )
        self.revision = get_current_consent_revision()
        now = timezone.now()
        self.state = MoySkladSyncState.objects.create(
            account_id=ACCOUNT_ID,
            environment=ENVIRONMENT,
            namespace=NAMESPACE,
            export_started_at=now - timedelta(days=1),
            export_consent_revision=self.revision,
            heartbeat_at=now,
            prices_enabled_at=now,
            prices_next_at=now + timedelta(days=1),
            status_next_at=now + timedelta(days=1),
            prices_enabled=True,
            catalog_enabled=False,
            export_enabled=False,
            statuses_enabled=False,
        )

    def set_stream(self, name, enabled):
        setattr(self.state, f"{name}_enabled", enabled)
        self.state.save(update_fields=(f"{name}_enabled", "updated_at"))

    def make_linked_product(self, *, external_id=None, name="Сайтная карточка", price="90.00"):
        from integrations.moysklad.catalog_sync import link_product, upsert_product
        from integrations.moysklad.price_sync import process_price_task
        from integrations.moysklad.queue import claim_task

        external_id = external_id or uuid.uuid4()
        product = Product.objects.create(
            name=name,
            slug=f"acceptance-{uuid.uuid4().hex}",
            category=self.category,
            price_per_kg=Decimal(price),
            is_published=True,
        )
        payload = product_payload(external_id, name=f"Учётный {name}", price=price)
        source = upsert_product(payload)
        link_product(product_id=product.pk, external_product_id=external_id, actor=self.owner)
        task = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("price",))
        self.assertIsNotNone(task)
        process_price_task(task, client=Mock(get=Mock(return_value=payload)))
        source.refresh_from_db()
        product.refresh_from_db()
        return source, product

    def make_order(self, product, source, *, email=None, weight="2.000"):
        order = Order.objects.create(
            name="Покупатель acceptance",
            phone="+7 900 111-22-33",
            email=email or f"buyer-{uuid.uuid4().hex}@example.test",
            fulfillment_method=Order.FulfillmentMethod.DELIVERY,
            delivery_address="Калининград",
            comment="Сохранённый комментарий заявки",
            privacy_consent=True,
            privacy_consent_at=timezone.now(),
            privacy_consent_revision=self.revision,
        )
        price = product.price_per_kg
        item = OrderItem.objects.create(
            order=order,
            product=product,
            product_name_snapshot=product.name,
            price_per_kg_snapshot=price,
            desired_weight=Decimal(weight),
            line_total=price * Decimal(weight),
            external_account_id_snapshot=ACCOUNT_ID,
            external_entity_type_snapshot="product",
            external_id_snapshot=source.external_id,
            unit_id_snapshot=UNIT_KG_ID,
            price_revision_snapshot=product.price_revision,
            price_source_snapshot=product.price_source,
            price_checked_at_snapshot=source.price_checked_at,
        )
        return order, item

    def accepted_snapshot(self, order):
        order.refresh_from_db()
        fields = (
            "product_name_snapshot", "price_per_kg_snapshot", "desired_weight", "line_total",
            "external_account_id_snapshot", "external_entity_type_snapshot", "external_id_snapshot",
            "unit_id_snapshot", "price_revision_snapshot", "price_source_snapshot", "price_checked_at_snapshot",
        )
        return (
            order.name, order.phone, order.email, order.delivery_address, order.comment,
            order.privacy_consent_revision_id, tuple(order.items.order_by("pk").values_list(*fields)),
        )

    def make_external_order_link(self, order, *, external_id=None):
        from integrations.moysklad.order_export import enqueue_order_export
        from integrations.moysklad.queue import claim_task, complete_task

        link = enqueue_order_export(order)
        task = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("export_order",))
        self.assertEqual(task.order_link_id, link.pk)
        complete_task(task)
        link.external_id = external_id or uuid.uuid4()
        link.save(update_fields=("external_id", "updated_at"))
        return link

    def test_same_status_uuid_rename_and_unknown_metadata_remain_active_without_changing_order(self):
        from integrations.moysklad.queue import claim_task
        from integrations.moysklad.status_sync import process_status_task, schedule_status_tasks

        self.set_stream("statuses", True)
        source, product = self.make_linked_product()
        order, item = self.make_order(product, source)
        link = self.make_external_order_link(order)
        original = self.accepted_snapshot(order)
        state_id = uuid.uuid4()
        link.state_id, link.state_name, link.state_type = state_id, "Старое название", "Regular"
        link.status_checked_at = timezone.now() - timedelta(minutes=6)
        link.save(update_fields=("state_id", "state_name", "state_type", "status_checked_at", "updated_at"))

        self.assertEqual(schedule_status_tasks(now=timezone.now()), 1)
        task = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("status_sync",))
        client = Mock(get=Mock(side_effect=[
            {"id": str(link.external_id), "state": {"id": str(state_id)}},
            {"states": [{"id": str(state_id), "name": "Согласование", "stateType": {"name": "Custom"}}]},
        ]))
        process_status_task(task, client=client)

        link.refresh_from_db()
        self.assertEqual((link.state_id, link.state_name, link.state_type), (state_id, "Согласование", "Custom"))
        self.assertEqual([call.args[0] for call in client.get.call_args_list], [
            f"entity/customerorder/{link.external_id}", "entity/customerorder/metadata",
        ])
        due = link.status_checked_at + timedelta(minutes=5)
        self.assertEqual(schedule_status_tasks(now=due - timedelta(seconds=1)), 0)
        self.assertEqual(schedule_status_tasks(now=due), 1)

        second = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("status_sync",))
        unknown_id = uuid.uuid4()
        process_status_task(second, client=Mock(get=Mock(side_effect=[
            {"id": str(link.external_id), "state": {"id": str(unknown_id), "name": "Ожидает проверки"}},
            {"states": []},
        ])))
        link.refresh_from_db()
        self.assertEqual((link.state_id, link.state_name, link.state_type),
                         (unknown_id, "Ожидает проверки", ""))
        due = link.status_checked_at + timedelta(minutes=5)
        self.assertEqual(schedule_status_tasks(now=due - timedelta(seconds=1)), 0)
        self.assertEqual(schedule_status_tasks(now=due), 1)
        self.assertEqual(self.accepted_snapshot(order), original)

    def test_actual_worker_schedules_successful_and_unsuccessful_statuses_once_per_day(self):
        from integrations.moysklad.worker import MoySkladWorker

        source, product = self.make_linked_product()
        links = [self.make_external_order_link(self.make_order(product, source)[0]) for _ in range(2)]
        originals = {link.pk: self.accepted_snapshot(link.order) for link in links}
        self.set_stream("statuses", True)
        self.set_stream("prices", False)
        # Queue model defaults retain their imported real-clock callable.
        # Start the simulated scheduler beyond those freshly created due dates.
        fixed_now = [timezone.now() + timedelta(days=1)]
        MoySkladSyncState.objects.filter(pk=self.state.pk).update(status_next_at=None)
        documents = {
            f"entity/customerorder/{link.external_id}": {
                "id": str(link.external_id),
                "state": {"id": str(uuid.uuid4()), "name": state_type, "stateType": {"name": state_type}},
            }
            for link, state_type in zip(links, ("Successful", "Unsuccessful"), strict=True)
        }
        client = Mock(get=Mock(side_effect=lambda path: documents[path]))
        worker = MoySkladWorker(client=client)
        try:
            with patch("django.utils.timezone.now", side_effect=lambda: fixed_now[0]):
                self.assertTrue(worker.run_once())
                self.assertTrue(worker.run_once())
                checked_at = fixed_now[0]
                fixed_now[0] = checked_at + timedelta(minutes=5)
                self.assertFalse(worker.run_once())
                fixed_now[0] = checked_at + timedelta(days=1) - timedelta(seconds=30)
                self.assertFalse(worker.run_once())
                self.assertEqual(client.get.call_count, 2)
                fixed_now[0] = checked_at + timedelta(days=1)
                self.assertTrue(worker.run_once())
                self.assertTrue(worker.run_once())
                self.assertFalse(worker.run_once())
        finally:
            worker.close()
        self.assertEqual(client.get.call_count, 4)
        for link, state_type in zip(links, ("Successful", "Unsuccessful"), strict=True):
            link.refresh_from_db()
            self.assertEqual(link.state_type, state_type)
            self.assertEqual(link.status_checked_at, checked_at + timedelta(days=1))
            self.assertEqual(self.accepted_snapshot(link.order), originals[link.pk])

    def test_status_metadata_failure_preserves_last_known_fields_and_later_recovers(self):
        from integrations.moysklad.contracts import MoySkladError
        from integrations.moysklad.queue import claim_task, enqueue_task
        from integrations.moysklad.status_sync import process_status_task

        self.set_stream("statuses", True)
        source, product = self.make_linked_product()
        order, _item = self.make_order(product, source)
        original = self.accepted_snapshot(order)
        link = self.make_external_order_link(order)
        prior = (uuid.uuid4(), "Не менять до полного ответа", "Regular", timezone.now() - timedelta(hours=1))
        link.state_id, link.state_name, link.state_type, link.status_checked_at = prior
        link.save(update_fields=("state_id", "state_name", "state_type", "status_checked_at", "updated_at"))
        enqueue_task(kind="status_sync", dedupe_key=f"status-failure:{link.pk}", account_id=ACCOUNT_ID,
                     environment=ENVIRONMENT, order_link=link, object_id=str(order.pk))
        task = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("status_sync",))
        client = Mock(get=Mock(side_effect=[
            {"id": str(link.external_id), "state": {"id": str(uuid.uuid4())}},
            MoySkladError("network_error", retryable=True),
        ]))
        process_status_task(task, client=client)

        link.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual((link.state_id, link.state_name, link.state_type, link.status_checked_at), prior)
        self.assertEqual(task.status, MoySkladTask.Status.PENDING)
        self.assertEqual(task.last_error_code, "network_error")
        self.assertEqual(self.accepted_snapshot(order), original)

        MoySkladTask.objects.filter(pk=task.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        retry = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("status_sync",))
        recovered_id = uuid.uuid4()
        process_status_task(retry, client=Mock(get=Mock(side_effect=[
            {"id": str(link.external_id), "state": {"id": str(recovered_id)}},
            {"states": [{"id": str(recovered_id), "name": "В обработке", "stateType": "Regular"}]},
        ])))
        link.refresh_from_db()
        retry.refresh_from_db()
        self.assertEqual((link.state_id, link.state_name, link.state_type), (recovered_id, "В обработке", "Regular"))
        self.assertGreater(link.status_checked_at, prior[3])
        self.assertEqual(retry.status, MoySkladTask.Status.DONE)
        self.assertEqual(self.accepted_snapshot(order), original)

    def test_missing_product_is_blocked_until_complete_manual_restore_and_owner_unblock(self):
        from integrations.moysklad.catalog_sync import process_catalog_task, start_catalog_sync, unblock_product
        from integrations.moysklad.contracts import MoySkladError
        from integrations.moysklad.queue import claim_task

        self.set_stream("catalog", True)
        external_id = uuid.uuid4()
        source, product = self.make_linked_product(external_id=external_id, price="112.50")
        order, item = self.make_order(product, source)
        original_snapshot = self.accepted_snapshot(order)

        run = start_catalog_sync(actor=self.owner)
        task = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("catalog_sync",))
        complete_listing = {"rows": [], "meta": {"size": 0, "limit": 100, "offset": 0}}
        listing_client = Mock(get=Mock(return_value=complete_listing))
        self.assertEqual(process_catalog_task(task, client=listing_client), "pending")
        listing_client.get.assert_called_once_with("entity/product", params={
            "limit": 100, "offset": 0, "filter": "archived=true;archived=false",
        })
        source.refresh_from_db()
        product.refresh_from_db()
        self.assertIsNone(source.deleted_at, "Absence from the full list alone cannot confirm deletion.")
        self.assertEqual(product.integration_block_reason, "")
        missing = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("catalog_sync",))
        self.assertEqual(missing.sync_run_id, run.pk)
        missing_client = Mock(get=Mock(
            side_effect=MoySkladError("not_found", status_code=404),
        ))
        self.assertEqual(process_catalog_task(missing, client=missing_client), MoySkladSyncRun.Status.PARTIAL)
        missing_client.get.assert_called_once_with(f"entity/product/{external_id}")

        source.refresh_from_db()
        product.refresh_from_db()
        self.assertIsNotNone(source.deleted_at)
        self.assertEqual(source.last_error_code, "deleted")
        self.assertEqual(product.integration_block_reason, "deleted")
        self.assertFalse(Product.objects.published().filter(pk=product.pk).exists())
        self.assertEqual(self.accepted_snapshot(order), original_snapshot)

        restored_payload = product_payload(external_id, name="Учётный товар восстановлен", price="124.00")
        restored_run = start_catalog_sync(actor=self.owner)
        restored_task = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("catalog_sync",))
        restored_client = Mock(get=Mock(side_effect=[
            {"rows": [restored_payload], "meta": {"size": 1, "limit": 100, "offset": 0}},
            restored_payload,
        ]))
        self.assertEqual(process_catalog_task(restored_task, client=restored_client), MoySkladSyncRun.Status.SUCCESS)
        source.refresh_from_db()
        product.refresh_from_db()
        self.assertIsNone(source.deleted_at)
        self.assertEqual(source.last_error_code, "")
        self.assertEqual(source.name, "Учётный товар восстановлен")
        self.assertEqual(source.last_checked_price, Decimal("124.00"))
        self.assertEqual(product.price_per_kg, Decimal("124.00"))
        self.assertEqual(product.integration_block_reason, "deleted", "A feed restore must not silently unblock publication.")
        self.assertFalse(Product.objects.published().filter(pk=product.pk).exists())

        unblocked = unblock_product(product_id=product.pk, actor=self.owner)
        product.refresh_from_db()
        order.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(unblocked.integration_block_reason, "")
        self.assertTrue(Product.objects.published().filter(pk=product.pk).exists())
        self.assertEqual(self.accepted_snapshot(order), original_snapshot)

    def test_expired_manual_catalog_run_requires_owner_restart_and_keeps_saved_pages(self):
        from integrations.moysklad.catalog_sync import process_catalog_task, start_catalog_sync
        from integrations.moysklad.queue import claim_task
        from integrations.moysklad.worker import MoySkladWorker

        self.set_stream("catalog", True)
        self.set_stream("prices", False)
        first_id, second_id = uuid.uuid4(), uuid.uuid4()
        first_payload, second_payload = product_payload(first_id), product_payload(second_id)
        old_run = start_catalog_sync(actor=self.owner)
        task = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("catalog_sync",))
        partial_page = {"rows": [first_payload], "meta": {
            "size": 2, "limit": 100, "offset": 0,
            "nextHref": f"{API_BASE}entity/product?limit=100&offset=1",
        }}
        self.assertEqual(process_catalog_task(task, client=Mock(get=Mock(return_value=partial_page))), "pending")
        self.assertTrue(MoySkladProduct.objects.filter(account_id=ACCOUNT_ID, external_id=first_id).exists())

        expired_token = uuid.uuid4()
        MoySkladTask.objects.filter(pk=task.pk).update(
            status=MoySkladTask.Status.PROCESSING,
            lease_token=expired_token,
            lease_expires_at=timezone.now() - timedelta(seconds=1),
        )
        automatic_client = Mock()
        worker = MoySkladWorker(client=automatic_client)
        try:
            self.assertFalse(worker.run_once(), "Automatic work must fail the expired manual run without resuming it.")
        finally:
            worker.close()
        automatic_client.get.assert_not_called()
        task.refresh_from_db()
        old_run.refresh_from_db()
        self.assertEqual(task.status, MoySkladTask.Status.FAILED)
        self.assertEqual(old_run.status, MoySkladSyncRun.Status.PARTIAL)
        self.assertEqual(old_run.processed_count, 1)
        self.assertTrue(MoySkladProduct.objects.filter(account_id=ACCOUNT_ID, external_id=first_id).exists())

        new_run = start_catalog_sync(actor=self.owner)
        self.assertNotEqual(new_run.pk, old_run.pk)
        new_task = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("catalog_sync",))
        full_page = {"rows": [first_payload, second_payload], "meta": {
            "size": 2, "limit": 100, "offset": 0,
        }}
        self.assertEqual(process_catalog_task(new_task, client=Mock(get=Mock(return_value=full_page))),
                         MoySkladSyncRun.Status.SUCCESS)
        old_run.refresh_from_db()
        new_run.refresh_from_db()
        self.assertEqual(old_run.status, MoySkladSyncRun.Status.PARTIAL)
        self.assertEqual(new_run.status, MoySkladSyncRun.Status.SUCCESS)
        self.assertEqual(MoySkladSyncRun.objects.filter(kind="catalog").count(), 2)
        self.assertEqual(MoySkladProduct.objects.filter(account_id=ACCOUNT_ID, entity_type="product").count(), 2)
        self.assertFalse(MoySkladTask.objects.filter(kind="catalog_sync", status__in=("pending", "processing")).exists())
        worker = MoySkladWorker()
        try:
            self.assertFalse(worker.run_once(), "Worker startup must not silently resume or create manual catalog work.")
        finally:
            worker.close()
        self.assertEqual(MoySkladSyncRun.objects.filter(kind="catalog").count(), 2)

    def test_unsupported_entities_and_configured_deferred_uuid_cannot_create_candidates(self):
        from integrations.moysklad.catalog_sync import create_product_from_candidate, upsert_product

        before = Product.objects.count()
        for entity_type in ("variant", "bundle", "service"):
            with self.subTest(entity_type=entity_type):
                identifier = uuid.uuid4()
                payload = product_payload(identifier, name=f"{entity_type} нельзя публиковать")
                payload["meta"] = {
                    "href": f"{API_BASE}entity/{entity_type}/{identifier}",
                    "type": entity_type,
                    "mediaType": "application/json",
                }
                source = upsert_product(payload)
                self.assertEqual(source.last_error_code, "unsupported_type")
                with self.assertRaises(ValidationError):
                    create_product_from_candidate(
                        external_product_id=identifier, category_id=self.category.pk, actor=self.owner,
                    )

        deferred_id = uuid.uuid4()
        with override_settings(MOYSKLAD=moysklad_config() | {"deferred_product_ids": [str(deferred_id)]}):
            deferred = upsert_product(product_payload(deferred_id, price="99.00"))
            self.assertTrue(deferred.is_deferred)
            with self.assertRaises(ValidationError):
                create_product_from_candidate(
                    external_product_id=deferred_id, category_id=self.category.pk, actor=self.owner,
                )
        self.assertEqual(Product.objects.count(), before)
        self.assertFalse(MoySkladProduct.objects.filter(product__isnull=False, external_id=deferred_id).exists())

    def test_notification_worker_monitors_stale_api_heartbeat_once_and_reopens_after_recovery(self):
        fixed_now = [timezone.now() + timedelta(days=1)]
        self.state.heartbeat_at = fixed_now[0] - timedelta(minutes=3)
        self.state.save(update_fields=("heartbeat_at", "updated_at"))
        backend = _InMemoryMailBackend()
        queue = NotificationQueue(now=lambda: fixed_now[0])
        worker = NotificationWorker(backend, queue=queue, now=lambda: fixed_now[0])

        self.assertEqual(worker.run_once(), 1)
        first = MoySkladIncident.objects.get(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, scope="worker", code="heartbeat_stale",
            closed_at__isnull=True,
        )
        self.assertEqual(NotificationOutbox.objects.filter(incident=first).count(), 1)
        self.assertEqual(NotificationOutbox.objects.get(incident=first).status, NotificationOutbox.Status.SENT)
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(worker.run_once(), 0)
        self.assertEqual(MoySkladIncident.objects.filter(scope="worker", code="heartbeat_stale").count(), 1)
        self.assertEqual(NotificationOutbox.objects.filter(incident__scope="worker").count(), 1)

        fixed_now[0] += timedelta(seconds=61)
        MoySkladSyncState.objects.filter(pk=self.state.pk).update(heartbeat_at=fixed_now[0])
        self.assertEqual(worker.run_once(), 0)
        first.refresh_from_db()
        self.assertIsNotNone(first.closed_at)

        fixed_now[0] += timedelta(seconds=61)
        MoySkladSyncState.objects.filter(pk=self.state.pk).update(
            heartbeat_at=fixed_now[0] - timedelta(minutes=3),
        )
        self.assertEqual(worker.run_once(), 1)
        incidents = MoySkladIncident.objects.filter(scope="worker", code="heartbeat_stale").order_by("opened_at")
        self.assertEqual(incidents.count(), 2)
        self.assertIsNotNone(incidents.first().closed_at)
        self.assertIsNone(incidents.last().closed_at)
        self.assertEqual(NotificationOutbox.objects.filter(incident__in=incidents).count(), 2)
        self.assertEqual(NotificationOutbox.objects.filter(incident__in=incidents, status="sent").count(), 2)
        self.assertEqual(len(backend.calls), 2)

    def test_temporary_incident_email_retry_does_not_block_price_worker(self):
        from integrations.moysklad.incidents import open_incident
        from integrations.moysklad.price_sync import request_price
        from integrations.moysklad.worker import MoySkladWorker

        source, product = self.make_linked_product(price="91.00")
        incident = open_incident(
            scope="api", code="temporary_failure", object_ids=[str(source.external_id)],
            account_id=ACCOUNT_ID, environment=ENVIRONMENT,
        )
        fixed_now = [timezone.now()]
        backend = _InMemoryMailBackend(temporary_failures=1)
        queue = NotificationQueue(now=lambda: fixed_now[0], retry_base_seconds=2, retry_max_seconds=5)
        mail_worker = NotificationWorker(backend, queue=queue, now=lambda: fixed_now[0])
        self.assertEqual(mail_worker.run_once(), 1)
        outbox = NotificationOutbox.objects.get(incident=incident)
        self.assertEqual(outbox.status, NotificationOutbox.Status.PENDING)
        self.assertEqual(outbox.attempt_count, 1)

        price_task = request_price(external_product_id=source.external_id, force_apply=True)
        api_client = Mock(get=Mock(return_value=product_payload(source.external_id, price="137.25")))
        api_worker = MoySkladWorker(client=api_client)
        try:
            self.assertTrue(api_worker.run_once())
        finally:
            api_worker.close()
        price_task.refresh_from_db()
        source.refresh_from_db()
        product.refresh_from_db()
        self.assertEqual(price_task.status, MoySkladTask.Status.DONE)
        self.assertEqual(product.price_per_kg, Decimal("137.25"))
        self.assertEqual(outbox.status, NotificationOutbox.Status.PENDING)

        fixed_now[0] += timedelta(seconds=3)
        self.assertEqual(mail_worker.run_once(), 1)
        outbox.refresh_from_db()
        self.assertEqual(outbox.status, NotificationOutbox.Status.SENT)
        self.assertEqual(outbox.attempt_count, 2)
        self.assertEqual([key for _notification, key in backend.calls], [
            f"moysklad_incident:{incident.pk}", f"moysklad_incident:{incident.pk}",
        ])
        for name in ("api_token", "webhook_secret"):
            self.assertNotIn(moysklad_config()[name], repr(backend.calls))

    def test_multiple_orders_survive_api_outage_and_unbounded_retries_with_frozen_identities(self):
        from catalog.pricing import change_product_price
        from integrations.moysklad.order_export import enqueue_order_export
        from integrations.moysklad.worker import MoySkladWorker

        self.set_stream("export", True)
        source, product = self.make_linked_product(price="102.50")
        self.set_stream("prices", False)
        order_data = [self.make_order(product, source, weight=str(weight)) for weight in (1, 2, 3)]
        links = [enqueue_order_export(order) for order, _item in order_data]
        frozen = {
            link.pk: (link.sync_id, self.accepted_snapshot(link.order))
            for link in links
        }
        products = {str(source.external_id): product_payload(source.external_id, price="102.50")}
        provider = _OrderProvider(products, fail_posts=True)

        def worker_turn():
            worker = MoySkladWorker(client=provider)
            try:
                return worker.run_once()
            finally:
                worker.close()

        tasks = list(MoySkladTask.objects.filter(kind="export_order", order_link__in=links).order_by("created_at"))
        self.assertEqual(len(tasks), 3)
        fixed_now = [timezone.now()]
        with patch("django.utils.timezone.now", side_effect=lambda: fixed_now[0]):
            for attempt in range(1, 9):
                for _ in links:
                    self.assertTrue(worker_turn())
                for task in tasks:
                    task.refresh_from_db()
                    self.assertEqual(task.attempt_count, attempt)
                    self.assertEqual(task.status, MoySkladTask.Status.PENDING)
                    self.assertEqual(task.last_error_code, "network_error")
                self.assertEqual(provider.documents, {})
                fixed_now[0] += timedelta(hours=6)

            prepared = {}
            for link in links:
                link.refresh_from_db()
                prepared[link.pk] = (link.prepared_payload, link.payload_hash)
                self.assertEqual((link.sync_id, self.accepted_snapshot(link.order)), frozen[link.pk])

            changed_product, changed = change_product_price(
                product_id=product.pk, price="999.99", actor=self.owner,
            )
            self.assertTrue(changed)
            self.assertEqual(changed_product.price_per_kg, Decimal("999.99"))
            provider.products[str(source.external_id)] = product_payload(
                source.external_id, price="777.77", updated="2026-10-16 10:00:00.000",
            )
            provider.fail_posts = False
            for _ in links:
                self.assertTrue(worker_turn())
            self.assertFalse(worker_turn(), "Recovery must drain every order without creating another export.")

        external_ids = set()
        for link in links:
            link.refresh_from_db()
            task = MoySkladTask.objects.get(order_link=link)
            self.assertEqual(task.status, MoySkladTask.Status.DONE)
            self.assertEqual(task.attempt_count, 9)
            self.assertEqual((link.sync_id, self.accepted_snapshot(link.order)), frozen[link.pk])
            self.assertEqual((link.prepared_payload, link.payload_hash), prepared[link.pk])
            self.assertIsNotNone(link.external_id)
            external_ids.add(link.external_id)
            item = link.order.items.get()
            calls = [payload for path, payload in provider.post_calls if payload["syncId"] == str(link.sync_id)]
            self.assertEqual(len(calls), 9)
            for payload in calls:
                self.assertEqual(payload, prepared[link.pk][0])
                self.assertEqual(payload["positions"][0]["price"], 10250)
                self.assertEqual(payload["positions"][0]["quantity"], int(item.desired_weight))
                self.assertEqual(payload["positions"][0]["assortment"]["meta"]["href"],
                                 f"{API_BASE}entity/product/{source.external_id}")
            self.assertEqual(provider.documents[str(link.sync_id)]["positions"], prepared[link.pk][0]["positions"])
        self.assertEqual(len(external_ids), 3)
        self.assertEqual(set(provider.documents), {str(link.sync_id) for link in links})
        self.assertEqual(len(provider.documents), 3)
        product.refresh_from_db()
        self.assertEqual(product.price_per_kg, Decimal("999.99"), "Export preflight cannot apply the provider's current price.")

    def test_worker_startup_price_reconciliation_targets_only_mapped_uuids(self):
        from integrations.moysklad.worker import MoySkladWorker

        self.set_stream("catalog", False)
        self.set_stream("prices", True)
        first, first_product = self.make_linked_product(price="90.00")
        second, _second_product = self.make_linked_product(price="91.00")
        candidate_id = uuid.uuid4()
        candidate = MoySkladProduct.objects.create(
            account_id=ACCOUNT_ID, entity_type="product", external_id=candidate_id,
            name="Не сопоставленный кандидат", last_checked_price=Decimal("99.00"), unit_id=UNIT_KG_ID,
        )
        order, item = self.make_order(first_product, first)
        original_snapshot = self.accepted_snapshot(order)
        fixed_now = [timezone.now() + timedelta(days=1)]
        stale = fixed_now[0] - timedelta(minutes=3)
        MoySkladSyncState.objects.filter(pk=self.state.pk).update(
            heartbeat_at=stale,
            prices_reconciled_at=stale - timedelta(days=2),
            prices_next_at=stale + timedelta(days=1),
        )
        responses = {
            str(first.external_id): product_payload(first.external_id, price="132.00"),
            str(second.external_id): product_payload(second.external_id, price="133.00"),
        }

        class ProductReader:
            def __init__(self):
                self.paths = []

            def get(inner_self, path, *, params=None):
                inner_self.paths.append(path)
                return responses[str(uuid.UUID(path.rsplit("/", 1)[1]))]

            def close(inner_self):
                pass

        client = ProductReader()
        worker = MoySkladWorker(client=client)
        try:
            with patch("django.utils.timezone.now", side_effect=lambda: fixed_now[0]):
                self.assertTrue(worker.run_once())
        finally:
            worker.close()

        run = MoySkladSyncRun.objects.get(kind="prices", reason="recovery")
        self.assertEqual(run.status, MoySkladSyncRun.Status.RUNNING)
        self.assertEqual(run.cursor["offset"], 2)
        self.assertCountEqual(run.cursor["targets"], [str(first.external_id), str(second.external_id)])
        saved_cursor = run.cursor
        self.state.refresh_from_db()
        deadline = self.state.prices_next_at
        self.assertEqual(deadline, fixed_now[0] + timedelta(days=1))
        self.assertEqual(client.paths, [], "Scheduling the recovery parent must not read the full catalog.")

        restarted = MoySkladWorker(client=client)
        try:
            with patch("django.utils.timezone.now", side_effect=lambda: fixed_now[0]):
                for _ in range(12):
                    fixed_now[0] += timedelta(seconds=2)
                    worked = restarted.run_once()
                    run.refresh_from_db()
                    self.state.refresh_from_db()
                    self.assertEqual(self.state.prices_next_at, deadline)
                    self.assertEqual(run.cursor, saved_cursor)
                    self.assertEqual(MoySkladSyncRun.objects.filter(kind="prices").count(), 1)
                    if run.status == MoySkladSyncRun.Status.SUCCESS:
                        break
                    self.assertTrue(worked, "The restarted worker must continue the persisted recovery run.")
                self.assertEqual(run.status, MoySkladSyncRun.Status.SUCCESS)
                self.assertFalse(restarted.run_once())
        finally:
            restarted.close()

        run = MoySkladSyncRun.objects.get(kind="prices", reason="recovery")
        self.assertEqual(run.status, MoySkladSyncRun.Status.SUCCESS)
        self.assertEqual((run.total_count, run.processed_count, run.updated_count, run.error_count), (2, 2, 2, 0))
        self.state.refresh_from_db()
        self.assertEqual(self.state.prices_reconciled_at, run.completed_at)
        self.assertCountEqual(client.paths, [f"entity/product/{first.external_id}", f"entity/product/{second.external_id}"])
        self.assertFalse(any(path == "entity/product" for path in client.paths))
        first.refresh_from_db()
        second.refresh_from_db()
        candidate.refresh_from_db()
        first_product.refresh_from_db()
        order.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(first_product.price_per_kg, Decimal("132.00"))
        self.assertEqual(second.product.price_per_kg, Decimal("133.00"))
        self.assertEqual(candidate.price_requested_revision, 0)
        self.assertEqual(self.accepted_snapshot(order), original_snapshot)
        self.assertFalse(MoySkladTask.objects.filter(kind="catalog_sync").exists())
        self.assertFalse(MoySkladSyncRun.objects.filter(kind="catalog").exists())

    def test_webhook_processed_at_price_completion_creates_one_fresh_price_job(self):
        from integrations.moysklad import price_sync
        from integrations.moysklad.contracts import LeaseLost
        from integrations.moysklad.price_sync import process_price_task, request_price
        from integrations.moysklad.queue import claim_task
        from integrations.moysklad.webhooks import process_inbox, receive_webhook

        source, product = self.make_linked_product(price="90.00")
        request_price(external_product_id=source.external_id, force_apply=False)
        claimed_price = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("price",))
        request_id = uuid.uuid4()
        received = threading.Event()
        completed = threading.Event()
        errors = []

        def deliver_and_process_event():
            close_old_connections()
            try:
                receive_webhook(
                    request_id=request_id,
                    events=[{"external_id": str(source.external_id), "updated_fields": ["salePrices"]}],
                )
                received.set()
                inbox_task = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("webhook",))
                self.assertIsNotNone(inbox_task)
                process_inbox(inbox_task)
            except BaseException as exc:
                errors.append(exc)
            finally:
                completed.set()
                connections.close_all()

        thread = None
        original_complete = price_sync.complete_task

        def complete_after_event_arrives(task):
            nonlocal thread
            thread = threading.Thread(target=deliver_and_process_event)
            thread.start()
            self.assertTrue(received.wait(timeout=10), "Webhook must commit while the old price task is finishing.")
            return original_complete(task)

        old_payload = product_payload(source.external_id, price="111.00")
        try:
            with patch("integrations.moysklad.price_sync.complete_task", side_effect=complete_after_event_arrives):
                process_price_task(claimed_price, client=Mock(get=Mock(return_value=old_payload)))
        finally:
            if thread is not None:
                thread.join(timeout=15)
        self.assertIsNotNone(thread)
        self.assertFalse(thread.is_alive(), "Webhook processing must finish after the old price transaction commits.")
        self.assertTrue(completed.is_set())
        if errors:
            raise errors[0]

        source.refresh_from_db()
        inbox = MoySkladWebhookInbox.objects.get(request_id=request_id)
        self.assertIsNotNone(inbox.processed_at)
        self.assertEqual(source.price_requested_revision, source.price_processed_revision + 1)
        pending_prices = MoySkladTask.objects.filter(
            kind="price", external_product=source, status__in=("pending", "processing"),
        )
        self.assertEqual(pending_prices.count(), 1)
        latest_task = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("price",))
        latest = product_payload(source.external_id, price="146.00")
        process_price_task(latest_task, client=Mock(get=Mock(return_value=latest)))
        source.refresh_from_db()
        product.refresh_from_db()
        latest_task.refresh_from_db()
        self.assertEqual(product.price_per_kg, Decimal("146.00"))
        self.assertEqual(source.price_processed_revision, source.price_requested_revision)
        self.assertEqual(latest_task.status, MoySkladTask.Status.DONE)
        self.assertFalse(MoySkladTask.objects.filter(
            kind="price", external_product=source, status__in=("pending", "processing"),
        ).exists())
        stale_client = Mock(get=Mock(return_value=old_payload))
        with self.assertRaises(LeaseLost):
            process_price_task(claimed_price, client=stale_client)
        stale_client.get.assert_not_called()
        product.refresh_from_db()
        self.assertEqual(product.price_per_kg, Decimal("146.00"))

    def test_fresh_worker_recovers_provider_document_from_restored_frozen_state_without_post(self):
        from integrations.moysklad.order_export import _prepare_payload, enqueue_order_export
        from integrations.moysklad.worker import MoySkladWorker

        self.set_stream("export", True)
        source, product = self.make_linked_product(price="103.25")
        order, item = self.make_order(product, source)
        original = self.accepted_snapshot(order)
        link = enqueue_order_export(order)
        payload = _prepare_payload(link)
        digest = hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode()).hexdigest()
        link.prepared_payload = payload
        link.payload_hash = digest
        link.attempted = True
        link.save(update_fields=("prepared_payload", "payload_hash", "attempted", "updated_at"))
        task = MoySkladTask.objects.get(order_link=link)
        expired_token = uuid.uuid4()
        MoySkladTask.objects.filter(pk=task.pk).update(
            status=MoySkladTask.Status.PROCESSING,
            attempt_count=1,
            lease_token=expired_token,
            lease_expires_at=timezone.now() - timedelta(seconds=1),
        )

        external_id = uuid.uuid4()
        remote_document = _customer_order_document(payload, external_id, link.sync_id)
        provider = _OrderProvider({}, documents={str(link.sync_id): remote_document})
        worker = MoySkladWorker(client=provider)
        try:
            self.assertTrue(worker.run_once(), "A fresh worker should claim the restored expired lease.")
        finally:
            worker.close()

        link.refresh_from_db()
        task.refresh_from_db()
        order.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(link.external_id, external_id)
        self.assertEqual(link.sync_id, uuid.UUID(payload["syncId"]))
        self.assertEqual(link.prepared_payload, payload)
        self.assertEqual(link.payload_hash, digest)
        self.assertEqual(task.status, MoySkladTask.Status.DONE)
        self.assertEqual(task.attempt_count, 2)
        self.assertIsNone(task.lease_token)
        self.assertEqual(provider.post_calls, [])
        self.assertEqual([path for path, _params in provider.get_calls], ["entity/customerorder"])
        self.assertEqual(len(provider.documents), 1)
        self.assertEqual(self.accepted_snapshot(order), original)
