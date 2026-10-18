from django.contrib import admin, messages
from django.core.exceptions import ImproperlyConfigured
from django.db import connections, transaction
from django.utils import timezone
from unfold.admin import ModelAdmin

from .models import (
    NotificationOutbox,
    MoySkladApiState, MoySkladIncident, MoySkladOrderLink, MoySkladProduct,
    MoySkladSyncRun, MoySkladSyncState, MoySkladTask, MoySkladWebhookInbox,
)
from .moysklad.config import get_config
from .moysklad.contracts import safe_error_code


@admin.register(NotificationOutbox)
class NotificationOutboxAdmin(ModelAdmin):
    list_display = (
        "id",
        "request_number",
        "status_display",
        "attempt_count",
        "next_attempt_at",
        "lease_expires_at",
        "created_at",
        "sent_at",
    )
    list_filter = ("status", "created_at")
    search_fields = ("order__order_number", "inquiry__inquiry_number", "last_error")
    readonly_fields = tuple(field.name for field in NotificationOutbox._meta.fields)
    fields = readonly_fields
    ordering = ("created_at",)
    date_hierarchy = "created_at"
    actions = ("retry_manual_review",)

    @admin.action(description="Повторить задачи, требующие разбора", permissions=("change",))
    def retry_manual_review(self, request, queryset):
        using = queryset.db
        ids = list(queryset.values_list("pk", flat=True))
        retried = 0
        for outbox_id in ids:
            with transaction.atomic(using=using):
                row = (
                    NotificationOutbox.objects.using(using)
                    .select_for_update()
                    .filter(pk=outbox_id, status=NotificationOutbox.Status.MANUAL_REVIEW)
                    .first()
                )
                if row is None:
                    continue
                now = timezone.now()
                row.status = NotificationOutbox.Status.PENDING
                row.attempt_count = 0
                row.last_error = ""
                row.next_attempt_at = now
                row.lease_expires_at = None
                row.lease_token = None
                row.sent_at = None
                row.save(
                    using=using,
                    update_fields=(
                        "status", "attempt_count", "last_error", "next_attempt_at",
                        "lease_expires_at", "lease_token", "sent_at",
                    ),
                )
                with connections[using].cursor() as cursor:
                    cursor.execute("SELECT pg_notify(%s, %s)", ("notification_outbox", ""))
                self.log_change(request, row, "Задача уведомления возвращена в очередь сотрудником.")
                retried += 1

        if retried:
            self.message_user(request, f"В очередь возвращено задач: {retried}.", level=messages.SUCCESS)
        else:
            self.message_user(request, "Нет задач в состоянии «Требует разбора».", level=messages.WARNING)

    @admin.display(description="Заявка")
    def request_number(self, obj):
        if obj.order_id:
            return obj.order.order_number
        if obj.inquiry_id:
            return obj.inquiry.inquiry_number
        return "—"

    @admin.display(description="Состояние")
    def status_display(self, obj):
        return obj.get_status_display()

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# Exchange records are diagnostics, never an alternative write path around services.
class MoySkladReadOnlyAdmin(ModelAdmin):
    actions = None
    list_per_page = 50
    target_has_environment = True

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        try:
            config = get_config()
        except ImproperlyConfigured:
            return queryset.none()
        if not config.account_id:
            return queryset.none()
        target = {"account_id": config.account_id}
        if self.target_has_environment:
            target["environment"] = config.environment
        return queryset.filter(**target)

    @admin.display(description="Безопасный код ошибки")
    def safe_last_error(self, obj):
        return safe_error_code(obj.last_error_code) if obj.last_error_code else "—"


@admin.register(MoySkladProduct)
class MoySkladProductAdmin(MoySkladReadOnlyAdmin):
    target_has_environment = False
    list_display = ("name", "code", "external_id", "product", "unit_name", "last_checked_price", "price_checked_at", "safe_last_error")
    search_fields = ("name", "code", "article", "external_id", "product__name")
    list_filter = ("archived", "is_deferred")
    fields = readonly_fields = (
        "account_id", "external_id", "entity_type", "product", "name", "code", "article",
        "unit_id", "unit_name", "archived", "deleted_at", "is_deferred", "last_checked_price",
        "price_checked_at", "price_pending_since", "price_requested_revision", "price_processed_revision", "safe_last_error",
    )


