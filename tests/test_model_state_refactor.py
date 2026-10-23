"""Account API ownership and reversible removal of duplicate model state.

Run sequentially with the isolated PostgreSQL profile described in
docs/test-environment.md; migration tests always restore the current graph.
"""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from catalog.models import Product
from content.models import ConsentRevision
from integrations.models import MoySkladApiState, MoySkladOrderLink, MoySkladSyncState
from integrations.moysklad.contracts import MoySkladError
from integrations.moysklad.order_export import process_export_task
from integrations.moysklad.queue import claim_task, enqueue_task
from integrations.moysklad.status_sync import process_status_task
from orders.models import Order
from tests.backoffice_helpers import login_verified_admin
from tests.moysklad_helpers import ACCOUNT_ID, ENVIRONMENT, NAMESPACE, moysklad_config


NOW = datetime(2026, 10, 23, 12, tzinfo=UTC)
API_FIELDS = (
    "api_next_at", "api_lease_token", "api_lease_expires_at",
    "api_paused_until", "access_error_code", "export_paused",
)


class ModelStateOwnershipTests(SimpleTestCase):
    def test_workflow_has_no_account_transport_fields_or_lease_constraint(self):
        fields = {field.name for field in MoySkladSyncState._meta.local_fields}
        self.assertEqual(set(API_FIELDS) & fields, set())
        self.assertNotIn("ms_api_lease_consistent", {
            constraint.name for constraint in MoySkladSyncState._meta.constraints
        })

    def test_account_state_retains_transport_fields_revisions_and_lease_constraint(self):
        fields = {field.name for field in MoySkladApiState._meta.local_fields}
        self.assertTrue(set(API_FIELDS) | {"access_revision", "export_pause_revision"} <= fields)
        self.assertIn("ms_account_api_lease_consistent", {
            constraint.name for constraint in MoySkladApiState._meta.constraints
        })

    def test_legacy_popular_flag_remains_without_an_index(self):
        field = Product._meta.get_field("is_popular")
        self.assertFalse(field.db_index)


