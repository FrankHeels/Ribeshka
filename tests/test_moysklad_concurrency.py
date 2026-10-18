"""Multi-connection PostgreSQL acceptance tests for MoySklad write paths."""

from concurrent.futures import ThreadPoolExecutor
import uuid
from threading import Barrier

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connections
from django.test import TransactionTestCase, override_settings

from catalog.models import Category, Product
from integrations.models import (
    MoySkladIncident,
    MoySkladProduct,
    MoySkladSyncRun,
    MoySkladTask,
    MoySkladWebhookInbox,
    NotificationOutbox,
)
from integrations.moysklad.contracts import MoySkladError
from tests.moysklad_helpers import ACCOUNT_ID, ENVIRONMENT, moysklad_config, product_payload


@override_settings(MOYSKLAD=moysklad_config())
class MoySkladConcurrentWriteTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.category = Category.objects.create(
            name="Конкурентная проверка", slug=f"concurrency-{uuid.uuid4().hex}"
        )
        self.owner = get_user_model().objects.create_superuser(
            username=f"concurrency-{uuid.uuid4().hex}",
            email=f"{uuid.uuid4().hex}@example.test",
            password="test-password",
        )

    def run_concurrently(self, calls):
        """Run operations behind a barrier, each on a fresh DB connection."""
        barrier = Barrier(len(calls), timeout=15)

        def invoke(call):
            close_old_connections()
            try:
                connection = connections["default"]
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '10s'")
                    cursor.execute("SET statement_timeout = '20s'")
                barrier.wait()
                return call()
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=len(calls)) as executor:
            futures = [executor.submit(invoke, call) for call in calls]
            # result() propagates worker exceptions, including database errors
            # and deadlocks, instead of turning them into a test outcome value.
            return [future.result(timeout=30) for future in futures]

    def make_product(self, suffix):
        return Product.objects.create(
            name=f"Карточка {suffix}", slug=f"concurrency-product-{suffix}-{uuid.uuid4().hex}",
            category=self.category, price_per_kg="80.00", is_published=True,
        )

    def test_concurrent_duplicate_webhook_delivery_creates_one_inbox_and_task(self):
        from integrations.moysklad.webhooks import receive_webhook

        request_id = uuid.uuid4()
        external_id = uuid.uuid4()
        events = [{"external_id": str(external_id), "updated_fields": ["salePrices"]}]

        results = self.run_concurrently([
            lambda: receive_webhook(request_id=request_id, events=events),
            lambda: receive_webhook(request_id=request_id, events=events),
        ])

        self.assertEqual(results, ["accepted", "accepted"])
        inboxes = MoySkladWebhookInbox.objects.filter(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, request_id=request_id,
        )
        self.assertEqual(inboxes.count(), 1)
        self.assertEqual(MoySkladTask.objects.filter(kind="webhook", inbox__in=inboxes).count(), 1)

    def test_concurrent_catalog_sync_start_reuses_running_run_and_task(self):
        from integrations.moysklad.catalog_sync import start_catalog_sync

        runs = self.run_concurrently([
            lambda: start_catalog_sync(actor=None),
            lambda: start_catalog_sync(actor=None),
        ])

        self.assertEqual(runs[0].pk, runs[1].pk)
        self.assertEqual(MoySkladSyncRun.objects.filter(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT,
            kind="catalog", status="running",
        ).count(), 1)
        self.assertEqual(MoySkladTask.objects.filter(
            kind="catalog_sync", sync_run_id=runs[0].pk,
            status__in=(MoySkladTask.Status.PENDING, MoySkladTask.Status.PROCESSING),
        ).count(), 1)

    def test_concurrent_workers_claim_one_export_task_once(self):
        task = MoySkladTask.objects.create(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT,
            kind=MoySkladTask.Kind.EXPORT_ORDER, dedupe_key=f"claim:{uuid.uuid4()}",
        )
        from integrations.moysklad.queue import claim_task

        results = self.run_concurrently([
            lambda: claim_task(
                account_id=ACCOUNT_ID, environment=ENVIRONMENT,
                kinds=(MoySkladTask.Kind.EXPORT_ORDER,),
            ),
            lambda: claim_task(
                account_id=ACCOUNT_ID, environment=ENVIRONMENT,
                kinds=(MoySkladTask.Kind.EXPORT_ORDER,),
            ),
        ])

        claimed = [result for result in results if result is not None]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].pk, task.pk)
        self.assertEqual(sum(result is None for result in results), 1)
        task.refresh_from_db()
        self.assertEqual(task.status, MoySkladTask.Status.PROCESSING)
        self.assertEqual(task.attempt_count, 1)

    def test_concurrent_link_of_one_external_uuid_has_one_winner_and_price_task(self):
        from integrations.moysklad.catalog_sync import link_product, upsert_product

        external_id = uuid.uuid4()
        source = upsert_product(product_payload(external_id))
        products = (self.make_product("one"), self.make_product("two"))

        def link(product):
            try:
                linked = link_product(
                    product_id=product.pk, external_product_id=external_id, actor=self.owner,
                )
                return ("linked", linked.product_id)
            except (ValidationError, MoySkladError) as error:
                return ("rejected", error)

        results = self.run_concurrently([lambda: link(products[0]), lambda: link(products[1])])

        successes = [result for result in results if result[0] == "linked"]
        rejected = [result for result in results if result[0] == "rejected"]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(rejected), 1)
        if isinstance(rejected[0][1], MoySkladError):
            self.assertEqual(rejected[0][1].code, "mapping_conflict")
        else:
            self.assertIsInstance(rejected[0][1], ValidationError)

        source.refresh_from_db()
        self.assertIn(source.product_id, {product.pk for product in products})
        self.assertEqual(MoySkladProduct.objects.filter(external_id=external_id, product__isnull=False).count(), 1)
        self.assertEqual(MoySkladTask.objects.filter(
            kind="price", external_product_id=source.pk,
            status__in=(MoySkladTask.Status.PENDING, MoySkladTask.Status.PROCESSING),
        ).count(), 1)

    def test_concurrent_same_cause_incident_merges_ids_and_creates_one_outbox(self):
        from integrations.moysklad.incidents import open_incident

        calls = (
            lambda: open_incident(
                scope="catalog", code="temporary_api_error", object_ids=["OBJECT-A"],
                account_id=ACCOUNT_ID, environment=ENVIRONMENT,
            ),
            lambda: open_incident(
                scope="catalog", code="temporary_api_error", object_ids=["OBJECT-B"],
                account_id=ACCOUNT_ID, environment=ENVIRONMENT,
            ),
        )
        incidents = self.run_concurrently(calls)

        self.assertEqual(incidents[0].pk, incidents[1].pk)
        open_incidents = MoySkladIncident.objects.filter(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, scope="catalog",
            code="temporary_api_error", closed_at__isnull=True,
        )
        self.assertEqual(open_incidents.count(), 1)
        self.assertCountEqual(open_incidents.get().object_ids, ["OBJECT-A", "OBJECT-B"])
        self.assertEqual(NotificationOutbox.objects.filter(incident__in=open_incidents).count(), 1)
