"""Durable exchange state registered by the integrations application."""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


class MoySkladProduct(models.Model):
    account_id = models.UUIDField()
    entity_type = models.CharField(max_length=32, default="product")
    external_id = models.UUIDField()
    product = models.OneToOneField(
        "catalog.Product", null=True, blank=True, on_delete=models.PROTECT, related_name="moysklad_product",
    )
    name = models.CharField(max_length=255, blank=True)
    article = models.CharField(max_length=255, blank=True)
    code = models.CharField(max_length=255, blank=True)
    unit_id = models.UUIDField(null=True, blank=True)
    unit_name = models.CharField(max_length=255, blank=True)
    archived = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)
    is_deferred = models.BooleanField(default=False)
    metadata_updated_at = models.DateTimeField(null=True, blank=True)
    source_updated_at = models.DateTimeField(null=True, blank=True)
    last_checked_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    price_checked_at = models.DateTimeField(null=True, blank=True)
    last_error_code = models.CharField(max_length=64, blank=True)
    price_requested_revision = models.PositiveBigIntegerField(default=0)
    price_processed_revision = models.PositiveBigIntegerField(default=0)
    price_pending_since = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "integrations"
        verbose_name = "товар МойСклад"
        verbose_name_plural = "товары МойСклад"
        constraints = [
            models.UniqueConstraint(fields=("account_id", "entity_type", "external_id"), name="ms_product_external_unique"),
            models.CheckConstraint(condition=Q(price_processed_revision__lte=F("price_requested_revision")), name="ms_price_revision_order"),
        ]
        permissions = [
            ("link_moysklad_product", "Может сопоставлять товары МойСклад"),
            ("create_moysklad_candidate", "Может создавать карточки из МойСклад"),
        ]

    def __str__(self):
        return self.name or str(self.external_id)


class MoySkladOrderLink(models.Model):
    order = models.OneToOneField("orders.Order", on_delete=models.PROTECT, related_name="moysklad_link")
    account_id = models.UUIDField()
    environment = models.CharField(max_length=32)
    sync_id = models.UUIDField(default=uuid.uuid4)
    external_id = models.UUIDField(null=True, blank=True)
    config_snapshot = models.JSONField(default=dict, blank=True)
    prepared_payload = models.JSONField(default=dict, blank=True)
    payload_hash = models.CharField(max_length=64, blank=True)
    attempted = models.BooleanField(default=False)
    state_id = models.UUIDField(null=True, blank=True)
    state_name = models.CharField(max_length=255, blank=True)
    state_type = models.CharField(max_length=32, blank=True)
    status_checked_at = models.DateTimeField(null=True, blank=True)
    last_error_code = models.CharField(max_length=64, blank=True)
    exported_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="moysklad_order_approvals")
    deletion_requested_at = models.DateTimeField(null=True, blank=True)
    deletion_requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="moysklad_order_deletions")
    override_payload = models.JSONField(default=dict, blank=True)
    override_reason = models.TextField(blank=True)
    override_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="moysklad_order_overrides")
    override_at = models.DateTimeField(null=True, blank=True)
    audit_log = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "integrations"
        verbose_name = "передача заявки в МойСклад"
        verbose_name_plural = "передачи заявок в МойСклад"
        constraints = [
            models.UniqueConstraint(fields=("account_id", "sync_id"), name="ms_order_sync_unique"),
            models.UniqueConstraint(fields=("account_id", "external_id"), condition=Q(external_id__isnull=False), name="ms_order_external_unique"),
        ]
        permissions = [("repair_moysklad_order", "Может исправлять передачу заявки в МойСклад")]


class MoySkladWebhookInbox(models.Model):
    account_id = models.UUIDField()
    environment = models.CharField(max_length=32)
    request_id = models.UUIDField()
    payload_hash = models.CharField(max_length=64)
    events = models.JSONField(default=list)
    cursor = models.PositiveIntegerField(default=0)
    received_at = models.DateTimeField(default=timezone.now)
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "integrations"
        constraints = [models.UniqueConstraint(fields=("environment", "account_id", "request_id"), name="ms_inbox_request_unique")]
        indexes = [models.Index(fields=("account_id", "environment", "processed_at"), name="ms_inbox_pending_idx")]


