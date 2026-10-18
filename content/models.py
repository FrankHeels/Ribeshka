import hashlib

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.db.models.query import QuerySet

from catalog.models import Product
from catalog.validators import validate_site_image

from .validators import validate_site_link


CONTENT_PAGE_KEYS = ("home", "about", "delivery", "contacts", "privacy")


class ContentPage(models.Model):
    class Key(models.TextChoices):
        HOME = "home", "Главная"
        ABOUT = "about", "О компании"
        DELIVERY = "delivery", "Доставка и самовывоз"
        CONTACTS = "contacts", "Контакты"
        PRIVACY = "privacy", "Политика обработки персональных данных"

    key = models.CharField("Страница", max_length=16, choices=Key.choices, unique=True)
    title = models.CharField("Заголовок страницы", max_length=180, blank=True)
    seo_title = models.CharField("SEO-заголовок", max_length=180, blank=True)
    seo_description = models.CharField("SEO-описание", max_length=320, blank=True)
    created_at = models.DateTimeField("Создана", auto_now_add=True)
    updated_at = models.DateTimeField("Изменена", auto_now=True)

    class Meta:
        verbose_name = "страница сайта"
        verbose_name_plural = "страницы сайта"
        ordering = ("key",)
        constraints = [
            models.CheckConstraint(
                condition=Q(key__in=CONTENT_PAGE_KEYS),
                name="content_page_fixed_key",
            ),
        ]

    def __str__(self):
        return self.get_key_display()

    def clean(self):
        super().clean()
        if self.pk and not self._state.adding:
            using = self._state.db or "default"
            previous_key = type(self).objects.using(using).filter(pk=self.pk).values_list("key", flat=True).first()
            if previous_key is not None and self.key != previous_key:
                raise ValidationError({"key": "Технический ключ маршрута нельзя менять."})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Фиксированные страницы сайта нельзя удалять.")


