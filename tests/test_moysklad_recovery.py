"""Acceptance coverage for safe MoySklad order and inbox recovery."""

import threading
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, close_old_connections, connection
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from catalog.models import Category, Product
from content.consent_revisions import get_current_consent_revision
from integrations.models import (
    MoySkladOrderLink,
    MoySkladProduct,
    MoySkladSyncState,
    MoySkladTask,
    MoySkladWebhookInbox,
    NotificationOutbox,
)
from tests.moysklad_helpers import (
    ACCOUNT_ID, ENVIRONMENT, NAMESPACE, UNIT_KG_ID, moysklad_config, product_payload,
)


def customer_order_response(payload, identifier, sync_id):
    return {
        "id": str(identifier), "syncId": str(sync_id), "accountId": str(ACCOUNT_ID),
        "rate": payload["rate"],
        "sum": sum(row["price"] * row["quantity"] for row in payload["positions"]),
        "vatEnabled": False,
        "positions": [
            {key: row[key] for key in (
                "assortment", "price", "quantity", "discount", "reserve", "vat", "vatEnabled",
            )}
            for row in payload["positions"]
        ],
    }


@override_settings(MOYSKLAD=moysklad_config())
class MoySkladRecoveryTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="Recovery", slug=f"recovery-{uuid.uuid4().hex}")
        self.owner = get_user_model().objects.create_superuser(
            username=f"recovery-{uuid.uuid4().hex}", email=f"{uuid.uuid4().hex}@example.test",
            password="test-password",
        )
        self.state = MoySkladSyncState.objects.create(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, namespace=NAMESPACE,
            export_enabled=True, catalog_enabled=False, prices_enabled=True,
            statuses_enabled=False, export_started_at=timezone.now() - timedelta(days=1),
            export_consent_revision=get_current_consent_revision(),
        )

    def make_order_link(self, *, external_id=None):
        from integrations.moysklad.catalog_sync import link_product, upsert_product
        from integrations.moysklad.order_export import enqueue_order_export

        external_id = external_id or uuid.uuid4()
        product = Product.objects.create(
            name="Треска", slug=f"cod-{uuid.uuid4().hex}", category=self.category,
            price_per_kg=Decimal("125.50"), is_published=True,
        )
        source = upsert_product(product_payload(external_id, price="125.50"))
        link_product(product_id=product.pk, external_product_id=source.external_id, actor=self.owner)
        order = self.make_order(product, external_id)
        return enqueue_order_export(order), product

    def make_order(self, product, external_id):
        from orders.models import Order, OrderItem

        order = Order.objects.create(
            name="Покупатель", phone="+7 900 000-00-00", email="buyer@example.test",
            fulfillment_method=Order.FulfillmentMethod.DELIVERY, delivery_address="Калининград",
            privacy_consent=True, privacy_consent_at=timezone.now(),
            privacy_consent_revision=get_current_consent_revision(),
        )
        OrderItem.objects.create(
            order=order, product=product, product_name_snapshot=product.name,
            price_per_kg_snapshot=Decimal("125.50"), desired_weight=Decimal("2"),
            line_total=Decimal("251.00"), external_account_id_snapshot=ACCOUNT_ID,
            external_entity_type_snapshot="product", external_id_snapshot=external_id,
            unit_id_snapshot=UNIT_KG_ID, price_revision_snapshot=1,
            price_source_snapshot="moysklad", price_checked_at_snapshot=timezone.now(),
        )
        return order

    def test_resolution_request_is_async_deduplicated_and_requires_owner_reason(self):
        from integrations.moysklad.order_export import enqueue_order_export

        link, _product = self.make_order_link()
        task = MoySkladTask.objects.get(order_link=link)
        link.attempted = True
        link.save(update_fields=("attempted",))
        MoySkladTask.objects.filter(pk=task.pk).update(status=MoySkladTask.Status.MANUAL_REVIEW)
        link.refresh_from_db()
        # Import inside the behavior test so the missing public API is an intentional RED.
        from integrations.moysklad.order_export import request_export_resolution

        with patch("integrations.moysklad.client.MoySkladClient") as client_factory:
            returned = request_export_resolution(link.pk, actor=self.owner, reason="Проверить результат")
            self.assertEqual(returned.pk, task.pk)
            self.assertEqual(enqueue_order_export(link.order).pk, link.pk)
            self.assertEqual(MoySkladTask.objects.filter(order_link=link).count(), 1)
            client_factory.assert_not_called()
        with self.assertRaises(ValidationError):
            request_export_resolution(link.pk, actor=self.owner, reason="   ")
        staff = get_user_model().objects.create_user(
            username=f"staff-{uuid.uuid4().hex}", is_staff=True,
        )
        with self.assertRaises(PermissionDenied):
            request_export_resolution(link.pk, actor=staff, reason="Нет разрешения")
        link.external_id = uuid.uuid4()
        link.save(update_fields=("external_id",))
        with self.assertRaises(ValidationError):
            request_export_resolution(link.pk, actor=self.owner, reason="Уже связано")

    def test_resolution_request_rejects_unattempted_export_without_mutating_link_or_task(self):
        from integrations.moysklad.order_export import request_export_resolution

        link, _product = self.make_order_link()
        task = MoySkladTask.objects.get(order_link=link)
        self.assertFalse(link.attempted)
        self.assertIsNone(link.external_id)
        self.assertEqual(task.status, MoySkladTask.Status.PENDING)
        task_fields = (
            "status", "next_attempt_at", "lease_token", "lease_expires_at",
            "attempt_count", "completed_at", "last_error_code", "first_error_at", "updated_at",
        )
        link_fields = ("attempted", "external_id", "sync_id", "audit_log", "override_payload", "updated_at")
        original_task = tuple(getattr(task, field) for field in task_fields)
        original_link = tuple(getattr(link, field) for field in link_fields)

        with self.assertRaises(ValidationError):
            request_export_resolution(link.pk, actor=self.owner, reason="Передача ещё не начиналась")

        task.refresh_from_db()
        link.refresh_from_db()
        self.assertEqual(tuple(getattr(task, field) for field in task_fields), original_task)
        self.assertEqual(tuple(getattr(link, field) for field in link_fields), original_link)
        self.assertEqual(MoySkladTask.objects.filter(order_link=link).count(), 1)

    def test_negative_sync_lookup_is_required_before_identity_override_and_preserves_snapshot(self):
        from integrations.moysklad.order_export import (
            process_export_task, request_export_resolution, set_export_override,
        )
        from integrations.moysklad.queue import claim_task
        from orders.models import OrderItem

        old_id, new_id = uuid.uuid4(), uuid.uuid4()
        link, product = self.make_order_link(external_id=old_id)
        from integrations.moysklad.catalog_sync import upsert_product
        upsert_product(product_payload(new_id, price="125.50"))
        task = MoySkladTask.objects.get(order_link=link)
        item = link.order.items.get()
        original = (item.price_per_kg_snapshot, item.desired_weight, item.external_id_snapshot)
        task.status = MoySkladTask.Status.MANUAL_REVIEW
        task.save(update_fields=("status",))
        link.attempted = True
        link.save(update_fields=("attempted",))
        override = {str(item.pk): {
            "account_id": str(ACCOUNT_ID), "entity_type": "product",
            "external_id": str(new_id), "unit_id": str(UNIT_KG_ID),
        }}
        with self.assertRaises(ValidationError):
            set_export_override(
                link.pk, override, actor=self.owner, reason="До проверки результата POST",
            )
        request_export_resolution(link.pk, actor=self.owner, reason="Уточнить неопределённый POST")
        claimed = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("export_order",))
        client = Mock(get=Mock(side_effect=lambda path, **kwargs: {"rows": [], "meta": {"size": 0}}
                               if "customerorder" in path else product_payload(old_id)))
        process_export_task(claimed, client=client)
        link.refresh_from_db()
        task.refresh_from_db()
        self.assertFalse(link.attempted)
        self.assertEqual(task.status, MoySkladTask.Status.MANUAL_REVIEW)
        self.assertTrue(any(entry.get("action") == "resolve_export_absent" for entry in link.audit_log))
        set_export_override(
            link.pk, override, actor=self.owner, reason="Исправлена ссылка на товар",
        )
        link.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(link.pk, task.order_link_id)
        self.assertEqual(link.sync_id, task.order_link.sync_id)
        self.assertEqual((item.price_per_kg_snapshot, item.desired_weight, item.external_id_snapshot), original)
        self.assertEqual(link.prepared_payload, {})
        self.assertEqual(link.payload_hash, "")
        claimed = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("export_order",))
        client.get.side_effect = lambda path, **kwargs: product_payload(new_id)
        client.post.side_effect = lambda path, *, json: customer_order_response(
            json, uuid.uuid4(), link.sync_id,
        )
        process_export_task(claimed, client=client)
        link.refresh_from_db()
        claimed.refresh_from_db()
        self.assertEqual(claimed.status, MoySkladTask.Status.DONE)
        self.assertIsNotNone(link.exported_at)
        sent = client.post.call_args.kwargs["json"]
        self.assertEqual(sent["positions"][0]["price"], 12550)
        self.assertEqual(sent["positions"][0]["quantity"], 2)
        self.assertEqual(client.post.call_count, 1)

    def test_found_or_ambiguous_resolution_keeps_attempt_guard(self):
        from integrations.moysklad.order_export import process_export_task, request_export_resolution
        from integrations.moysklad.queue import claim_task

        for rows, size in (([{}], 1), ([], 2)):
            with self.subTest(rows=rows, size=size):
                link, _ = self.make_order_link()
                task = MoySkladTask.objects.get(order_link=link)
                link.attempted = True
                link.save(update_fields=("attempted",))
                request_export_resolution(link.pk, actor=self.owner, reason="Повторно проверить поиск")
                claimed = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("export_order",))
                client = Mock()
                client.get.side_effect = lambda path, **kwargs: (
                    {"rows": rows, "meta": {"size": size}}
                    if "customerorder" in path else product_payload(link.order.items.get().external_id_snapshot)
                )
                process_export_task(claimed, client=client)
                link.refresh_from_db()
                task.refresh_from_db()
                self.assertTrue(link.attempted)
                self.assertIsNone(link.external_id)
                self.assertEqual(task.status, MoySkladTask.Status.MANUAL_REVIEW)
                sync_lookup = next(call for call in client.get.call_args_list if "customerorder" in call.args[0])
                self.assertEqual(
                    sync_lookup.kwargs["params"]["filter"],
                    f"syncId={link.sync_id};isDeleted=true;isDeleted=false",
                )
                with self.assertRaises(ValidationError):
                    from integrations.moysklad.order_export import set_export_override
                    set_export_override(link.pk, {}, actor=self.owner, reason="Пока нельзя")
                client.post.assert_not_called()

    def test_stale_resolution_lookup_cannot_clear_attempt_guard_or_replacement_lease(self):
        from integrations.moysklad.order_export import process_export_task, request_export_resolution
        from integrations.moysklad.queue import claim_task

        link, _product = self.make_order_link()
        link.attempted = True
        link.save(update_fields=("attempted",))
        request_export_resolution(link.pk, actor=self.owner, reason="Проверить прежнюю передачу")
        stale = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("export_order",))
        self.assertIsNotNone(stale)
        link.refresh_from_db()
        original_override = link.override_payload
        original_audit = link.audit_log
        replacement = None

        def reclaim_during_get(path, **kwargs):
            nonlocal replacement
            if "customerorder" not in path:
                return product_payload(link.order.items.get().external_id_snapshot)
            self.assertIsNone(replacement)
            MoySkladTask.objects.filter(pk=stale.pk).update(
                lease_expires_at=timezone.now() - timedelta(seconds=1),
            )
            replacement = claim_task(
                account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("export_order",),
            )
            self.assertIsNotNone(replacement)
            self.assertEqual(replacement.pk, stale.pk)
            self.assertNotEqual(replacement.lease_token, stale.lease_token)
            return {"rows": [], "meta": {"size": 0}}

        client = Mock(get=Mock(side_effect=reclaim_during_get))
        process_export_task(stale, client=client)

        self.assertIsNotNone(replacement)
        link.refresh_from_db()
        current = MoySkladTask.objects.get(pk=stale.pk)
        self.assertTrue(link.attempted)
        self.assertIsNone(link.external_id)
        self.assertIsNone(link.exported_at)
        self.assertEqual(link.override_payload, original_override)
        self.assertEqual(link.audit_log, original_audit)
        self.assertFalse(any(entry.get("action") == "resolve_export_absent" for entry in link.audit_log))
        self.assertEqual(current.status, MoySkladTask.Status.PROCESSING)
        self.assertEqual(current.lease_token, replacement.lease_token)
        self.assertEqual(current.lease_expires_at, replacement.lease_expires_at)
        self.assertEqual(current.attempt_count, replacement.attempt_count)
        client.post.assert_not_called()

    def test_resolution_lookup_recovers_matching_document_after_post_timeout_without_another_post(self):
        from integrations.moysklad.contracts import MoySkladError
        from integrations.moysklad.order_export import (
            process_export_task, request_export_resolution, set_export_override,
        )
        from integrations.moysklad.queue import claim_task

        link, _product = self.make_order_link()
        first = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("export_order",))
        initial = Mock()
        initial.get.side_effect = lambda path, **kwargs: (
            {"rows": [], "meta": {"size": 0}}
            if "customerorder" in path else product_payload(link.order.items.get().external_id_snapshot)
        )
        initial.post.side_effect = MoySkladError("timeout", retryable=True)
        process_export_task(first, client=initial)
        link.refresh_from_db()
        self.assertTrue(link.attempted)
        self.assertIsNone(link.external_id)
        self.assertTrue(link.prepared_payload)
        frozen_payload = link.prepared_payload
        initial.post.assert_called_once()

        request_export_resolution(link.pk, actor=self.owner, reason="Найти результат передачи после тайм-аута")
        claimed = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("export_order",))
        identifier = uuid.uuid4()
        found = customer_order_response(frozen_payload, identifier, link.sync_id)
        client = Mock(get=Mock(return_value={"rows": [found], "meta": {"size": 1}}))
        process_export_task(claimed, client=client)

        link.refresh_from_db()
        claimed.refresh_from_db()
        self.assertEqual(link.external_id, identifier)
        self.assertIsNotNone(link.exported_at)
        self.assertEqual(link.prepared_payload, frozen_payload)
        self.assertEqual(claimed.status, MoySkladTask.Status.DONE)
        self.assertEqual(client.get.call_count, 1)
        self.assertEqual(
            client.get.call_args.kwargs["params"]["filter"],
            f"syncId={link.sync_id};isDeleted=true;isDeleted=false",
        )
        client.post.assert_not_called()
        with self.assertRaises(ValidationError):
            set_export_override(link.pk, {}, actor=self.owner, reason="Связь уже найдена")

    def test_resolution_request_survives_recording_crash_and_empty_lookup_never_posts(self):
        from integrations.moysklad import order_export
        from integrations.moysklad.contracts import MoySkladError
        from integrations.moysklad.order_export import process_export_task, request_export_resolution
        from integrations.moysklad.queue import claim_task

        link, _product = self.make_order_link()
        first = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("export_order",))
        initial = Mock()
        initial.get.side_effect = lambda path, **kwargs: (
            {"rows": [], "meta": {"size": 0}}
            if "customerorder" in path else product_payload(link.order.items.get().external_id_snapshot)
        )
        initial.post.side_effect = MoySkladError("timeout", retryable=True)
        process_export_task(first, client=initial)
        link.refresh_from_db()
        self.assertTrue(link.attempted)
        self.assertTrue(link.prepared_payload)
        frozen_payload, frozen_hash, sync_id = link.prepared_payload, link.payload_hash, link.sync_id
        item = link.order.items.get()
        snapshot_fields = (
            "product_name_snapshot", "price_per_kg_snapshot", "desired_weight", "line_total",
            "external_account_id_snapshot", "external_entity_type_snapshot", "external_id_snapshot",
            "unit_id_snapshot", "price_revision_snapshot", "price_source_snapshot", "price_checked_at_snapshot",
        )
        original_snapshot = tuple(getattr(item, field) for field in snapshot_fields)
        initial.post.assert_called_once()

        request_export_resolution(link.pk, actor=self.owner, reason="Найти результат неопределённого POST")
        claimed = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("export_order",))
        link.refresh_from_db()
        original_override, original_audit = link.override_payload, link.audit_log
        self.assertTrue(original_override.get("resolution_requested"))
        found = customer_order_response(frozen_payload, uuid.uuid4(), sync_id)
        client = Mock(get=Mock(return_value={"rows": [found], "meta": {"size": 1}}))
        with patch.object(order_export, "_record_document", side_effect=DatabaseError("recording crash")):
            with self.assertRaises(DatabaseError):
                process_export_task(claimed, client=client)

        link.refresh_from_db()
        self.assertIsNone(link.external_id)
        self.assertTrue(link.attempted)
        self.assertEqual(link.override_payload, original_override)
        self.assertEqual(link.audit_log, original_audit)
        client.post.assert_not_called()

        MoySkladTask.objects.filter(pk=claimed.pk).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1),
        )
        restarted = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("export_order",))
        self.assertEqual(restarted.pk, claimed.pk)
        self.assertNotEqual(restarted.lease_token, claimed.lease_token)
        later = Mock(get=Mock(return_value={"rows": [], "meta": {"size": 0}}))
        later.post.side_effect = AssertionError("Result resolution must never create a document.")
        process_export_task(restarted, client=later)

        link.refresh_from_db()
        restarted.refresh_from_db()
        item.refresh_from_db()
        self.assertFalse(link.attempted)
        self.assertIsNone(link.external_id)
        self.assertIsNone(link.exported_at)
        self.assertEqual(restarted.status, MoySkladTask.Status.MANUAL_REVIEW)
        self.assertTrue(any(entry.get("action") == "resolve_export_absent" for entry in link.audit_log))
        self.assertEqual(link.sync_id, sync_id)
        self.assertEqual((link.prepared_payload, link.payload_hash), (frozen_payload, frozen_hash))
        self.assertEqual(tuple(getattr(item, field) for field in snapshot_fields), original_snapshot)
        self.assertEqual(MoySkladTask.objects.filter(order_link=link).count(), 1)
        self.assertEqual(later.get.call_count, 1)
        later.post.assert_not_called()

    def test_repair_actions_reject_blank_reason_and_resolution_rejects_foreign_target(self):
        from integrations.moysklad.order_export import (
            request_existing_order_link, request_order_retry, request_export_resolution,
        )

        link, _ = self.make_order_link()
        for operation in (
            lambda: request_order_retry(link.pk, actor=self.owner, reason=" "),
            lambda: request_existing_order_link(link.pk, uuid.uuid4(), actor=self.owner, reason="\t"),
            lambda: request_export_resolution(link.pk, actor=self.owner, reason="\n"),
        ):
            with self.subTest(operation=operation), self.assertRaises(ValidationError):
                operation()
        with override_settings(MOYSKLAD=moysklad_config() | {"account_id": str(uuid.uuid4())}):
            with self.assertRaises(ValidationError):
                request_export_resolution(link.pk, actor=self.owner, reason="Чужой аккаунт")

    def test_sync_recovery_wins_before_requested_candidate(self):
        from integrations.moysklad.order_export import (
            process_export_task, request_existing_order_link,
        )
        from integrations.moysklad.queue import claim_task
        from integrations.moysklad.contracts import MoySkladError

        link, _ = self.make_order_link()
        first = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("export_order",))
        initial = Mock()
        initial.get.side_effect = lambda path, **kwargs: (
            {"rows": [], "meta": {"size": 0}}
            if "customerorder" in path else product_payload(link.order.items.get().external_id_snapshot)
        )
        initial.post.side_effect = MoySkladError("timeout", retryable=True)
        process_export_task(first, client=initial)
        link.refresh_from_db()
        candidate_id = uuid.uuid4()
        request_existing_order_link(link.pk, candidate_id, actor=self.owner, reason="Проверить найденный заказ")
        task = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("export_order",))
        found = customer_order_response(link.prepared_payload, uuid.uuid4(), link.sync_id)
        client = Mock(get=Mock(return_value={"rows": [found], "meta": {"size": 1}}))
        process_export_task(task, client=client)
        link.refresh_from_db()
        self.assertEqual(str(link.external_id), found["id"])
        self.assertNotEqual(str(link.external_id), str(candidate_id))
        self.assertEqual(client.get.call_count, 1)
        client.post.assert_not_called()

    def test_post_then_database_crash_recovers_with_one_remote_document_and_one_post(self):
        from integrations.moysklad.order_export import process_export_task
        from integrations.moysklad.queue import claim_task
        from integrations.models import MoySkladTask
        from catalog.pricing import change_product_price

        link, product = self.make_order_link()
        task = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("export_order",))
        remote = {}

        class FirstClient:
            def get(self, path, params=None):
                if "customerorder" in path:
                    return {"rows": [], "meta": {"size": 0}}
                return product_payload(link.order.items.get().external_id_snapshot, price="125.50")

            def post(self, path, json):
                self.payload = json
                doc = customer_order_response(json, uuid.uuid4(), link.sync_id)
                remote[str(link.sync_id)] = doc
                return doc

        first_client = FirstClient()
        from integrations.moysklad import order_export
        original_record = order_export._record_document
        with patch.object(order_export, "_record_document", side_effect=DatabaseError("crash after provider create")):
            with self.assertRaises(DatabaseError):
                process_export_task(task, client=first_client)
        link.refresh_from_db()
        frozen = link.prepared_payload
        change_product_price(product_id=product.pk, price="999.99", actor=self.owner)
        MoySkladTask.objects.filter(pk=task.pk).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1),
        )
        retry = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("export_order",))

        class RestartedClient:
            post_calls = 0

            def get(self, path, params=None):
                if "customerorder" in path:
                    return {"rows": [remote[str(link.sync_id)]], "meta": {"size": 1}}
                return product_payload(link.order.items.get().external_id_snapshot, price="125.50")

            def post(self, path, json):
                self.post_calls += 1
                raise AssertionError("must recover the existing syncId")

        restarted_client = RestartedClient()
        process_export_task(retry, client=restarted_client)
        link.refresh_from_db()
        self.assertEqual(restarted_client.post_calls, 0)
        self.assertEqual(link.external_id, uuid.UUID(remote[str(link.sync_id)]["id"]))
        self.assertEqual(link.prepared_payload, frozen)
        self.assertEqual(frozen["positions"][0]["price"], 12550)
        self.assertEqual(frozen["positions"][0]["quantity"], 2)
        self.assertEqual(original_record.__name__, "_record_document")