class MoySkladApiState(models.Model):
    """One transport budget and pause state for the entire external account."""

    account_id = models.UUIDField(unique=True)
    api_next_at = models.DateTimeField(null=True, blank=True)
    api_lease_token = models.UUIDField(null=True, blank=True)
    api_lease_expires_at = models.DateTimeField(null=True, blank=True)
    api_paused_until = models.DateTimeField(null=True, blank=True)
    access_error_code = models.CharField(max_length=64, blank=True)
    access_revision = models.PositiveBigIntegerField(default=0)
    export_paused = models.BooleanField(default=False)
    export_pause_revision = models.PositiveBigIntegerField(default=0)

    class Meta:
        app_label = "integrations"
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(api_lease_token__isnull=True, api_lease_expires_at__isnull=True)
                    | Q(api_lease_token__isnull=False, api_lease_expires_at__isnull=False)
                ),
                name="ms_account_api_lease_consistent",
            ),
        ]


class MoySkladSyncState(models.Model):
    account_id = models.UUIDField()
    environment = models.CharField(max_length=32)
    namespace = models.UUIDField(default=uuid.uuid4)
    export_started_at = models.DateTimeField(null=True, blank=True)
    export_consent_revision = models.ForeignKey(
        "content.ConsentRevision", null=True, blank=True, on_delete=models.PROTECT,
        related_name="moysklad_export_approvals",
    )
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    status_next_at = models.DateTimeField(null=True, blank=True)
    prices_next_at = models.DateTimeField(null=True, blank=True)
    webhook_id = models.UUIDField(null=True, blank=True)
    webhook_received_at = models.DateTimeField(null=True, blank=True)
    webhook_processed_at = models.DateTimeField(null=True, blank=True)
    prices_reconciled_at = models.DateTimeField(null=True, blank=True)
    prices_enabled_at = models.DateTimeField(null=True, blank=True)
    catalog_enabled = models.BooleanField(null=True, blank=True)
    prices_enabled = models.BooleanField(null=True, blank=True)
    export_enabled = models.BooleanField(null=True, blank=True)
    statuses_enabled = models.BooleanField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "integrations"
        constraints = [
            models.UniqueConstraint(fields=("account_id", "environment"), name="ms_sync_target_unique"),
        ]
        permissions = [
            ("manage_moysklad", "Может управлять обменом с МойСклад"),
            ("sync_moysklad_catalog", "Может запускать сверку каталога МойСклад"),
            ("reconcile_moysklad_prices", "Может запускать сверку цен МойСклад"),
        ]


