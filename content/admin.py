from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import redirect
from unfold.admin import ModelAdmin, StackedInline

from catalog.models import Product

from .models import (
    ConsentRevision,
    ContentPage,
    HomepageRecommendation,
    SiteSettings,
    SiteLink,
)


@admin.register(ContentPage)
class ContentPageAdmin(ModelAdmin):
    list_display = ("page_name", "title", "updated_at")
    search_fields = ("title", "body", "seo_title", "seo_description")
    readonly_fields = ("key", "created_at", "updated_at")
    ordering = ("key",)
    fieldsets = (
        ("Страница", {"fields": ("key", "title", "created_at", "updated_at")}),
        ("Для поисковых систем", {"fields": ("seo_title", "seo_description"), "classes": ("tab",)}),
    )

    @admin.display(description="Страница", ordering="key")
    def page_name(self, obj):
        return obj.get_key_display()

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).exclude(key=ContentPage.Key.CONTACTS)

    def get_fieldsets(self, request, obj=None):
        if obj and obj.key != ContentPage.Key.PRIVACY:
            description = (
                "Этот текст отображается в разделе «О компании» на главной странице."
                if obj.key == ContentPage.Key.HOME else "Введите текст для этой страницы."
            )
            return (
                ("Содержимое страницы", {
                    "fields": ("body",),
                    "description": description + " Переносы строк и абзацы сохраняются.",
                }),
                ("Заголовок и поиск", {
                    "fields": ("title", "seo_title", "seo_description"),
                    "classes": ("collapse",),
                }),
            )
        return super().get_fieldsets(request, obj)

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if obj and obj.key == ContentPage.Key.PRIVACY:
            if "title" in form.base_fields:
                form.base_fields["title"].help_text = "Текст политики берётся из Markdown и редактируется разработчиком после юридической проверки."
            if "seo_title" in form.base_fields:
                form.base_fields["seo_title"].help_text = "Можно изменить заголовок страницы; сам юридический текст здесь не редактируется."
        return form


class SingletonAdmin(ModelAdmin):
    field_help_texts = {}

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).filter(pk=1)

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        for name, help_text in self.field_help_texts.items():
            if name in form.base_fields:
                form.base_fields[name].help_text = help_text
        return form


class SiteLinkInline(StackedInline):
    model = SiteLink
    fields = ("name", "url", "image")
    extra = 0

    def has_add_permission(self, request, obj=None):
        return request.user.has_perm("content.change_sitesettings")

    def has_change_permission(self, request, obj=None):
        return request.user.has_perm("content.change_sitesettings")

    def has_delete_permission(self, request, obj=None):
        return request.user.has_perm("content.change_sitesettings")

    def has_view_permission(self, request, obj=None):
        return request.user.has_perm("content.view_sitesettings") or self.has_change_permission(request, obj)

    def get_formset(self, request, obj=None, **kwargs):
        formset = super().get_formset(request, obj, **kwargs)
        formset.form.base_fields["url"].help_text = "Пустая ссылка не отображается на сайте. Для внешнего сайта укажите полный адрес https://…"
        formset.form.base_fields["image"].help_text = "Необязательный логотип: PNG, WebP или JPEG. Без изображения показывается название."
        return formset


@admin.register(SiteSettings)
class SiteSettingsAdmin(SingletonAdmin):
    inlines = (SiteLinkInline,)
    field_help_texts = {
        "opening_hours": "Переносы строк сохраняются. Пустое поле не показывает график.",
        "footer_notice": "Пустое поле скрывает дополнительную подпись.",
    }
    fieldsets = (
        ("Контакты", {"fields": ("phone_primary", "email", "address", "opening_hours")}),
        ("Подвал сайта", {"fields": ("footer_notice",)}),
    )


