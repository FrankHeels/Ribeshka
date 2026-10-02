from django.contrib import admin
from django.core.exceptions import ValidationError
from django.forms.models import BaseInlineFormSet

from .models import Category, Product, ProductImage


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "display_order")
    search_fields = ("name", "slug")
    ordering = ("display_order", "name")
    list_per_page = 50


class ProductImageInlineFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        primary_count = sum(
            bool(form.cleaned_data.get("is_primary"))
            for form in self.forms
            if form.cleaned_data and not form.cleaned_data.get("DELETE", False)
        )
        if primary_count > 1:
            raise ValidationError("Отметьте основным только одно фото товара.")


class ProductImageInline(admin.TabularInline):
    model = ProductImage
    formset = ProductImageInlineFormSet
    fields = ("image", "alt_text", "display_order", "is_primary")
    extra = 0
    ordering = ("display_order", "pk")

    def get_queryset(self, request):
        return super().get_queryset(request).order_by("display_order", "pk")


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "price_per_kg", "is_published")
    list_filter = ("is_published", "category")
    search_fields = ("name", "slug", "short_description", "description", "category__name")
    autocomplete_fields = ("category",)
    readonly_fields = ("id",)
    inlines = (ProductImageInline,)
    ordering = ("name",)
    list_editable = ("is_published",)
    list_per_page = 50

    def save_formset(self, request, form, formset, change):
        if formset.model is not ProductImage:
            return super().save_formset(request, form, formset, change)
        # The admin change view already wraps the parent and its inlines in
        # one transaction. Delete/demote the old primary before promoting the
        # new one, regardless of the rows' display order. Unchanged rows stay
        # untouched, including an unchanged primary image.
        images = formset.save(commit=False)
        for deleted in formset.deleted_objects:
            deleted.delete()
        for image in sorted(images, key=lambda item: item.is_primary):
            image.save()
        formset.save_m2m()
