from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.core import signing
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.forms.models import BaseInlineFormSet
from django.shortcuts import render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.html import format_html
from unfold.admin import ModelAdmin, TabularInline

from integrations.moysklad.config import get_config
from integrations.moysklad.contracts import safe_error_code

from .models import Category, Product, ProductImage
from .pricing import PriceRevisionConflict, change_product_price


PRICE_BASELINE_SALT = "catalog.product.price-baseline.v1"


class ProductHasPhotosFilter(admin.SimpleListFilter):
    title = "Фотографии"
    parameter_name = "photos"

    def lookups(self, request, model_admin):
        return (("yes", "С фото"), ("no", "Без фото"))

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(images__isnull=False).distinct()
        if self.value() == "no":
            return queryset.filter(images__isnull=True)
        return queryset


class ProductMoySkladFilter(admin.SimpleListFilter):
    title = "МойСклад"
    parameter_name = "moysklad"

    def lookups(self, request, model_admin):
        return (("unlinked", "Нет связи"), ("blocked", "Заблокированы"), ("pending", "Ожидают цену"), ("audit", "Опубликованы, нужна проверка"))

    def queryset(self, request, queryset):
        if not self.value():
            return queryset
        try:
            config = get_config()
        except ImproperlyConfigured:
            return queryset.none()
        if not config.account_id:
            return queryset.none()
        if self.value() == "unlinked":
            return queryset.filter(moysklad_product__isnull=True)
        if self.value() == "blocked":
            return queryset.filter(integration_block_reason__gt="", moysklad_product__account_id=config.account_id)
        if self.value() == "pending":
            return queryset.filter(moysklad_product__account_id=config.account_id, moysklad_product__price_pending_since__isnull=False)
        if self.value() == "audit":
            return queryset.filter(is_published=True).filter(
                Q(moysklad_product__isnull=True) | Q(integration_block_reason__gt="", moysklad_product__account_id=config.account_id)
            )
        return queryset


class ProductAdminForm(forms.ModelForm):
    price_baseline = forms.CharField(required=False, widget=forms.HiddenInput)

    class Meta:
        model = Product
        fields = (
            "name", "slug", "category", "short_description", "description", "price_per_kg",
            "display_order", "is_published", "seo_title", "seo_description", "price_baseline",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.price_baseline_data = None
        self.price_change_requested = False
        if not self.is_bound and self.instance.pk:
            self.initial["price_baseline"] = signing.dumps(
                {"pk": self.instance.pk, "revision": self.instance.price_revision, "price": str(self.instance.price_per_kg)},
                salt=PRICE_BASELINE_SALT,
            )
        if "price_per_kg" in self.fields:
            self.fields["price_per_kg"].help_text = (
                "Новая внешняя цена или принудительная сверка заменит ручное значение. "
                "Обычное чтение неизменной внешней цены сохраняет его."
            )

    def clean(self):
        cleaned_data = super().clean()
        if not self.instance.pk or "price_per_kg" not in self.fields:
            return cleaned_data

        try:
            baseline = signing.loads(cleaned_data.get("price_baseline", ""), salt=PRICE_BASELINE_SALT)
            baseline_pk = int(baseline["pk"])
            baseline_revision = int(baseline["revision"])
            baseline_price = Decimal(baseline["price"])
            if baseline_pk != self.instance.pk or not baseline_price.is_finite():
                raise ValueError
        except (signing.BadSignature, KeyError, TypeError, ValueError, InvalidOperation):
            self.add_error("price_per_kg", "Форма устарела или повреждена. Обновите страницу и повторите ввод.")
            return cleaned_data

        self.price_baseline_data = {"revision": baseline_revision, "price": baseline_price}
        submitted_price = cleaned_data.get("price_per_kg")
        if submitted_price is None:
            return cleaned_data

        self.price_change_requested = submitted_price != baseline_price
        current_is_baseline = (
            self.instance.price_revision == baseline_revision
            and self.instance.price_per_kg == baseline_price
        )
        if not current_is_baseline and self.price_change_requested:
            self.add_error(
                "price_per_kg",
                f"Цена изменилась после открытия формы. Сейчас: {self.instance.price_per_kg} ₽/кг. "
                "Обновите страницу и внесите изменение ещё раз.",
            )
        elif not current_is_baseline:
            # Preserve the latest price when only editorial fields were changed.
            cleaned_data["price_per_kg"] = self.instance.price_per_kg
        return cleaned_data


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


class ProductImageInline(TabularInline):
    model = ProductImage
    formset = ProductImageInlineFormSet
    fields = ("thumbnail", "image", "alt_text", "display_order", "is_primary")
    readonly_fields = ("thumbnail",)
    extra = 0
    ordering = ("display_order", "pk")

    @admin.display(description="Фото")
    def thumbnail(self, obj):
        if not obj or not obj.image:
            return "—"
        return format_html('<img class="backoffice-thumbnail" src="{}" alt="">', obj.image.url)

    def get_queryset(self, request):
        return super().get_queryset(request).order_by("display_order", "pk")


@admin.register(Category)
class CategoryAdmin(ModelAdmin):
    list_display = ("name", "display_order", "product_count")
    search_fields = ("name", "slug")
    ordering = ("display_order", "name")
    list_per_page = 50
    fieldsets = (
        ("Категория", {"fields": ("name", "slug", "description", "image", "display_order")}),
        ("Для поисковых систем", {"fields": ("seo_title", "seo_description"), "classes": ("tab",)}),
    )

    @admin.display(description="Товаров", ordering="_product_count")
    def product_count(self, obj):
        return obj._product_count

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_product_count=Count("products"))


