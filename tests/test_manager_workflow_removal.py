"""Removal of local staff workflow must preserve incoming requests and exchange data."""
import uuid

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.exceptions import FieldDoesNotExist
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from content.consent_revisions import get_current_consent_revision
from inquiries.models import Inquiry
from orders.models import Order, OrderItem
from tests.backoffice_helpers import login_verified_admin


class RequestAdministrationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_superuser(username="request-owner", password="synthetic-test-password")
        consent = get_current_consent_revision()
        common = {"phone": "0000000000", "privacy_consent": True,
                  "privacy_consent_at": timezone.now(), "privacy_consent_revision": consent}
        cls.order = Order.objects.create(name="Покупатель", fulfillment_method="pickup", comment="Комментарий покупателя", **common)
        cls.inquiry = Inquiry.objects.create(contact_name="Заказчик", company="Компания", need="Исходный запрос", **common)
        OrderItem.objects.create(order=cls.order, product_name_snapshot="Угорь", price_per_kg_snapshot="2200.00", desired_weight="2")

    def setUp(self):
        login_verified_admin(self.client, self.owner)

    def test_obsolete_fields_are_absent_from_models_and_database(self):
        for model, removed in ((Order, ("assigned_to", "internal_notes", "viewed_at")),
                               (Inquiry, ("assigned_to", "internal_notes"))):
            with connection.cursor() as cursor:
                columns = {column.name for column in connection.introspection.get_table_description(cursor, model._meta.db_table)}
            for name in removed:
                with self.subTest(model=model.__name__, field=name):
                    with self.assertRaises(FieldDoesNotExist):
                        model._meta.get_field(name)
                    self.assertNotIn(f"{name}_id" if name == "assigned_to" else name, columns)

    def test_request_screens_have_no_staff_workflow_and_keep_original_details(self):
        for model, obj, expected in ((Order, self.order, "Комментарий покупателя"),
                                     (Inquiry, self.inquiry, "Исходный запрос")):
            with self.subTest(model=model.__name__):
                response = self.client.get(reverse(f"admin:{model._meta.app_label}_{model._meta.model_name}_change", args=(obj.pk,)))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, expected)
                self.assertContains(response, "Архив согласия")
                self.assertEqual(list(response.context["adminform"].form.fields), [])
                for text in ("Работа менеджера", "Ответственный менеджер", "Заметка менеджера", "Просмотрена менеджером"):
                    self.assertNotContains(response, text)
        response = self.client.get(reverse("admin:orders_order_changelist"))
        self.assertEqual(response.status_code, 200)
        for text in ("Отметить просмотренными", "Просмотр менеджером", "Не просмотрена"):
            self.assertNotContains(response, text)
        self.assertContains(response, "4400.00")

    def test_old_post_fields_cannot_change_request_data(self):
        for model, obj in ((Order, self.order), (Inquiry, self.inquiry)):
            with self.subTest(model=model.__name__):
                before = model.objects.filter(pk=obj.pk).values().get()
                response = self.client.post(reverse(f"admin:{model._meta.app_label}_{model._meta.model_name}_change", args=(obj.pk,)), {
                    "_save": "Сохранить", "assigned_to": self.owner.pk, "internal_notes": "Лишняя заметка",
                    "viewed_at": "2026-01-01", "name": "Подмена", "contact_name": "Подмена",
                })
                self.assertEqual(response.status_code, 403)
                self.assertEqual(model.objects.filter(pk=obj.pk).values().get(), before)

    def test_dashboard_counts_all_requests_without_read_tracking(self):
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["retail_count"], 1)
        self.assertEqual(response.context["wholesale_count"], 1)
        self.assertContains(response, "Розничные заявки — всего")
        self.assertContains(response, self.order.order_number)
        self.assertNotIn("unseen_retail_count", response.context)
        self.assertNotIn("viewed_at", response.context["recent_retail_requests"][0])
        self.assertNotContains(response, "Непросмотренные")
        self.assertNotContains(response, "Не просмотрена")

    def test_owner_role_allows_viewing_but_not_editing_requests(self):
        owner = get_user_model().objects.create_user(username="ordinary-request-owner", is_staff=True)
        call_command("sync_backoffice_roles", owner.username, verbosity=0)
        login_verified_admin(self.client, owner)
        self.assertTrue(owner.has_perm("orders.view_order"))
        self.assertTrue(owner.has_perm("inquiries.view_inquiry"))
        self.assertFalse(owner.has_perm("orders.change_order"))
        self.assertFalse(owner.has_perm("inquiries.change_inquiry"))
        for model, obj in ((Order, self.order), (Inquiry, self.inquiry)):
            response = self.client.get(reverse(f"admin:{model._meta.app_label}_{model._meta.model_name}_change", args=(obj.pk,)))
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, 'name="_save"')
        dashboard = self.client.get(reverse("admin:index"))
        self.assertEqual(dashboard.context["retail_count"], 1)
        self.assertEqual(dashboard.context["wholesale_count"], 1)