class MoySkladSyncRun(models.Model):
    class Kind(models.TextChoices):
        CATALOG = "catalog", "Каталог"
        PRICES = "prices", "Цены"
        STATUS = "status", "Статусы"

    class Status(models.TextChoices):
        RUNNING = "running", "Выполняется"
        SUCCESS = "success", "Успешно"
        PARTIAL = "partial", "Частично"
        FAILED = "failed", "Ошибка"

    class Reason(models.TextChoices):
        MANUAL = "manual", "Вручную"
        SCHEDULED = "scheduled", "По расписанию"
        RECOVERY = "recovery", "Восстановление"

    account_id = models.UUIDField()
    environment = models.CharField(max_length=32)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    reason = models.CharField(max_length=16, choices=Reason.choices, default=Reason.MANUAL)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="moysklad_sync_runs")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RUNNING)
    total_count = models.PositiveIntegerField(default=0)
    processed_count = models.PositiveIntegerField(default=0)
    updated_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    cursor = models.JSONField(default=dict, blank=True)
    last_error_code = models.CharField(max_length=64, blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = "integrations"
        constraints = [
            models.CheckConstraint(condition=Q(kind__in=("catalog", "prices", "status")), name="ms_run_kind_valid"),
            models.CheckConstraint(condition=Q(reason__in=("manual", "scheduled", "recovery")), name="ms_run_reason_valid"),
            models.CheckConstraint(condition=Q(status__in=("running", "success", "partial", "failed")), name="ms_run_status_valid"),
        ]


class MoySkladTask(models.Model):
    class Kind(models.TextChoices):
        PRICE = "price", "Цена"
        EXPORT_ORDER = "export_order", "Передача заявки"
        DELETE_ORDER = "delete_order", "Удаление заявки"
        WEBHOOK = "webhook", "Обработка уведомления"
        CATALOG_SYNC = "catalog_sync", "Сверка каталога"
        PRICE_RECONCILIATION = "price_reconciliation", "Сверка цен"
        STATUS_SYNC = "status_sync", "Чтение статусов"
        CHECK_ACCESS = "check_access", "Проверка доступа"

    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает"
        PROCESSING = "processing", "Выполняется"
        DONE = "done", "Завершена"
        FAILED = "failed", "Ошибка"
        MANUAL_REVIEW = "manual_review", "Требует разбора"

    account_id = models.UUIDField()
    environment = models.CharField(max_length=32)
    kind = models.CharField(max_length=32, choices=Kind.choices)
    dedupe_key = models.CharField(max_length=255)
    object_id = models.CharField(max_length=64, blank=True)
    external_product = models.ForeignKey(MoySkladProduct, null=True, blank=True, on_delete=models.PROTECT, related_name="tasks")
    order_link = models.ForeignKey(MoySkladOrderLink, null=True, blank=True, on_delete=models.PROTECT, related_name="tasks")
    inbox = models.ForeignKey(MoySkladWebhookInbox, null=True, blank=True, on_delete=models.PROTECT, related_name="tasks")
    sync_run = models.ForeignKey(MoySkladSyncRun, null=True, blank=True, on_delete=models.PROTECT, related_name="tasks")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    force_apply = models.BooleanField(default=False)
    attempt_count = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    last_error_code = models.CharField(max_length=64, blank=True)
    first_error_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = "integrations"
        ordering = ("created_at", "pk")
        constraints = [
            models.UniqueConstraint(fields=("account_id", "environment", "kind", "dedupe_key"), condition=Q(status__in=("pending", "processing")), name="ms_task_active_unique"),
            models.UniqueConstraint(fields=("account_id", "environment", "kind", "dedupe_key"), condition=Q(kind__in=("export_order", "delete_order", "webhook")), name="ms_task_permanent_unique"),
            models.UniqueConstraint(fields=("order_link",), condition=Q(kind="export_order", order_link__isnull=False), name="ms_task_order_export_unique"),
            models.UniqueConstraint(fields=("order_link",), condition=Q(kind="delete_order", order_link__isnull=False), name="ms_task_order_delete_unique"),
            models.UniqueConstraint(fields=("inbox",), condition=Q(kind="webhook", inbox__isnull=False), name="ms_task_inbox_unique"),
            models.CheckConstraint(condition=Q(kind__in=("price", "export_order", "delete_order", "webhook", "catalog_sync", "price_reconciliation", "status_sync", "check_access")), name="ms_task_kind_valid"),
            models.CheckConstraint(condition=(Q(status="processing", lease_token__isnull=False, lease_expires_at__isnull=False) | Q(status__in=("pending", "done", "failed", "manual_review"), lease_token__isnull=True, lease_expires_at__isnull=True)), name="ms_task_state_lease_consistent"),
        ]
        indexes = [
            models.Index(fields=("account_id", "environment", "status", "next_attempt_at"), name="ms_task_ready_idx"),
            models.Index(fields=("status", "lease_expires_at"), name="ms_task_expired_idx"),
        ]


class MoySkladIncident(models.Model):
    account_id = models.UUIDField()
    environment = models.CharField(max_length=32)
    scope = models.CharField(max_length=64)
    code = models.CharField(max_length=64)
    object_ids = models.JSONField(default=list, blank=True)
    opened_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = "integrations"
        constraints = [models.UniqueConstraint(fields=("account_id", "environment", "scope", "code"), condition=Q(closed_at__isnull=True), name="ms_open_incident_unique")]
        permissions = [("close_moysklad_incident", "Может закрывать инциденты МойСклад")]