@admin.register(Product)
class ProductAdmin(ModelAdmin):
    form = ProductAdminForm
    list_display = ("thumbnail", "name", "category", "price_per_kg", "price_source_display", "is_published", "display_order", "site_link")
    list_filter = ("is_published", "category", ProductHasPhotosFilter, ProductMoySkladFilter)
    search_fields = ("name", "slug", "short_description", "description", "category__name", "moysklad_product__name", "moysklad_product__code", "moysklad_product__article")
    autocomplete_fields = ("category",)
    inlines = (ProductImageInline,)
    ordering = ("display_order", "name", "pk")
    list_per_page = 50
    actions = ("publish_selected", "hide_selected")
    fieldsets = (
        ("Описание товара", {"fields": ("name", "slug", "category", "short_description", "description")}),
        ("Цена за килограмм", {"fields": ("price_per_kg", "price_baseline", "price_changed_at", "price_source"), "classes": ("tab",)}),
        ("Показ на сайте", {"fields": ("is_published", "display_order"), "classes": ("tab",)}),
        ("Поисковая оптимизация", {"fields": ("seo_title", "seo_description"), "classes": ("tab",)}),
        ("Интеграция", {"fields": ("moysklad_status",), "classes": ("tab",)}),
    )
    readonly_fields = ("price_changed_at", "price_source", "moysklad_status")

    @admin.display(description="МойСклад")
    def moysklad_status(self, obj):
        if not obj or not obj.pk:
            return "Сначала сохраните карточку сайта."
        try:
            config = get_config()
        except ImproperlyConfigured:
            return "Серверная конфигурация обмена требует проверки."
        source = getattr(obj, "moysklad_product", None)
        if source is not None and source.account_id != config.account_id:
            source = None
        return render_to_string("admin/moysklad/product_panel.html", {
            "product": obj,
            "source": ({
                "id": source.external_id, "name": source.name, "code": source.code,
                "article": source.article, "unit": (
                    "Не указана" if source.unit_id is None
                    else "кг" if source.unit_id == config.unit_id
                    else source.unit_name or "Другая единица"
                ),
                "price": source.last_checked_price, "checked_at": source.price_checked_at,
                "pending_since": source.price_pending_since,
                "error": safe_error_code(source.last_error_code) if source.last_error_code else "",
            } if source else None),
            "block_reason": safe_error_code(obj.integration_block_reason) if obj.integration_block_reason else "",
        })

    @admin.display(description="Ссылка на сайт")
    def site_link(self, obj):
        if obj.is_published and not obj.integration_block_reason:
            url, label = reverse("catalog:product", args=(obj.slug,)), "Открыть"
        else:
            url, label = reverse("admin:product_preview", args=(obj.pk,)), "Предпросмотр"
        return format_html('<a href="{}">{}</a>', url, label)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("category", "moysklad_product").prefetch_related("images")

    @admin.display(description="Фото")
    def thumbnail(self, obj):
        photo = obj.primary_photo
        if not photo:
            return "—"
        return format_html('<img class="backoffice-thumbnail" src="{}" alt="">', photo.url)

    @admin.display(description="Источник цены", ordering="price_source")
    def price_source_display(self, obj):
        return obj.get_price_source_display()

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if not request.user.has_perm("catalog.change_product_price"):
            readonly.append("price_per_kg")
        return tuple(readonly)

    def get_exclude(self, request, obj=None):
        excluded = list(super().get_exclude(request, obj) or ())
        excluded.extend(("minimum_weight", "weight_step"))
        if not request.user.has_perm("catalog.change_product_price"):
            excluded.append("price_baseline")
        return tuple(excluded)

    def has_add_permission(self, request):
        return super().has_add_permission(request) and request.user.has_perm("catalog.change_product_price")

    def has_delete_permission(self, request, obj=None):
        return False

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        if request.method == "POST" and object_id:
            with transaction.atomic():
                try:
                    product_id = int(object_id)
                except (ValueError, TypeError):
                    return super().changeform_view(request, object_id, form_url, extra_context)
                if Product.objects.select_for_update().filter(pk=product_id).exists():
                    return super().changeform_view(request, object_id, form_url, extra_context)
        return super().changeform_view(request, object_id, form_url, extra_context)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.price_changed_by = request.user
            super().save_model(request, obj, form, change)
            return

        changed_fields = set(form.changed_data)
        wants_price_change = bool(form.price_change_requested)
        changed_fields.discard("price_per_kg")
        changed_fields.discard("price_baseline")
        if changed_fields:
            obj.save(update_fields=changed_fields)
        if wants_price_change:
            try:
                change_product_price(
                    product_id=obj.pk,
                    price=form.cleaned_data["price_per_kg"],
                    actor=request.user,
                    expected_revision=form.price_baseline_data["revision"],
                )
            except PriceRevisionConflict as exc:
                raise ValidationError(
                    {"price_per_kg": f"Цена уже изменилась. Текущая цена: {exc.product.price_per_kg} ₽/кг."}
                ) from exc

    def save_formset(self, request, form, formset, change):
        if formset.model is ProductImage:
            # Delete/demote old primary rows before promoting a new one under
            # the unique partial index, inside the parent admin transaction.
            images = formset.save(commit=False)
            for deleted in formset.deleted_objects:
                deleted.delete()
            for image in sorted(images, key=lambda item: item.is_primary):
                image.save()
            formset.save_m2m()
            return
        return super().save_formset(request, form, formset, change)

    @admin.action(description="Опубликовать выбранные товары", permissions=["change"])
    def publish_selected(self, request, queryset):
        updated = self._set_publication(request, queryset, published=True)
        self.message_user(request, f"Опубликовано товаров: {updated}.", messages.SUCCESS)

    @admin.action(description="Скрыть выбранные товары", permissions=["change"])
    def hide_selected(self, request, queryset):
        if request.POST.get("confirm_hide") == "yes":
            updated = self._set_publication(request, queryset, published=False)
            self.message_user(request, f"Скрыто товаров: {updated}.", messages.SUCCESS)
            return None
        return render(
            request,
            "admin/catalog/confirm_hide_products.html",
            {
                **self.admin_site.each_context(request),
                "title": "Подтвердите скрытие товаров",
                "products": queryset,
                "action_checkbox_name": ACTION_CHECKBOX_NAME,
                "selected_ids": [str(pk) for pk in queryset.values_list("pk", flat=True)],
                "action_name": "hide_selected",
                "select_across": request.POST.get("select_across", "0"),
            },
        )

    def _set_publication(self, request, queryset, *, published):
        label = "Опубликован из списка каталога." if published else "Скрыт из списка каталога."
        with transaction.atomic(using=queryset.db):
            products = list(queryset.select_for_update())
            changed = 0
            for product in products:
                if product.is_published == published:
                    continue
                Product.objects.using(queryset.db).filter(pk=product.pk).update(is_published=published)
                product.is_published = published
                self.log_change(request, product, label)
                changed += 1
        return changed
