from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from .validators import validate_site_image


class Category(models.Model):
    name = models.CharField("Название", max_length=160)
    slug = models.SlugField("Адрес", max_length=180, unique=True)
    description = models.TextField("Описание", blank=True)
    image = models.ImageField("Изображение", upload_to="catalog/categories/", blank=True, validators=[validate_site_image])
    display_order = models.PositiveIntegerField("Порядок показа", default=0)
    seo_title = models.CharField("SEO-заголовок", max_length=180, blank=True)
    seo_description = models.CharField("SEO-описание", max_length=320, blank=True)

    class Meta:
        verbose_name = "категория"
        verbose_name_plural = "категории"
        ordering = ("display_order", "name", "pk")

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class ProductQuerySet(models.QuerySet):
    def published(self):
        """Return only products eligible for future public handlers."""
        return self.filter(is_published=True, integration_block_reason="")


class Product(models.Model):
    class PriceSource(models.TextChoices):
        MANUAL = "manual", "Админка"
        MOYSKLAD = "moysklad", "МойСклад"

    name = models.CharField("Название", max_length=180)
    slug = models.SlugField("Адрес", max_length=200, unique=True)
    category = models.ForeignKey(
        Category,
        verbose_name="Категория",
        related_name="products",
        on_delete=models.PROTECT,
    )
    short_description = models.CharField("Краткое описание", max_length=240, blank=True)
    description = models.TextField("Описание", blank=True)
    price_per_kg = models.DecimalField(
        "Цена за кг",
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    minimum_weight = models.DecimalField(
        "Минимальный вес, кг",
        max_digits=8,
        decimal_places=3,
        default=Decimal("0.1"),
        validators=[MinValueValidator(Decimal("0.001"))],
    )
    weight_step = models.DecimalField(
        "Шаг веса, кг",
        max_digits=8,
        decimal_places=3,
        default=Decimal("0.1"),
        validators=[MinValueValidator(Decimal("0.001"))],
    )
    is_published = models.BooleanField("Опубликован", default=False, db_index=True)
    integration_block_reason = models.CharField("Причина блокировки МойСклад", max_length=64, blank=True)
    mapping_revision = models.PositiveIntegerField("Редакция связи с МойСклад", default=0, editable=False)
    display_order = models.PositiveIntegerField("Порядок показа", default=0)
    # Legacy stored flag; homepage hits are selected only by HomepageRecommendation.
    is_popular = models.BooleanField("Хит продаж", default=False, db_index=True)
    seo_title = models.CharField("SEO-заголовок", max_length=180, blank=True)
    seo_description = models.CharField("SEO-описание", max_length=320, blank=True)
    price_revision = models.PositiveIntegerField("Редакция цены", default=0, editable=False)
    price_changed_at = models.DateTimeField("Цена изменена", null=True, blank=True, editable=False)
    price_changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Кто изменил цену",
        null=True,
        blank=True,
        editable=False,
        on_delete=models.SET_NULL,
        related_name="catalog_price_changes",
    )
    price_source = models.CharField(
        "Источник цены",
        max_length=16,
        choices=PriceSource.choices,
        default=PriceSource.MANUAL,
        editable=False,
    )

    objects = ProductQuerySet.as_manager()

    class Meta:
        verbose_name = "товар"
        verbose_name_plural = "товары"
        ordering = ("display_order", "name", "pk")
        constraints = [
            models.CheckConstraint(condition=Q(price_per_kg__gt=0), name="product_price_positive"),
            models.CheckConstraint(condition=Q(minimum_weight__gt=0), name="product_min_weight_positive"),
            models.CheckConstraint(condition=Q(weight_step__gt=0), name="product_weight_step_positive"),
        ]
        permissions = [("change_product_price", "Может изменять цены товаров")]

    def __str__(self):
        return self.name

    @property
    def photos(self):
        """Images in deterministic display order, using the prefetch cache when present."""
        return list(self.images.all())

    @property
    def primary_photo(self):
        photos = self.photos
        return next((photo for photo in photos if photo.is_primary), photos[0] if photos else None)

    def save(self, *args, **kwargs):
        using = kwargs.get("using") or self._state.db or "default"
        if self._state.adding:
            self.price_revision = 1
            self.price_changed_at = timezone.now()
        update_fields = kwargs.get("update_fields")
        protected_price_fields = {
            "price_per_kg", "price_revision", "price_changed_at", "price_changed_by", "price_source",
        }
        if self.pk and not self._state.adding:
            stored = type(self).objects.using(using).filter(pk=self.pk).values(
                *protected_price_fields
            ).first()
            if stored:
                if update_fields is not None and (protected_price_fields | {"price_changed_by_id"}).intersection(update_fields):
                    raise ValidationError("Изменяйте цену через сервис catalog.pricing.change_product_price().")
                if update_fields is None and self.price_per_kg != stored["price_per_kg"]:
                    raise ValidationError("Изменяйте цену через сервис catalog.pricing.change_product_price().")
                self.price_per_kg = stored["price_per_kg"]
                self.price_revision = stored["price_revision"]
                self.price_changed_at = stored["price_changed_at"]
                self.price_changed_by_id = stored["price_changed_by"]
                self.price_source = stored["price_source"]
                if update_fields is None:
                    # Keep a normal full save from writing a stale price snapshot
                    # over a concurrent price service update.
                    kwargs["update_fields"] = tuple(
                        field.name
                        for field in self._meta.concrete_fields
                        if field.name not in protected_price_fields
                        and not field.primary_key
                        and not field.generated
                    )
        self.full_clean()
        return super().save(*args, **kwargs)


class ProductImage(models.Model):
    product = models.ForeignKey(
        Product,
        verbose_name="Товар",
        related_name="images",
        on_delete=models.CASCADE,
    )
    image = models.ImageField("Файл изображения", upload_to="catalog/products/", validators=[validate_site_image])
    alt_text = models.CharField("Альтернативный текст", max_length=240, blank=True)
    display_order = models.PositiveIntegerField("Порядок показа", default=0)
    is_primary = models.BooleanField("Основное фото", default=False)

    class Meta:
        verbose_name = "фотография товара"
        verbose_name_plural = "фотографии товаров"
        ordering = ("display_order", "pk")
        constraints = [
            models.UniqueConstraint(
                fields=("product",),
                condition=Q(is_primary=True),
                name="one_primary_photo_per_product",
            ),
        ]

    def __str__(self):
        return self.alt_text or f"Фото: {self.product}"

    @property
    def url(self):
        return self.image.url if self.image else ""

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)
