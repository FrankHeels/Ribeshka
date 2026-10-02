from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q


class Category(models.Model):
    name = models.CharField("Название", max_length=160)
    slug = models.SlugField("Адрес", max_length=180, unique=True)
    description = models.TextField("Описание", blank=True)
    image = models.ImageField("Изображение", upload_to="catalog/categories/", blank=True)
    display_order = models.PositiveIntegerField("Порядок показа", default=0)

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
        return self.filter(is_published=True)


class Product(models.Model):
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

    objects = ProductQuerySet.as_manager()

    class Meta:
        verbose_name = "товар"
        verbose_name_plural = "товары"
        ordering = ("name", "pk")
        constraints = [
            models.CheckConstraint(condition=Q(price_per_kg__gt=0), name="product_price_positive"),
            models.CheckConstraint(condition=Q(minimum_weight__gt=0), name="product_min_weight_positive"),
            models.CheckConstraint(condition=Q(weight_step__gt=0), name="product_weight_step_positive"),
        ]

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
        self.full_clean()
        return super().save(*args, **kwargs)


class ProductImage(models.Model):
    product = models.ForeignKey(
        Product,
        verbose_name="Товар",
        related_name="images",
        on_delete=models.CASCADE,
    )
    image = models.ImageField("Файл изображения", upload_to="catalog/products/")
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
