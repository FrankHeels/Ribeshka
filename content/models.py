from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


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