class WorkflowRemovalMigrationTests(TransactionTestCase):
    def test_existing_request_snapshots_notifications_and_exchange_survive(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        before = [node for node in latest if node[0] not in ("orders", "inquiries")] + [
            ("orders", "0004_orderitem_external_account_id_snapshot_and_more"),
            ("inquiries", "0005_remove_inquiry_status"),
        ]
        try:
            executor.migrate(before)
            old = executor.loader.project_state(before).apps
            now = timezone.now()
            user = old.get_model(get_user_model()._meta.label).objects.create(username="previous-manager", is_staff=True)
            consent = old.get_model("content", "ConsentRevision").objects.create(
                revision_id="sha256:" + "a" * 64, sha256="a" * 64, text="Синтетический архив согласия")
            common = {"phone": "0000000000", "privacy_consent": True, "privacy_consent_at": now,
                      "privacy_consent_revision_id": consent.pk, "assigned_to_id": user.pk, "internal_notes": "Удаляемая заметка"}
            order = old.get_model("orders", "Order").objects.create(
                order_number="R-PRESERVE", name="Покупатель", fulfillment_method="pickup", viewed_at=now, comment="Исходный комментарий", **common)
            inquiry = old.get_model("inquiries", "Inquiry").objects.create(
                inquiry_number="W-PRESERVE", contact_name="Заказчик", company="Компания", need="Исходная потребность", **common)
            old.get_model("orders", "OrderItem").objects.create(
                order_id=order.pk, product_name_snapshot="Угорь", price_per_kg_snapshot="2200.00", desired_weight="1.25", line_total="2750.00",
                external_account_id_snapshot=uuid.uuid4(), external_entity_type_snapshot="product", external_id_snapshot=uuid.uuid4(),
                unit_id_snapshot=uuid.uuid4(), price_revision_snapshot=3, price_source_snapshot="moysklad", price_checked_at_snapshot=now)
            outbox = old.get_model("integrations", "NotificationOutbox")
            outbox.objects.create(order_id=order.pk, status="manual_review", last_error="synthetic_error")
            outbox.objects.create(inquiry_id=inquiry.pk, status="sent", sent_at=now)
            account = uuid.uuid4()
            link = old.get_model("integrations", "MoySkladOrderLink").objects.create(
                order_id=order.pk, account_id=account, environment="test", external_id=uuid.uuid4(),
                state_name="Подтвержден", config_snapshot={"synthetic": True}, prepared_payload={"name": "R-PRESERVE"})
            old.get_model("integrations", "MoySkladTask").objects.create(
                account_id=account, environment="test", kind="export_order", dedupe_key="preserve-order",
                order_link_id=link.pk, status="manual_review", last_error_code="synthetic_error")
            group = old.get_model("auth", "Group").objects.create(name="FishSite Owner")
            perms = old.get_model("auth", "Permission").objects
            for app, codename in (("orders", "view_order"), ("orders", "change_order"), ("inquiries", "change_inquiry")):
                group.permissions.add(perms.get(content_type__app_label=app, codename=codename))
            labels = ("orders.Order", "orders.OrderItem", "inquiries.Inquiry", "content.ConsentRevision",
                      "integrations.NotificationOutbox", "integrations.MoySkladOrderLink", "integrations.MoySkladTask")
            snapshots = {}
            for label in labels:
                model = old.get_model(label)
                fields = [field.attname for field in model._meta.fields if field.name not in ("assigned_to", "internal_notes", "viewed_at")]
                snapshots[label] = (fields, list(model.objects.order_by("pk").values(*fields)))
            executor = MigrationExecutor(connection)
            executor.migrate(latest)
            current = executor.loader.project_state(latest).apps
            for label, (fields, rows) in snapshots.items():
                with self.subTest(model=label):
                    self.assertEqual(list(current.get_model(label).objects.order_by("pk").values(*fields)), rows)
            permissions = current.get_model("auth", "Group").objects.get(pk=group.pk).permissions
            self.assertEqual(list(permissions.values_list("codename", flat=True)), ["view_order"])
        finally:
            MigrationExecutor(connection).migrate(latest)
