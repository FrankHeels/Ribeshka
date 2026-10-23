"""PHASE A acceptance tests for MoySklad data, queue, configuration and privacy.

Run with:
    .venv/bin/python manage.py test tests.test_moysklad_foundation --settings=fishsite.settings_test --noinput
"""

import importlib
import hashlib
import uuid
from datetime import timedelta

from django.apps import apps
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import IntegrityError, transaction
from django.db import models
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from catalog.models import Category, Product
from content.models import ConsentRevision
from inquiries.models import Inquiry
from orders.forms import RetailRequestForm
from orders.models import Order, OrderItem
from tests.moysklad_helpers import ACCOUNT_ID, ENVIRONMENT, moysklad_config


def required_model(test, model_name):
    model = apps.get_model("integrations", model_name, require_ready=False)
    test.assertIsNotNone(model, f"integrations.{model_name} is required by PHASE A")
    return model


def required_field_values(model, **overrides):
    """Build minimal values for raw bulk inserts so DB constraints are exercised."""
    values = dict(overrides)
    for field in model._meta.concrete_fields:
        if field.primary_key or field.name in values or field.null or field.has_default():
            continue
        if field.is_relation:
            continue
        if isinstance(field, models.UUIDField):
            values[field.name] = uuid.uuid4()
        elif isinstance(field, models.DateTimeField):
            values[field.name] = timezone.now()
        elif isinstance(field, models.JSONField):
            values[field.name] = []
        elif isinstance(field, models.BooleanField):
            values[field.name] = False
        elif isinstance(field, models.IntegerField):
            values[field.name] = 0
        elif isinstance(field, models.CharField):
            values[field.name] = field.choices[0][0] if field.choices else "test"
    return values


class MoySkladProductFoundationTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="МойСклад", slug=f"moysklad-{uuid.uuid4().hex}")

    # Связь товара уникальна по аккаунту, типу и UUID, а одна карточка сайта
    # принадлежит не более чем одной внешней позиции.
    def test_external_product_identity_and_product_link_are_unique(self):
        model = required_model(self, "MoySkladProduct")
        fields = {field.name for field in model._meta.get_fields()}
        self.assertTrue({"account_id", "entity_type", "external_id", "product"} <= fields)
        product = Product.objects.create(
            name="Тестовый товар", slug=f"test-{uuid.uuid4().hex}", category=self.category,
            price_per_kg="10.00", is_published=True,
        )
        link = model.objects.create(
            account_id=ACCOUNT_ID, entity_type="product", external_id=uuid.uuid4(), product=product,
        )
        duplicate_identity = model(
            account_id=link.account_id, entity_type=link.entity_type,
            external_id=link.external_id, product=None,
        )
        with self.assertRaises((IntegrityError, ValidationError)):
            with transaction.atomic():
                duplicate_identity.save()
        duplicate_product = model(
            account_id=uuid.uuid4(), entity_type="product", external_id=uuid.uuid4(), product=product,
        )
        with self.assertRaises((IntegrityError, ValidationError)):
            with transaction.atomic():
                duplicate_product.save()

    # Обычный объект остаётся опубликованным, но публичный queryset скрывает
    # опубликованную карточку с причиной интеграционной блокировки.
    def test_integration_block_hides_without_rewriting_manual_publication_flag(self):
        fields = {field.name for field in Product._meta.fields}
        self.assertTrue({"integration_block_reason", "mapping_revision"} <= fields)
        blocked = Product.objects.create(
            name="Заблокированный товар", slug=f"blocked-{uuid.uuid4().hex}", category=self.category,
            price_per_kg="10.00", is_published=True, integration_block_reason="archived",
        )
        self.assertTrue(blocked.is_published)
        self.assertFalse(Product.objects.published().filter(pk=blocked.pk).exists())

    # Исторические позиции сохраняют nullable внешние снимки; заполненные
    # снимки нельзя изменить после принятия заказа.
    def test_order_item_has_immutable_nullable_external_identity_and_price_snapshots(self):
        expected_fields = {
            "external_account_id_snapshot", "external_entity_type_snapshot", "external_id_snapshot",
            "unit_id_snapshot", "price_revision_snapshot", "price_source_snapshot", "price_checked_at_snapshot",
        }
        fields = {field.name for field in OrderItem._meta.fields}
        self.assertTrue(expected_fields <= fields)
        self.assertEqual(OrderItem._meta.get_field("price_checked_at_snapshot").name, "price_checked_at_snapshot")
        for field_name in expected_fields:
            self.assertTrue(OrderItem._meta.get_field(field_name).null, field_name)
        protected = expected_fields & OrderItem.objects._snapshot_fields
        self.assertEqual(protected, expected_fields)


