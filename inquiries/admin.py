from django.contrib import admin

from .models import Inquiry


@admin.register(Inquiry)
class InquiryAdmin(admin.ModelAdmin):
    list_display = ("inquiry_number", "contact_name", "company", "phone", "created_at")
    list_filter = ("created_at",)
    search_fields = ("inquiry_number", "contact_name", "company", "phone", "email")
    readonly_fields = tuple(field.name for field in Inquiry._meta.fields)
    fields = readonly_fields
    ordering = ("-created_at",)
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
