from django import forms
from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html
from unfold.admin import ModelAdmin

from fishsite.backoffice.forms import assignee_field_for

from .models import Inquiry


class InquiryAdminForm(forms.ModelForm):
    class Meta:
        model = Inquiry
        fields = ("internal_notes", "assigned_to")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assigned_to"] = assignee_field_for(self.instance)


@admin.register(Inquiry)
class InquiryAdmin(ModelAdmin):
    form = InquiryAdminForm
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
        ("Работа менеджера", {"fields": ("assigned_to", "internal_notes"), "classes": ("tab",)}),
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

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("privacy_consent_revision", "assigned_to")