@override_settings(MOYSKLAD=moysklad_config())
class MoySkladInboxRecoveryTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        MoySkladSyncState.objects.create(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, namespace=NAMESPACE,
            prices_enabled=True, export_enabled=False, catalog_enabled=False,
        )
        self.ids = [uuid.uuid4() for _ in range(3)]
        self.category = Category.objects.create(name="Inbox", slug=f"inbox-{uuid.uuid4().hex}")
        for index, identifier in enumerate(self.ids):
            product = Product.objects.create(
                name=f"Inbox product {index}", slug=f"inbox-product-{index}-{uuid.uuid4().hex}",
                category=self.category, price_per_kg=Decimal("80.00"), is_published=True,
            )
            MoySkladProduct.objects.create(
                account_id=ACCOUNT_ID, entity_type="product",
                external_id=identifier, name=f"Продукт {index}", product=product,
            )

    def make_inbox_task(self, events=None, *, account_id=ACCOUNT_ID, environment=ENVIRONMENT):
        inbox = MoySkladWebhookInbox.objects.create(
            account_id=account_id, environment=environment, request_id=uuid.uuid4(),
            payload_hash="f" * 64,
            events=events or [{"external_id": str(i), "updated_fields": ["salePrices"]} for i in self.ids],
        )
        task = MoySkladTask.objects.create(
            account_id=account_id, environment=environment, kind="webhook",
            dedupe_key=f"inbox:{inbox.pk}", object_id=str(inbox.pk), inbox=inbox,
            status=MoySkladTask.Status.PENDING,
        )
        return inbox, task

    def test_partial_batch_restart_uses_durable_cursor_and_rejects_stale_worker(self):
        from integrations.moysklad.queue import claim_task
        from integrations.moysklad.webhooks import process_inbox

        repeated = [{"external_id": str(self.ids[0]), "updated_fields": ["salePrices"]} for _ in range(3)]
        inbox, _ = self.make_inbox_task(events=repeated)
        first = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("webhook",))
        self.assertEqual(process_inbox(first, batch_size=1), "pending")
        inbox.refresh_from_db()
        self.assertEqual(inbox.cursor, 1)
        stale = first
        from integrations.models import MoySkladTask as Task
        replacement = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("webhook",))
        from integrations.moysklad.contracts import LeaseLost
        with self.assertRaises(LeaseLost):
            process_inbox(stale, batch_size=1)
        self.assertEqual(process_inbox(replacement, batch_size=1), "pending")
        replacement = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("webhook",))
        self.assertEqual(process_inbox(replacement, batch_size=1), "done")
        inbox.refresh_from_db()
        self.assertEqual(inbox.cursor, 3)
        self.assertIsNotNone(inbox.processed_at)
        self.assertEqual(MoySkladTask.objects.filter(
            kind="price", external_product__external_id__in=self.ids,
        ).count(), 1)
        source = MoySkladProduct.objects.get(external_id=self.ids[0])
        self.assertEqual(source.price_requested_revision, 3)

    def test_cleanup_removes_only_old_processed_finished_inboxes_in_current_target(self):
        from integrations.moysklad.webhooks import cleanup_processed_inbox

        now = timezone.now()
        old_done, done_task = self.make_inbox_task(events=[])
        recent, _ = self.make_inbox_task(events=[])
        exact_boundary, _ = self.make_inbox_task(events=[])
        unfinished, unfinished_task = self.make_inbox_task(events=[])
        active, active_task = self.make_inbox_task(events=[])
        foreign, _ = self.make_inbox_task(events=[], account_id=uuid.uuid4())
        foreign_environment, _ = self.make_inbox_task(events=[], environment="other")
        for inbox, processed in (
            (old_done, now - timedelta(days=31)),
            (recent, now - timedelta(days=29)),
            (exact_boundary, now - timedelta(days=30)),
            (unfinished, now - timedelta(days=31)),
            (active, now - timedelta(days=31)),
            (foreign, now - timedelta(days=31)),
            (foreign_environment, now - timedelta(days=31)),
        ):
            MoySkladWebhookInbox.objects.filter(pk=inbox.pk).update(processed_at=processed)
        MoySkladTask.objects.filter(pk=done_task.pk).update(status=MoySkladTask.Status.DONE)
        MoySkladTask.objects.filter(pk=unfinished_task.pk).update(status=MoySkladTask.Status.PENDING)
        MoySkladTask.objects.filter(pk=active_task.pk).update(
            status=MoySkladTask.Status.PROCESSING, lease_token=uuid.uuid4(),
            lease_expires_at=now + timedelta(minutes=1),
        )
        removed = cleanup_processed_inbox(now=now)
        self.assertEqual(removed, 1)
        self.assertFalse(MoySkladWebhookInbox.objects.filter(pk=old_done.pk).exists())
        for retained in (recent, exact_boundary, unfinished, active, foreign, foreign_environment):
            self.assertTrue(MoySkladWebhookInbox.objects.filter(pk=retained.pk).exists())

    def test_webhook_enqueue_failure_rolls_back_insert_and_returns_503(self):
        from tests.moysklad_helpers import WEBHOOK_SECRET, webhook_event

        client = Client()
        event = webhook_event(self.ids[0])
        url = f"/integrations/moysklad/webhooks/prices/{WEBHOOK_SECRET}/"
        import json
        with patch("integrations.moysklad.webhooks.enqueue_task", side_effect=RuntimeError("synthetic enqueue failure")):
            response = client.post(
                f"{url}?requestId={uuid.uuid4()}", data=json.dumps({"events": [event]}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 503)
        self.assertFalse(MoySkladWebhookInbox.objects.exists())
        self.assertFalse(MoySkladTask.objects.filter(kind="webhook").exists())


@override_settings(MOYSKLAD=moysklad_config())
class MoySkladRetailAcceptanceRaceTests(TransactionTestCase):
    reset_sequences = True

    def test_concurrent_identical_retail_posts_accept_exactly_one_order_and_export(self):
        """Barrier-start two independent PostgreSQL-backed sessions after export starts."""
        from content.consent_revisions import get_current_consent_revision
        from integrations.moysklad.catalog_sync import link_product, upsert_product
        from integrations.moysklad.controls import start_export
        from orders.cart import CART_SESSION_KEY

        category = Category.objects.create(name="Retail race", slug=f"race-{uuid.uuid4().hex}")
        product = Product.objects.create(
            name="Race fish", slug=f"race-fish-{uuid.uuid4().hex}", category=category,
            price_per_kg=Decimal("125.50"), is_published=True,
        )
        owner = get_user_model().objects.create_superuser(
            username=f"race-owner-{uuid.uuid4().hex}", email=f"{uuid.uuid4().hex}@example.test",
            password="test-password",
        )
        source_id = uuid.uuid4()
        upsert_product(product_payload(source_id, price="125.50"))
        link_product(product_id=product.pk, external_product_id=source_id, actor=owner)
        start_export(actor=owner)
        self.assertEqual(connection.vendor, "postgresql")

        # Each client receives its own signed submission snapshot and session.
        original_client = Client()
        original_client.post(reverse("orders:cart"), {
            "action": "add", "product_id": str(product.pk), "weight": "2",
        })
        response = original_client.get(reverse("orders:cart"))
        same_submission = response.context["submission"].copy()
        snapshots = []
        for _ in range(2):
            client = Client()
            client.cookies = original_client.cookies.copy()
            snapshots.append((client, same_submission.copy()))
        barrier = threading.Barrier(2)
        outcomes, failures = [], []

        def submit(client, snapshot):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                outcomes.append(client.post(reverse("orders:cart"), {
                    "action": "submit", **snapshot, "name": "Race Buyer", "phone": "+7 900 000-00-01",
                    "email": "race@example.test", "pickup_method": "pickup", "consent": "yes",
                }).status_code)
            except Exception as error:  # collected for assertion in the main thread
                failures.append(error)
            finally:
                close_old_connections()

        workers = [threading.Thread(target=submit, args=pair) for pair in snapshots]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=20)
        self.assertFalse(failures, failures)
        self.assertEqual(sorted(outcomes), [302, 302])
        from orders.models import Order
        order = Order.objects.get(email="race@example.test")
        self.assertEqual(Order.objects.filter(email="race@example.test").count(), 1)
        self.assertEqual(NotificationOutbox.objects.filter(order=order).count(), 1)
        self.assertEqual(MoySkladOrderLink.objects.filter(order=order).count(), 1)
        self.assertEqual(MoySkladTask.objects.filter(kind="export_order", order_link__order=order).count(), 1)