class MoySkladDatabaseContractTests(TestCase):
    # Ограничения очереди, inbox, инцидентов и outbox должны находиться в БД,
    # чтобы корректность сохранялась при нескольких процессах.
    def test_database_models_declare_deduplication_and_source_constraints(self):
        task = required_model(self, "MoySkladTask")
        inbox = required_model(self, "MoySkladWebhookInbox")
        incident = required_model(self, "MoySkladIncident")
        order_link = required_model(self, "MoySkladOrderLink")
        self.assertTrue(task._meta.constraints, "queue state and lease checks must be database constraints")
        self.assertTrue(inbox._meta.constraints, "inbox environment/account/request uniqueness must be in DB")
        self.assertTrue(incident._meta.constraints, "one open incident per account/environment/scope/code")
        self.assertTrue(order_link._meta.constraints, "one export link per order must be enforced")
        outbox = apps.get_model("integrations", "NotificationOutbox")
        self.assertTrue(outbox._meta.constraints, "outbox source exclusivity must be enforced in DB")

    # Состояние processing без полного lease отклоняет сама БД, даже при
    # обходе model.clean() и save() через bulk_create.
    def test_processing_task_requires_token_and_unexpired_lease_in_database(self):
        task = required_model(self, "MoySkladTask")
        values = required_field_values(
            task, account_id=ACCOUNT_ID, environment=ENVIRONMENT, kind="price",
            dedupe_key=f"invalid-lease:{uuid.uuid4()}", status="processing",
            lease_token=None, lease_expires_at=None,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            task.objects.bulk_create([task(**values)])

    # Inbox не создаёт вторую запись для повторной доставки одного request_id.
    def test_webhook_inbox_request_id_is_unique_per_environment_and_account(self):
        inbox = required_model(self, "MoySkladWebhookInbox")
        request_id = uuid.uuid4()
        values = required_field_values(
            inbox, account_id=ACCOUNT_ID, environment=ENVIRONMENT, request_id=request_id,
        )
        inbox.objects.bulk_create([inbox(**values)])
        with self.assertRaises(IntegrityError), transaction.atomic():
            inbox.objects.bulk_create([inbox(**values)])

    # Повторное наблюдение объединяется в один открытый incident; закрытый
    # incident допускает новый ряд той же области и причины.
    def test_only_one_open_incident_exists_for_scope_and_error(self):
        incident = required_model(self, "MoySkladIncident")
        identity = {
            "account_id": ACCOUNT_ID, "environment": ENVIRONMENT,
            "scope": "api_access", "code": "unauthorized",
        }
        values = required_field_values(incident, **identity)
        incident.objects.bulk_create([incident(**values)])
        with self.assertRaises(IntegrityError), transaction.atomic():
            incident.objects.bulk_create([incident(**values)])
        incident.objects.filter(**identity).update(closed_at=timezone.now())
        incident.objects.bulk_create([incident(**values)])

    # Outbox требует ровно один источник; инцидент является самостоятельным
    # допустимым источником наряду с розничной и оптовой заявкой.
    def test_outbox_source_exclusivity_is_enforced_and_incident_is_valid(self):
        outbox = apps.get_model("integrations", "NotificationOutbox")
        incident = required_model(self, "MoySkladIncident")
        self.assertIn("incident", {field.name for field in outbox._meta.fields})
        with self.assertRaises(IntegrityError), transaction.atomic():
            outbox.objects.bulk_create([outbox()])

        text = "Согласие для проверки БД"
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        revision = ConsentRevision.objects.create(
            revision_id=f"sha256:{digest}", sha256=digest, text=text,
        )
        order = Order.objects.create(
            name="Покупатель", phone="+79215550000", fulfillment_method=Order.FulfillmentMethod.PICKUP,
            privacy_consent=True, privacy_consent_at=timezone.now(), privacy_consent_revision=revision,
        )
        inquiry = Inquiry.objects.create(
            contact_name="Покупатель", phone="+79215550000", privacy_consent=True,
            privacy_consent_at=timezone.now(), privacy_consent_revision=revision,
        )
        incident_row = incident.objects.create(**required_field_values(
            incident, account_id=ACCOUNT_ID, environment=ENVIRONMENT,
            scope="api_access", code="unauthorized",
        ))
        with self.assertRaises(IntegrityError), transaction.atomic():
            outbox.objects.bulk_create([outbox(order=order, inquiry=inquiry, incident=incident_row)])
        valid = outbox.objects.create(incident=incident_row)
        self.assertEqual(valid.incident_id, incident_row.pk)


class MoySkladConfigurationTests(SimpleTestCase):
    # Тестовый профиль не наследует рабочие секреты даже при наличии env.
    def test_test_settings_force_synthetic_empty_moysklad_credentials(self):
        config = settings.MOYSKLAD
        self.assertEqual(config["environment"], ENVIRONMENT)
        self.assertFalse(config["api_token"])
        self.assertFalse(config["webhook_secret"])
        self.assertFalse(config["webhook_url"])
        for flag in ("catalog_enabled", "prices_enabled", "export_enabled", "statuses_enabled"):
            self.assertFalse(config[flag], flag)

    @override_settings(MOYSKLAD=moysklad_config() | {"currency_id": "invalid-uuid"})
    def test_enabled_configuration_rejects_missing_required_uuids(self):
        config = importlib.import_module("integrations.moysklad.config")
        with self.assertRaises((ImproperlyConfigured, ValidationError, ValueError)):
            config.get_config()

    @override_settings(MOYSKLAD=moysklad_config(enabled=False) | {"api_token": ""})
    def test_disabled_configuration_is_safe_without_credentials(self):
        config = importlib.import_module("integrations.moysklad.config")
        result = config.get_config()
        self.assertFalse(result.export_enabled)


class MoySkladQueueContractTests(TransactionTestCase):
    reset_sequences = True

    def queue(self):
        return importlib.import_module("integrations.moysklad.queue")

    def enqueue(self, kind, key, **kwargs):
        return self.queue().enqueue_task(
            kind=kind, dedupe_key=key, account_id=ACCOUNT_ID, environment=ENVIRONMENT, **kwargs,
        )

    # Конкурирующие процессы используют транзакционный claim; lease token и
    # срок захвата фиксируются на задаче до выполнения внешней работы.
    def test_claim_assigns_a_live_lease(self):
        task = self.enqueue("export_order", f"order:{uuid.uuid4()}")
        claimed = self.queue().claim_task()
        self.assertEqual(claimed.pk, task.pk)
        self.assertTrue(claimed.lease_token)
        self.assertGreater(claimed.lease_expires_at, timezone.now())

    # Повтор постановки с тем же ключом возвращает ту же активную задачу,
    # чтобы для внешнего товара оставалась одна задача цены.
    def test_duplicate_price_enqueue_returns_the_single_active_task(self):
        key = f"product:{uuid.uuid4()}"
        first = self.enqueue("price", key)
        second = self.enqueue("price", key)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(
            type(first).objects.filter(
                kind="price", dedupe_key=key, account_id=ACCOUNT_ID, environment=ENVIRONMENT,
                status__in=("pending", "processing"),
            ).count(),
            1,
        )

    # Старый процесс теряет право завершить задачу после истечения lease;
    # истекший автоматический export можно снова захватить.
    def test_expired_executor_cannot_finalize_and_automatic_task_is_reclaimed(self):
        task = self.enqueue("export_order", f"order:{uuid.uuid4()}")
        claimed = self.queue().claim_task()
        old_token = claimed.lease_token
        type(claimed).objects.filter(pk=claimed.pk).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1),
        )
        with self.assertRaises(Exception):
            self.queue().complete_task(claimed)
        reclaimed = self.queue().claim_task()
        self.assertEqual(reclaimed.pk, task.pk)
        self.assertNotEqual(reclaimed.lease_token, old_token)

    # Временная ошибка не ограничивает автоматически повторяемые export и
    # price-задачи числом попыток.
    def assert_task_retries_past_attempt_limit(self, kind):
        dedupe_key = f"{kind}:{uuid.uuid4()}"
        task = self.enqueue(kind, dedupe_key)
        for _ in range(12):
            claimed = self.queue().claim_task()
            self.assertEqual(claimed.pk, task.pk)
            self.queue().retry_task(claimed, "temporary_api_error", retry_after=timezone.now())
        task.refresh_from_db()
        self.assertEqual(task.status, "pending")

    def test_export_task_retries_past_a_conventional_attempt_limit(self):
        self.assert_task_retries_past_attempt_limit("export_order")

    def test_automatic_price_task_retries_past_a_conventional_attempt_limit(self):
        self.assert_task_retries_past_attempt_limit("price")

    # Потеря lease при ручной сверке каталога закрывает запуск ошибкой,
    # поскольку повторный полный проход требует нового действия владельца.
    def test_expired_manual_catalog_task_does_not_auto_repeat(self):
        task = self.enqueue("catalog_sync", f"catalog:{uuid.uuid4()}")
        claimed = self.queue().claim_task()
        type(claimed).objects.filter(pk=claimed.pk).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1),
        )
        self.queue().ensure_lease(claimed)
        claimed.refresh_from_db()
        self.assertEqual(claimed.status, "failed")
        self.assertEqual(task.pk, claimed.pk)


class MoySkladFormPrivacyTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="Form", slug=f"form-{uuid.uuid4().hex}")
        self.product = Product.objects.create(
            name="Form fish", slug=f"form-fish-{uuid.uuid4().hex}", category=self.category,
            price_per_kg="10.00", is_published=True,
        )

    # Адрес 255 и комментарий 3000 допустимы, следующий символ отклоняется;
    # существующие границы имени и email остаются прежними.
    def test_retail_form_boundaries_and_html_maxlength_values(self):
        valid = RetailRequestForm({
            "name": "Н" * 160, "phone": "+7 (921) 555-00-00", "email": f"{'a' * 247}@x.test",
            "pickup_method": "delivery", "address": "А" * 255, "comment": "К" * 3000,
            "consent": "yes", "submission_key": str(uuid.uuid4()), "submission_token": "synthetic-token",
        })
        self.assertTrue(valid.is_valid(), valid.errors)
        too_long_address = RetailRequestForm({
            "name": "Н", "phone": "+7 (921) 555-00-00", "pickup_method": "delivery", "address": "А" * 256,
            "consent": "yes", "submission_key": str(uuid.uuid4()), "submission_token": "synthetic-token",
        })
        self.assertFalse(too_long_address.is_valid())
        self.assertIn("address", too_long_address.errors)
        too_long_comment = RetailRequestForm({
            "name": "Н", "phone": "+7 (921) 555-00-00", "pickup_method": "pickup", "comment": "К" * 3001,
            "consent": "yes", "submission_key": str(uuid.uuid4()), "submission_token": "synthetic-token",
        })
        self.assertFalse(too_long_comment.is_valid())
        self.assertIn("comment", too_long_comment.errors)

    def test_retail_html_uses_contract_maxlengths(self):
        from django.test import Client
        from django.urls import reverse

        client = Client()
        client.post(reverse("orders:cart"), {"action": "add", "product_id": self.product.pk, "weight": "1"})
        response = client.get(reverse("orders:cart"))
        html = response.content.decode()
        self.assertRegex(html, r'<textarea[^>]*name="address"[^>]*maxlength="255"')
        self.assertRegex(html, r'<textarea[^>]*name="comment"[^>]*maxlength="3000"')
        self.assertRegex(html, r'<input[^>]*name="name"[^>]*maxlength="160"')
        self.assertRegex(html, r'<input[^>]*name="email"[^>]*maxlength="254"')