@override_settings(MOYSKLAD=moysklad_config())
class AccountStateConsumerTests(TestCase):
    def setUp(self):
        self.workflow = MoySkladSyncState.objects.create(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, namespace=NAMESPACE,
            export_enabled=True, statuses_enabled=True,
        )

    def link(self):
        revision = ConsentRevision.objects.create(text="Synthetic model refactor consent")
        order = Order.objects.create(
            name="Synthetic customer", phone="+7 900 000-00-00",
            fulfillment_method="pickup", privacy_consent=True,
            privacy_consent_at=NOW, privacy_consent_revision=revision,
        )
        return MoySkladOrderLink.objects.create(
            order=order, account_id=ACCOUNT_ID, environment=ENVIRONMENT,
            external_id=uuid.uuid4(), approved_at=NOW,
            config_snapshot={"namespace": str(NAMESPACE), "consent_approved": True},
            prepared_payload={"synthetic": True},
        )

    def task(self, kind, link):
        enqueue_task(
            kind=kind, dedupe_key=f"{kind}:{link.pk}", order_link=link,
            account_id=ACCOUNT_ID, environment=ENVIRONMENT,
        )
        return claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=(kind,))

    def assert_paused(self, task, client):
        client.get.assert_not_called()
        client.post.assert_not_called()
        task.refresh_from_db()
        self.assertEqual((task.status, task.last_error_code), ("pending", "stream_paused"))

    def test_export_worker_honors_account_export_pause_with_clean_workflow(self):
        MoySkladApiState.objects.create(account_id=ACCOUNT_ID, export_paused=True)
        task = self.task("export_order", self.link())
        client = Mock(get=Mock(side_effect=MoySkladError("temporary_api_error", retryable=True)))

        process_export_task(task, client=client)

        self.assert_paused(task, client)

    def test_export_worker_honors_account_access_error_with_clean_workflow(self):
        MoySkladApiState.objects.create(account_id=ACCOUNT_ID, access_error_code="access_denied")
        task = self.task("export_order", self.link())
        client = Mock(get=Mock(side_effect=MoySkladError("temporary_api_error", retryable=True)))

        process_export_task(task, client=client)

        self.assert_paused(task, client)

    def test_status_worker_honors_account_access_error_with_clean_workflow(self):
        MoySkladApiState.objects.create(account_id=ACCOUNT_ID, access_error_code="access_denied")
        link = self.link()
        task = self.task("status_sync", link)
        client = Mock(get=Mock(return_value={"id": str(link.external_id)}))

        process_status_task(task, client=client)

        self.assert_paused(task, client)
        link.refresh_from_db()
        self.assertIsNone(link.status_checked_at)

    def test_export_worker_ignores_other_account_pause(self):
        MoySkladApiState.objects.create(
            account_id=uuid.uuid4(), access_error_code="access_denied", export_paused=True,
        )
        link = self.link()
        task = self.task("export_order", link)
        client = Mock(get=Mock(side_effect=MoySkladError("temporary_api_error", retryable=True)))

        process_export_task(task, client=client)

        client.get.assert_called_once_with(f"entity/customerorder/{link.external_id}")
        client.post.assert_not_called()
        task.refresh_from_db()
        self.assertEqual((task.status, task.last_error_code), ("pending", "temporary_api_error"))

    def test_status_worker_ignores_other_account_access_error(self):
        MoySkladApiState.objects.create(account_id=uuid.uuid4(), access_error_code="access_denied")
        link = self.link()
        task = self.task("status_sync", link)
        client = Mock(get=Mock(return_value={"id": str(link.external_id)}))

        process_status_task(task, client=client)

        client.get.assert_called_once_with(f"entity/customerorder/{link.external_id}")
        client.post.assert_not_called()
        task.refresh_from_db()
        self.assertEqual(task.status, "done")

    def test_missing_account_state_diagnostics_use_empty_defaults_without_writes(self):
        user = get_user_model().objects.create_superuser(username="state-diagnostics", password="synthetic")
        login_verified_admin(self.client, user)
        before = list(MoySkladSyncState.objects.values())

        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(reverse("admin:moysklad_technical"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["api"], {
            "access_error_code": "", "paused_until": None, "export_paused": False,
        })
        self.assertFalse(MoySkladApiState.objects.exists())
        self.assertEqual(list(MoySkladSyncState.objects.values()), before)
        writes = [query["sql"] for query in captured.captured_queries
                  if query["sql"].lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE"}]
        self.assertEqual(writes, [])


class ApiStateConsolidationMigrationTests(TransactionTestCase):
    migrate_from = ("integrations", "0008_moysklad_order_deletion")
    migrate_to = ("integrations", "0009_consolidate_api_state")

    def test_conservative_account_merge_preserves_workflows_and_survives_round_trip(self):
        executor = MigrationExecutor(connection)
        self.assertIn(self.migrate_to, executor.loader.graph.nodes, "Missing API state consolidation migration")
        self.assertIn(self.migrate_from, executor.loader.disk_migrations[self.migrate_to].dependencies)
        latest = executor.loader.graph.leaf_nodes()
        old_targets = [node for node in latest if node[0] != "integrations"] + [self.migrate_from]
        new_targets = [node for node in latest if node[0] != "integrations"] + [self.migrate_to]
        try:
            executor.migrate(old_targets)
            old_apps = executor.loader.project_state(old_targets).apps
            Api = old_apps.get_model("integrations", "MoySkladApiState")
            Workflow = old_apps.get_model("integrations", "MoySkladSyncState")
            Revision = old_apps.get_model("content", "ConsentRevision")
            text = "Synthetic historical consent"
            digest = hashlib.sha256(text.encode()).hexdigest()
            revision = Revision.objects.create(text=text, sha256=digest, revision_id=f"sha256:{digest}")
            account_b, account_c, account_d, account_only = (uuid.uuid4() for _ in range(4))
            api_token, longer_token, retained_token, cleared_token = (uuid.uuid4() for _ in range(4))
            Api.objects.create(
                account_id=ACCOUNT_ID, api_next_at=NOW + timedelta(minutes=4),
                api_paused_until=NOW + timedelta(minutes=2),
                api_lease_token=api_token, api_lease_expires_at=NOW + timedelta(minutes=3),
                access_error_code="access_denied", access_revision=7,
                export_paused=False, export_pause_revision=9,
            )
            Api.objects.create(account_id=account_c, export_paused=True, access_revision=11, export_pause_revision=13)
            Api.objects.create(
                account_id=account_only, access_error_code="account_only_error", export_paused=True,
                access_revision=21, export_pause_revision=22,
            )
            Api.objects.create(
                account_id=account_d, api_next_at=NOW + timedelta(minutes=50),
                api_paused_until=NOW + timedelta(minutes=40),
                api_lease_token=retained_token, api_lease_expires_at=NOW + timedelta(minutes=30),
                access_revision=5, export_pause_revision=6,
            )
            common = {
                "export_started_at": NOW - timedelta(days=2), "export_consent_revision_id": revision.pk,
                "heartbeat_at": NOW, "catalog_enabled": True, "prices_enabled": False,
                "export_enabled": True, "statuses_enabled": None,
                "prices_next_at": NOW + timedelta(hours=1), "status_next_at": NOW + timedelta(hours=2),
            }
            first = Workflow.objects.create(
                **common, account_id=ACCOUNT_ID, environment="staging", namespace=uuid.uuid4(),
                api_next_at=NOW + timedelta(minutes=1), api_paused_until=NOW + timedelta(minutes=8),
                api_lease_token=longer_token, api_lease_expires_at=NOW + timedelta(minutes=12),
                access_error_code="legacy_error", export_paused=True,
            )
            Workflow.objects.create(
                **common, account_id=ACCOUNT_ID, environment="production", namespace=uuid.uuid4(),
                api_next_at=NOW + timedelta(minutes=10), api_paused_until=NOW + timedelta(minutes=5),
                api_lease_token=uuid.uuid4(), api_lease_expires_at=NOW + timedelta(minutes=7),
                export_paused=False,
            )
            Workflow.objects.create(
                account_id=account_b, environment="test", namespace=uuid.uuid4(),
                api_next_at=NOW + timedelta(minutes=20), api_paused_until=NOW + timedelta(minutes=21),
                api_lease_token=cleared_token, api_lease_expires_at=NOW + timedelta(minutes=22),
                access_error_code="legacy_only_error", export_paused=False,
            )
            Workflow.objects.create(account_id=account_c, environment="test", namespace=uuid.uuid4())
            Workflow.objects.create(
                account_id=account_d, environment="test", namespace=uuid.uuid4(),
                api_next_at=NOW + timedelta(minutes=1), api_paused_until=NOW + timedelta(minutes=2),
                api_lease_token=uuid.uuid4(), api_lease_expires_at=NOW + timedelta(minutes=15),
                access_error_code="legacy_fallback_error",
            )
            Workflow.objects.create(
                account_id=account_d, environment="lease-tie", namespace=uuid.uuid4(),
                api_lease_token=uuid.uuid4(), api_lease_expires_at=NOW + timedelta(minutes=30),
            )
            workflow_fields = [field.name for field in Workflow._meta.local_fields if field.name not in API_FIELDS]
            before = list(Workflow.objects.order_by("pk").values_list(*workflow_fields))

            executor = MigrationExecutor(connection)
            executor.migrate(new_targets)
            new_apps = executor.loader.project_state(new_targets).apps
            Api = new_apps.get_model("integrations", "MoySkladApiState")
            Workflow = new_apps.get_model("integrations", "MoySkladSyncState")
            self.assertEqual(list(Workflow.objects.order_by("pk").values_list(*workflow_fields)), before)
            self.assertEqual(Api.objects.count(), 5)
            a = Api.objects.get(account_id=ACCOUNT_ID)
            self.assertEqual((a.api_next_at, a.api_paused_until),
                             (NOW + timedelta(minutes=10), NOW + timedelta(minutes=8)))
            self.assertEqual((a.api_lease_token, a.api_lease_expires_at),
                             (longer_token, NOW + timedelta(minutes=12)))
            self.assertEqual((a.access_error_code, a.export_paused, a.access_revision, a.export_pause_revision),
                             ("access_denied", True, 7, 9))
            b = Api.objects.get(account_id=account_b)
            self.assertEqual((b.api_next_at, b.api_paused_until, b.access_error_code, b.export_paused),
                             (NOW + timedelta(minutes=20), NOW + timedelta(minutes=21), "legacy_only_error", False))
            self.assertEqual((b.api_lease_token, b.api_lease_expires_at, b.access_revision, b.export_pause_revision),
                             (cleared_token, NOW + timedelta(minutes=22), 0, 0))
            c = Api.objects.get(account_id=account_c)
            self.assertEqual((c.api_next_at, c.api_paused_until, c.access_error_code, c.export_paused,
                              c.access_revision, c.export_pause_revision), (None, None, "", True, 11, 13))
            d = Api.objects.get(account_id=account_d)
            self.assertEqual((d.api_next_at, d.api_paused_until, d.api_lease_token, d.api_lease_expires_at,
                              d.access_error_code, d.export_paused, d.access_revision, d.export_pause_revision),
                             (NOW + timedelta(minutes=50), NOW + timedelta(minutes=40), retained_token,
                              NOW + timedelta(minutes=30), "legacy_fallback_error", False, 5, 6))
            only = Api.objects.get(account_id=account_only)
            self.assertEqual((only.access_error_code, only.export_paused, only.access_revision,
                              only.export_pause_revision), ("account_only_error", True, 21, 22))
            with connection.cursor() as cursor:
                columns = {column.name for column in connection.introspection.get_table_description(cursor, Workflow._meta.db_table)}
                constraints = connection.introspection.get_constraints(cursor, Workflow._meta.db_table)
            self.assertEqual(set(API_FIELDS) & columns, set())
            self.assertNotIn("ms_api_lease_consistent", constraints)

            # Account state may change between deploy and rollback. Restore
            # those current values, including a cleared export pause.
            Api.objects.filter(account_id=ACCOUNT_ID).update(
                api_next_at=NOW + timedelta(minutes=60), api_paused_until=NOW + timedelta(minutes=70),
                api_lease_token=uuid.uuid4(), api_lease_expires_at=NOW + timedelta(minutes=80),
                access_error_code="updated_access_error", export_paused=False,
                access_revision=17, export_pause_revision=19,
            )
            Api.objects.filter(account_id=account_b).update(
                access_error_code="", api_lease_token=None, api_lease_expires_at=None,
            )
            expected_api = list(Api.objects.order_by("pk").values())

            # Reverse must copy the account state to every environment before
            # old code can read those restored columns again.
            executor = MigrationExecutor(connection)
            executor.migrate(old_targets)
            restored_apps = executor.loader.project_state(old_targets).apps
            RestoredWorkflow = restored_apps.get_model("integrations", "MoySkladSyncState")
            RestoredApi = restored_apps.get_model("integrations", "MoySkladApiState")
            for workflow in RestoredWorkflow.objects.order_by("pk"):
                api = RestoredApi.objects.get(account_id=workflow.account_id)
                with self.subTest(account=workflow.account_id, environment=workflow.environment):
                    self.assertEqual(tuple(getattr(workflow, name) for name in API_FIELDS),
                                     tuple(getattr(api, name) for name in API_FIELDS))
            self.assertEqual(RestoredWorkflow.objects.get(pk=first.pk).export_consent_revision_id, revision.pk)
            self.assertEqual(list(RestoredWorkflow.objects.filter(account_id=account_b).values_list(
                "access_error_code", "api_lease_token", "api_lease_expires_at",
            )), [("", None, None)])
            self.assertEqual(list(RestoredWorkflow.objects.order_by("pk").values_list(*workflow_fields)), before)

            executor = MigrationExecutor(connection)
            executor.migrate(new_targets)
            reapplied = executor.loader.project_state(new_targets).apps
            self.assertEqual(list(reapplied.get_model("integrations", "MoySkladApiState").objects.order_by("pk").values()), expected_api)
            self.assertEqual(list(reapplied.get_model("integrations", "MoySkladSyncState").objects.order_by("pk").values_list(*workflow_fields)), before)
        finally:
            MigrationExecutor(connection).migrate(latest)


class LegacyPopularIndexMigrationTests(TransactionTestCase):
    migrate_from = ("catalog", "0006_product_integration_block_reason_and_more")
    migrate_to = ("catalog", "0007_remove_legacy_popular_index")

    def test_index_removal_retains_both_legacy_flag_values_and_other_product_data(self):
        executor = MigrationExecutor(connection)
        self.assertIn(self.migrate_to, executor.loader.graph.nodes, "Missing popular index removal migration")
        latest = executor.loader.graph.leaf_nodes()
        old_targets = [node for node in latest if node[0] != "catalog"] + [self.migrate_from]
        new_targets = [node for node in latest if node[0] != "catalog"] + [self.migrate_to]
        try:
            executor.migrate(old_targets)
            apps = executor.loader.project_state(old_targets).apps
            Category = apps.get_model("catalog", "Category")
            LegacyProduct = apps.get_model("catalog", "Product")
            category = Category.objects.create(name="Historical fish", slug="historical-index-fish")
            products = [LegacyProduct.objects.create(
                category_id=category.pk, name=f"Fish {popular}", slug=f"index-fish-{popular}",
                price_per_kg=Decimal("123.45"), is_popular=popular,
            ) for popular in (True, False)]
            before = list(LegacyProduct.objects.order_by("pk").values())

            executor = MigrationExecutor(connection)
            executor.migrate(new_targets)
            Product = executor.loader.project_state(new_targets).apps.get_model("catalog", "Product")
            self.assertEqual(list(Product.objects.order_by("pk").values()), before)
            self.assertEqual(list(Product.objects.filter(pk__in=[p.pk for p in products]).order_by("pk").values_list("is_popular", flat=True)), [True, False])
            self.assertFalse(Product._meta.get_field("is_popular").db_index)
            with connection.cursor() as cursor:
                constraints = connection.introspection.get_constraints(cursor, Product._meta.db_table)
            self.assertEqual([name for name, info in constraints.items()
                              if info["index"] and "is_popular" in info["columns"]], [])
        finally:
            MigrationExecutor(connection).migrate(latest)
