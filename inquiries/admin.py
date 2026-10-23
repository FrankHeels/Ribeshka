from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html
from unfold.admin import ModelAdmin
from fishsite.backoffice.request_views import RequestDeletionAdminMixin

from .models import Inquiry


@admin.register(Inquiry)
class InquiryAdmin(RequestDeletionAdminMixin, ModelAdmin):
    change_form_template = "admin/inquiries/inquiry/change_form.html"
    list_display = ("inquiry_number", "contact_name", "company", "phone", "created_at")
    list_filter = ("created_at",)
    search_fields = ("inquiry_number", "contact_name", "company", "phone", "email")
    readonly_fields = (
        "inquiry_number", "created_at", "contact_name", "company", "phone", "email", "need",
        "privacy_consent", "privacy_consent_at", "consent_revision_archive",
    )
    fieldsets = (
        ("Запрос", {"fields": ("inquiry_number", "created_at", ("contact_name", "company"), "phone", "email", "need")}),
        ("Согласие", {"fields": ("privacy_consent", "privacy_consent_at", "consent_revision_archive"), "classes": ("tab",)}),
    )
    ordering = ("-created_at", "-pk")
    date_hierarchy = "created_at"

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

    @staticmethod
    def request_deletion_service(object_id, *, actor):
        from .services import delete_inquiry

        return delete_inquiry(object_id, actor=actor)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("privacy_consent_revision")
