from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

OUTBOX_PENDING = "pending"
OUTBOX_PROCESSING = "processing"
OUTBOX_SENT = "sent"
OUTBOX_MANUAL_REVIEW = "manual_review"


class NotificationOutbox(models.Model):
    class Status(models.TextChoices):
        PENDING = OUTBOX_PENDING, "Ожидает отправки"
        PROCESSING = OUTBOX_PROCESSING, "Обрабатывается"
        SENT = OUTBOX_SENT, "Отправлено"
        MANUAL_REVIEW = OUTBOX_MANUAL_REVIEW, "Требует разбора"

    order = models.OneToOneField(
        "orders.Order",
        verbose_name="Розничная заявка",
        related_name="notification_outbox",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    inquiry = models.OneToOneField(
        "inquiries.Inquiry",
        verbose_name="Оптовый запрос",
        related_name="notification_outbox",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    status = models.CharField("Состояние", max_length=20, choices=Status.choices, default=Status.PENDING)
    attempt_count = models.PositiveIntegerField("Число попыток", default=0)
    last_error = models.TextField("Последняя ошибка", blank=True)
    next_attempt_at = models.DateTimeField("Следующая попытка", default=timezone.now)
    lease_expires_at = models.DateTimeField("Захват действует до", null=True, blank=True)
    lease_token = models.UUIDField("Токен владельца захвата", null=True, blank=True)
    created_at = models.DateTimeField("Создана", auto_now_add=True)
    sent_at = models.DateTimeField("Успешно отправлена", null=True, blank=True)

    class Meta:
        verbose_name = "уведомление"
        verbose_name_plural = "очередь уведомлений"
        ordering = ("created_at", "pk")
        constraints = [
            models.CheckConstraint(
                condition=(Q(order__isnull=False, inquiry__isnull=True) | Q(order__isnull=True, inquiry__isnull=False)),
                name="outbox_exactly_one_request",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        status=OUTBOX_PROCESSING,
                        lease_expires_at__isnull=False,
                        lease_token__isnull=False,
                        sent_at__isnull=True,
                    )
                    | Q(
                        status=OUTBOX_SENT,
                        lease_expires_at__isnull=True,
                        lease_token__isnull=True,
                        sent_at__isnull=False,
                    )
                    | Q(
                        status__in=(OUTBOX_PENDING, OUTBOX_MANUAL_REVIEW),
                        lease_expires_at__isnull=True,
                        lease_token__isnull=True,
                        sent_at__isnull=True,
                    )
                ),
                name="outbox_state_lease_consistent",
            ),
        ]
        indexes = [
            models.Index(fields=("status", "next_attempt_at", "created_at"), name="outbox_ready_queue_idx"),
            models.Index(fields=("status", "lease_expires_at"), name="outbox_expired_lease_idx"),
        ]

    def __str__(self):
        if self.order_id:
            request_number = self.order.order_number
        elif self.inquiry_id:
            request_number = self.inquiry.inquiry_number
        else:
            request_number = "Заявка не указана"
        return f"{request_number}: {self.get_status_display()}"

    def clean(self):
        super().clean()
        if bool(self.order_id) == bool(self.inquiry_id):
            raise ValidationError("Уведомление должно ссылаться ровно на одну заявку.")
        if self.status == self.Status.PROCESSING and (not self.lease_token or not self.lease_expires_at):
            raise ValidationError("Для обработки нужны срок захвата и токен владельца.")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)
