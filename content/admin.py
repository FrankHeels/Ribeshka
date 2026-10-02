from django import forms
from django.contrib import admin

from .models import ContentPage, ContentSection, HOME_SECTION_LABELS


class ContentSectionInlineForm(forms.ModelForm):
    class Meta:
        model = ContentSection
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and "key" in self.fields:
            self.fields["key"].disabled = True


class ContentSectionInline(admin.StackedInline):
    model = ContentSection
    form = ContentSectionInlineForm
    fields = ("key", "title", "body", "display_order", "updated_at")
    readonly_fields = ("updated_at",)
    extra = 0
    ordering = ("display_order", "pk")

    @admin.display(description="Фиксированный блок")
    def block_label(self, obj):
        return HOME_SECTION_LABELS.get(obj.key, "Текстовый блок")

    def get_fields(self, request, obj=None):
        if obj and obj.key == ContentPage.Key.HOME:
            return ("block_label", "title", "body", "display_order", "updated_at")
        return super().get_fields(request, obj)

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.key == ContentPage.Key.HOME:
            return ("block_label", "updated_at")
        return super().get_readonly_fields(request, obj)

    def has_add_permission(self, request, obj=None):
        if obj and obj.key == ContentPage.Key.HOME:
            return False
        return super().has_add_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj and obj.key == ContentPage.Key.HOME:
            return False
        return super().has_delete_permission(request, obj)


@admin.register(ContentPage)
class ContentPageAdmin(admin.ModelAdmin):
    list_display = ("page_name", "title", "updated_at")
    search_fields = ("title", "seo_title", "seo_description")
    readonly_fields = ("key", "created_at", "updated_at")
    fields = ("key", "title", "seo_title", "seo_description", "created_at", "updated_at")
    inlines = (ContentSectionInline,)
    ordering = ("key",)

    @admin.display(description="Страница", ordering="key")
    def page_name(self, obj):
        return obj.get_key_display()

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("sections")