class ConsentRevisionQuerySet(QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Архивные редакции согласия нельзя изменять.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError("Архивные редакции согласия нельзя изменять.")

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        if update_conflicts:
            raise ValidationError("Архивные редакции согласия нельзя обновлять.")
        raise ValidationError("Создавайте архивные редакции согласия через save().")

    def delete(self):
        raise ValidationError("Архивные редакции согласия нельзя удалять.")


class ConsentRevision(models.Model):
    """Immutable copy of a consent Markdown source used by a public form."""

    revision_id = models.CharField("Идентификатор редакции", max_length=71, unique=True)
    sha256 = models.CharField("SHA-256 исходного Markdown", max_length=64, unique=True)
    text = models.TextField("Текст исходной редакции")
    registered_at = models.DateTimeField("Зарегистрирована", auto_now_add=True)

    objects = ConsentRevisionQuerySet.as_manager()

    class Meta:
        verbose_name = "редакция согласия на обработку данных"
        verbose_name_plural = "архив редакций согласия на обработку данных"
        ordering = ("-registered_at", "-pk")

    def __str__(self):
        return self.revision_id

    def clean(self):
        super().clean()
        if not self.text:
            raise ValidationError({"text": "Текст редакции не может быть пустым."})
        digest = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        if self.sha256 != digest or self.revision_id != f"sha256:{digest}":
            raise ValidationError("Идентификатор и хеш должны соответствовать тексту редакции.")

    def save(self, *args, **kwargs):
        using = kwargs.get("using") or self._state.db or "default"
        if self.pk:
            previous = type(self).objects.using(using).filter(pk=self.pk).first()
            if previous is not None and (
                self.revision_id,
                self.sha256,
                self.text,
                self.registered_at,
            ) != (
                previous.revision_id,
                previous.sha256,
                previous.text,
                previous.registered_at,
            ):
                raise ValidationError("Архивные редакции согласия нельзя изменять.")
        else:
            digest = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
            self.sha256 = digest
            self.revision_id = f"sha256:{digest}"
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Архивные редакции согласия нельзя удалять.")


HOME_SECTION_KEYS = (
    "hero",
    "categories",
    "products",
    "about",
    "advantage_1",
    "advantage_2",
    "advantage_3",
    "advantage_4",
    "steps",
    "step_1",
    "step_2",
    "step_3",
    "delivery",
    "contacts",
    "hero_value_1",
    "hero_value_2",
    "hero_value_3",
)

HOME_SECTION_LABELS = {
    "hero": "Обложка",
    "categories": "Категории",
    "products": "Товары",
    "about": "О компании",
    "advantage_1": "Преимущество 1",
    "advantage_2": "Преимущество 2",
    "advantage_3": "Преимущество 3",
    "advantage_4": "Преимущество 4",
    "steps": "Порядок заявки",
    "step_1": "Шаг 1",
    "step_2": "Шаг 2",
    "step_3": "Шаг 3",
    "delivery": "Доставка",
    "contacts": "Контакты",
    "hero_value_1": "Преимущество на обложке 1",
    "hero_value_2": "Преимущество на обложке 2",
    "hero_value_3": "Преимущество на обложке 3",
    "lead": "Вступление",
}


class ContentSection(models.Model):
    page = models.ForeignKey(
        ContentPage,
        verbose_name="Страница",
        related_name="sections",
        on_delete=models.CASCADE,
    )
    key = models.SlugField("Технический ключ", max_length=80)
    title = models.CharField("Заголовок блока", max_length=180, blank=True)
    body = models.TextField("Текст", blank=True)
    image = models.ImageField("Изображение блока", upload_to="content/sections/", blank=True, validators=[validate_site_image])
    image_alt = models.CharField("Описание изображения", max_length=240, blank=True)
    display_order = models.PositiveSmallIntegerField("Порядок показа", default=0)
    updated_at = models.DateTimeField("Изменена", auto_now=True)

    class Meta:
        verbose_name = "блок страницы"
        verbose_name_plural = "блоки страниц"
        ordering = ("display_order", "pk")
        constraints = [
            models.UniqueConstraint(fields=("page", "key"), name="content_section_page_key_uniq"),
        ]
        indexes = [models.Index(fields=("page", "display_order"), name="content_section_order_idx")]

    def __str__(self):
        return self.title or HOME_SECTION_LABELS.get(self.key, "Текстовый блок")

    def clean(self):
        super().clean()
        if self.image and not self.image_alt.strip():
            raise ValidationError({"image_alt": "Добавьте краткое описание изображения для пользователей экранного диктора."})
        using = self._state.db or "default"
        if self.page_id:
            page_key = ContentPage.objects.using(using).filter(pk=self.page_id).values_list("key", flat=True).first()
            if page_key == ContentPage.Key.HOME and self.key not in HOME_SECTION_KEYS:
                raise ValidationError({"key": "Для главной страницы разрешены только фиксированные блоки."})
        if self.pk and not self._state.adding:
            previous = type(self).objects.using(using).filter(pk=self.pk).values_list("page_id", "key").first()
            if previous is not None and (self.page_id, self.key) != previous:
                raise ValidationError("Страницу и технический ключ существующего блока менять нельзя.")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class SingletonModel(models.Model):
    """Base for editable site settings whose public route expects one row."""

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        self.pk = 1
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Настройки сайта нельзя удалять.")


class SiteSettings(SingletonModel):
    phone_primary = models.CharField("Основной телефон", max_length=40, blank=True)
    phone_secondary = models.CharField("Дополнительный телефон", max_length=40, blank=True)
    email = models.EmailField("Электронная почта", blank=True)
    address = models.CharField("Адрес", max_length=240, blank=True)
    opening_hours = models.TextField("График работы", blank=True)
    whatsapp_url = models.URLField("Ссылка WhatsApp", max_length=500, blank=True, validators=[validate_site_link])
    telegram_url = models.URLField("Ссылка Telegram", max_length=500, blank=True, validators=[validate_site_link])
    vk_url = models.URLField("Ссылка ВКонтакте", max_length=500, blank=True, validators=[validate_site_link])
    footer_copy = models.TextField("Текст в подвале сайта", blank=True)
    footer_notice = models.CharField("Подпись внизу сайта", max_length=240, blank=True)

    class Meta:
        verbose_name = "контакты и общие настройки"
        verbose_name_plural = "контакты и общие настройки"
        constraints = [models.CheckConstraint(condition=Q(pk=1), name="site_settings_singleton_pk")]

    def __str__(self):
        return "Контакты и общие настройки"


class HomepageRecommendation(models.Model):
    class Slot(models.IntegerChoices):
        FIRST = 1, "Рекомендуемый товар 1"
        SECOND = 2, "Рекомендуемый товар 2"

    slot = models.PositiveSmallIntegerField("Позиция", choices=Slot.choices, unique=True)
    product = models.ForeignKey(
        Product,
        verbose_name="Товар",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="homepage_recommendation_slots",
    )

    class Meta:
        verbose_name = "рекомендуемые товары"
        verbose_name_plural = "рекомендуемые товары"
        ordering = ("slot",)
        constraints = [
            models.CheckConstraint(condition=Q(slot__in=(1, 2)), name="homepage_recommendation_slot_valid"),
            models.UniqueConstraint(
                fields=("product",),
                condition=Q(product__isnull=False),
                name="homepage_recommendation_product_uniq",
            ),
        ]

    def __str__(self):
        return self.get_slot_display()

    def clean(self):
        super().clean()
        if not self.product_id:
            return
        using = self._state.db or "default"
        current_id = None
        if self.pk:
            current_id = type(self).objects.using(using).filter(pk=self.pk).values_list("product_id", flat=True).first()
        if current_id == self.product_id:
            return
        if not Product.objects.using(using).filter(pk=self.product_id, is_published=True).exists():
            raise ValidationError({"product": "Для новой рекомендации выберите опубликованный товар."})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)
