from django.contrib import admin, messages
from django.db import connections, transaction
from django.utils import timezone

from .models import NotificationOutbox


@admin.register(NotificationOutbox)
class NotificationOutboxAdmin(admin.ModelAdmin):
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