class RecommendationForm(forms.Form):
    first_product = forms.ModelChoiceField(queryset=Product.objects.none(), label="Первый товар", required=False)
    second_product = forms.ModelChoiceField(queryset=Product.objects.none(), label="Второй товар", required=False)

    def __init__(self, *args, current_products=None, **kwargs):
        super().__init__(*args, **kwargs)
        current_products = current_products or {1: None, 2: None}
        current_ids = {pk for pk in current_products.values() if pk}
        choices = Product.objects.filter(is_published=True).order_by("name", "pk")
        if current_ids:
            from django.db.models import Q

            choices = Product.objects.filter(Q(is_published=True) | Q(pk__in=current_ids)).order_by("name", "pk")
        for field_name, slot in (("first_product", 1), ("second_product", 2)):
            field = self.fields[field_name]
            field.queryset = choices
            field.initial = current_products.get(slot)
            field.help_text = "Выбирайте опубликованный товар. Скрытый товар остаётся в списке только пока закреплён здесь."

    def clean(self):
        cleaned = super().clean()
        first = cleaned.get("first_product")
        second = cleaned.get("second_product")
        current_ids = getattr(self, "current_products", {})
        if first and second and first.pk == second.pk:
            raise ValidationError("Один товар нельзя поставить в оба места.")
        for field_name, slot, product in (("first_product", 1, first), ("second_product", 2, second)):
            if product and not product.is_published and product.pk != current_ids.get(slot):
                self.add_error(field_name, "Скрытый товар нельзя выбрать заново. Сначала опубликуйте его.")
        return cleaned

    def set_current_products(self, current_products):
        self.current_products = current_products


@admin.register(HomepageRecommendation)
class HomepageRecommendationAdmin(ModelAdmin):
    change_list_template = "admin/content/homepage_recommendations.html"
    list_display = ("slot", "product")
    ordering = ("slot",)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj)

    def changelist_view(self, request, extra_context=None):
        if not request.user.has_perm("content.change_homepagerecommendation"):
            return super().changelist_view(request, extra_context)

        form = None
        with transaction.atomic():
            if request.method == "POST":
                for slot in HomepageRecommendation.Slot.values:
                    HomepageRecommendation.objects.get_or_create(slot=slot)
            rows = list(HomepageRecommendation.objects.select_for_update().order_by("slot"))
            current = {row.slot: row.product_id for row in rows}
            if request.method == "POST":
                form = RecommendationForm(request.POST, current_products=current)
                form.set_current_products(current)
                if form.is_valid():
                    selected = {
                        1: form.cleaned_data["first_product"],
                        2: form.cleaned_data["second_product"],
                    }
                    HomepageRecommendation.objects.filter(slot__in=(1, 2)).update(product=None)
                    for row in rows:
                        product = selected[row.slot]
                        row.product_id = product.pk if product else None
                        HomepageRecommendation.objects.filter(pk=row.pk).update(product_id=row.product_id)
                        if current.get(row.slot) != row.product_id:
                            self.log_change(request, row, "Сохранён выбор рекомендуемых товаров на главной странице.")
                    self.message_user(request, "Рекомендуемые товары сохранены.", messages.SUCCESS)
                    return redirect("admin:content_homepagerecommendation_changelist")
            else:
                form = RecommendationForm(current_products=current)
                form.set_current_products(current)

        context = {
            **self.admin_site.each_context(request),
            "title": "Рекомендуемые товары",
            "opts": self.model._meta,
            "form": form,
            "current_products": current,
            "recommendation_rows": rows,
            "save_label": "Сохранить рекомендации",
        }
        return self._render_recommendation_form(request, context)

    def _render_recommendation_form(self, request, context):
        from django.template.response import TemplateResponse

        return TemplateResponse(request, self.change_list_template, context)


@admin.register(ConsentRevision)
class ConsentRevisionAdmin(ModelAdmin):
    list_display = ("revision_id", "registered_at", "sha256")
    search_fields = ("revision_id", "sha256")
    readonly_fields = tuple(field.name for field in ConsentRevision._meta.fields)
    fields = readonly_fields
    ordering = ("-registered_at", "-pk")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and request.user.has_perm("content.view_consentrevision")

    def has_delete_permission(self, request, obj=None):
        return False
