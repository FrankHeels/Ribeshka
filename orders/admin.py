from django.contrib import admin

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
    list_display = ("order_number", "name", "phone", "fulfillment_method", "created_at")
    list_filter = ("fulfillment_method", "created_at")
    search_fields = ("order_number", "name", "phone", "email")
    readonly_fields = tuple(field.name for field in Order._meta.fields)
    fields = readonly_fields
    inlines = (OrderItemInline,)
    ordering = ("-created_at",)
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
