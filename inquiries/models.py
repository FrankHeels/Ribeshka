import secrets
import uuid

from django.conf import settings

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from content.models import ConsentRevision


def _wholesale_inquiry_number():
    return f"W-{secrets.token_hex(5).upper()}"


class Inquiry(models.Model):
    inquiry_number = models.CharField("Номер запроса", max_length=24, unique=True, default=_wholesale_inquiry_number, editable=False)
    contact_name = models.CharField("Контактное лицо", max_length=160)
    company = models.CharField("Компания", max_length=180, blank=True)
    phone = models.CharField("Телефон", max_length=40)
    email = models.EmailField("Email", blank=True)
    need = models.TextField("Описание потребности", blank=True, default="")
    privacy_consent = models.BooleanField("Согласие на обработку данных", default=False)
    privacy_consent_at = models.DateTimeField("Время согласия")
    privacy_consent_revision = models.ForeignKey(
        ConsentRevision,
        verbose_name="Редакция согласия",
        related_name="wholesale_inquiries",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    submission_key = models.UUIDField("Ключ отправки", default=uuid.uuid4, unique=True, editable=False)
    success_token = models.UUIDField("Токен страницы успеха", default=uuid.uuid4, unique=True, editable=False)
    created_at = models.DateTimeField("Создан", auto_now_add=True)
    internal_notes = models.TextField("Заметка менеджера", blank=True)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Ответственный менеджер",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_wholesale_inquiries",
    )

    class Meta:
        verbose_name = "оптовый запрос"
        verbose_name_plural = "оптовые запросы"
        ordering = ("-created_at", "-pk")
        constraints = [
            models.CheckConstraint(condition=Q(privacy_consent=True), name="inquiry_privacy_consent_true"),
        ]

    def __str__(self):
        return self.inquiry_number

    def clean(self):
        super().clean()
        errors = {}
        if not isinstance(self.contact_name, str) or not self.contact_name.strip():
            errors["contact_name"] = "Укажите контактное лицо."
        if not isinstance(self.phone, str) or not self.phone.strip():
            errors["phone"] = "Укажите телефон."
        if not self.privacy_consent:
            errors["privacy_consent"] = "Для создания запроса требуется согласие на обработку данных."
        if self.assigned_to_id:
            previous_assignee_id = (
                type(self).objects.filter(pk=self.pk).values_list("assigned_to_id", flat=True).first()
                if self.pk
                else None
            )
            if self.assigned_to_id != previous_assignee_id and (
                not self.assigned_to.is_active or not self.assigned_to.is_staff
            ):
                errors["assigned_to"] = "Ответственным можно назначить только активного сотрудника сайта."
        if self._state.adding and self.privacy_consent_revision_id is None:
            errors["privacy_consent_revision"] = "Для нового запроса требуется редакция принятого согласия."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)