@admin.register(MoySkladOrderLink)
class MoySkladOrderLinkAdmin(MoySkladReadOnlyAdmin):
    list_display = ("order", "external_id", "exported_at", "state_name", "status_checked_at", "safe_last_error")
    fields = readonly_fields = (
        "order", "account_id", "environment", "sync_id", "external_id", "attempted",
        "exported_at", "state_name", "state_type", "status_checked_at", "safe_last_error",
        "override_at", "override_by",
    )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("order", "override_by")


class MoySkladTechnicalAdmin(MoySkladReadOnlyAdmin):
    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_staff and request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request) and super().has_view_permission(request, obj)


@admin.register(MoySkladTask)
class MoySkladTaskAdmin(MoySkladTechnicalAdmin):
    list_display = ("id", "kind", "status", "object_id", "attempt_count", "next_attempt_at", "safe_last_error")
    list_filter = ("kind", "status")
    fields = readonly_fields = (
        "id", "account_id", "environment", "kind", "status", "object_id", "attempt_count",
        "next_attempt_at", "lease_expires_at", "created_at", "completed_at", "safe_last_error",
    )


@admin.register(MoySkladSyncRun)
class MoySkladSyncRunAdmin(MoySkladTechnicalAdmin):
    list_display = ("id", "kind", "reason", "status", "processed_count", "total_count", "started_at")
    list_filter = ("kind", "status", "reason")
    fields = readonly_fields = (
        "id", "account_id", "environment", "kind", "reason", "actor", "status",
        "total_count", "processed_count", "updated_count", "skipped_count", "error_count",
        "started_at", "completed_at", "safe_last_error",
    )


@admin.register(MoySkladSyncState)
class MoySkladSyncStateAdmin(MoySkladTechnicalAdmin):
    list_display = ("account_id", "environment", "heartbeat_at", "prices_reconciled_at", "export_started_at")
    fields = readonly_fields = (
        "account_id", "environment", "heartbeat_at", "export_started_at", "webhook_id",
        "webhook_received_at", "webhook_processed_at", "prices_reconciled_at", "prices_next_at",
        "catalog_enabled", "prices_enabled", "export_enabled", "statuses_enabled",
    )


@admin.register(MoySkladApiState)
class MoySkladApiStateAdmin(MoySkladTechnicalAdmin):
    target_has_environment = False
    list_display = ("account_id", "api_paused_until", "safe_access_error", "export_paused")
    fields = readonly_fields = ("account_id", "api_paused_until", "safe_access_error", "export_paused")

    @admin.display(description="Ошибка доступа")
    def safe_access_error(self, obj):
        return safe_error_code(obj.access_error_code) if obj.access_error_code else "—"


@admin.register(MoySkladWebhookInbox)
class MoySkladWebhookInboxAdmin(MoySkladTechnicalAdmin):
    list_display = ("id", "request_id", "received_at", "processed_at")
    fields = readonly_fields = ("id", "account_id", "environment", "request_id", "received_at", "processed_at", "cursor")


@admin.register(MoySkladIncident)
class MoySkladIncidentAdmin(MoySkladTechnicalAdmin):
    list_display = ("id", "safe_scope", "safe_code", "opened_at", "last_seen_at", "closed_at")
    fields = readonly_fields = ("id", "account_id", "environment", "safe_scope", "safe_code", "opened_at", "last_seen_at", "closed_at")
    list_filter = ("closed_at",)

    @admin.display(description="Область")
    def safe_scope(self, obj):
        return safe_error_code(obj.scope)

    @admin.display(description="Код")
    def safe_code(self, obj):
        return safe_error_code(obj.code)
