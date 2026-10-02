from django.contrib import admin

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
