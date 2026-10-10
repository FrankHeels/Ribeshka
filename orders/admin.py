from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from .models import Order, OrderItem


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    fields = (
        "product",
        "product_name_snapshot",
        "price_per_kg_snapshot",
        "desired_weight",
        "line_total",
    )
    readonly_fields = fields
    extra = 0
    can_delete = False
    ordering = ("pk",)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("order_number", "name", "phone", "fulfillment_method", "consent_revision_archive", "created_at")
    list_filter = ("fulfillment_method", "created_at")
    search_fields = ("order_number", "name", "phone", "email")
    readonly_fields = tuple(
        field.name for field in Order._meta.fields if field.name != "privacy_consent_revision"
    ) + ("consent_revision_archive",)
    fields = readonly_fields
    inlines = (OrderItemInline,)
    ordering = ("-created_at",)
    date_hierarchy = "created_at"

    @admin.display(description="Архивная редакция согласия")
    def consent_revision_archive(self, obj):
        revision = obj.privacy_consent_revision
        if revision is None:
            return "Не зафиксирована (старая запись)"
        return format_html(
            '<a href="{}">{}</a>',
            reverse("admin:content_consentrevision_change", args=(revision.pk,)),
            revision.revision_id,
        )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("privacy_consent_revision")
