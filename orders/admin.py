from django.contrib import admin
from django.core.exceptions import ImproperlyConfigured
from django.db.models import Prefetch, Sum
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.html import format_html
from unfold.admin import ModelAdmin, TabularInline

from integrations.models import MoySkladTask
from integrations.moysklad.config import get_config
from integrations.moysklad.contracts import safe_error_code

from .models import Order, OrderItem


class OrderItemsInline(TabularInline):
    model = OrderItem
    fields = (
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

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Order)
class OrderAdmin(ModelAdmin):
    list_display = ("order_number", "name", "phone", "preliminary_total", "fulfillment_method", "created_at")
    list_filter = ("fulfillment_method", "created_at")
    search_fields = ("order_number", "name", "phone", "email")
    readonly_fields = (
        "order_number", "created_at", "name", "phone", "email", "fulfillment_method",
        "delivery_address", "comment", "privacy_consent", "privacy_consent_at",
        "consent_revision_archive", "moysklad_exchange",
    )
    fieldsets = (
        ("Заявка", {"fields": ("order_number", "created_at", ("name", "phone"), "email", "fulfillment_method", "delivery_address", "comment")}),
        ("Согласие", {"fields": ("privacy_consent", "privacy_consent_at", "consent_revision_archive"), "classes": ("tab",)}),
        ("МойСклад", {"fields": ("moysklad_exchange",), "classes": ("tab",)}),
    )
    inlines = (OrderItemsInline,)
    ordering = ("-created_at", "-pk")
    date_hierarchy = "created_at"
    actions = None

    @admin.display(description="Передача и учётный статус")
    def moysklad_exchange(self, obj):
        if not obj or not obj.pk:
            return "Заявка ещё не сохранена."
        try:
            config = get_config()
        except ImproperlyConfigured:
            return "Серверная конфигурация обмена требует проверки."
        link = getattr(obj, "moysklad_link", None)
        if link is None or link.account_id != config.account_id or link.environment != config.environment:
            return "Для этой заявки нет передачи в текущем окружении."
        tasks = getattr(link, "backoffice_export_tasks", ())
        task = tasks[0] if tasks else None
        return render_to_string("admin/moysklad/order_panel.html", {
            "link_id": link.pk, "account_id": link.account_id, "environment": link.environment,
            "external_id": link.external_id, "exported_at": link.exported_at,
            "state_name": link.state_name, "state_type": link.state_type,
            "status_checked_at": link.status_checked_at,
            "error_code": safe_error_code(link.last_error_code) if link.last_error_code else "",
            "task_status": task.get_status_display() if task else "Задача ещё не создана",
            "task_error": safe_error_code(task.last_error_code) if task and task.last_error_code else "",
            "next_attempt_at": task.next_attempt_at if task and task.status in ("pending", "processing") else None,
            "can_override": not link.attempted and not link.external_id,
            "can_resolve": link.attempted and not link.external_id,
        })

    @admin.display(description="Предварительная сумма", ordering="preliminary_total")
    def preliminary_total(self, obj):
        if obj.preliminary_total is None:
            return "—"
        return f"{obj.preliminary_total:.2f} ₽"

    @admin.display(description="Архив согласия")
    def consent_revision_archive(self, obj):
        revision = obj.privacy_consent_revision
        if revision is None:
            return "Не зафиксирована (старая запись)"
        return format_html(
            '<a href="{}">Открыть архивную редакцию</a>',
            reverse("admin:content_consentrevision_change", args=(revision.pk,)),
        )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return (
            super().get_queryset(request)
            .select_related("privacy_consent_revision", "moysklad_link")
            .prefetch_related(Prefetch("moysklad_link__tasks", queryset=MoySkladTask.objects.filter(kind="export_order").order_by("-created_at", "-pk"), to_attr="backoffice_export_tasks"))
            .annotate(preliminary_total=Sum("items__line_total"))
        )
