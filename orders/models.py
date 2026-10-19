import secrets
import uuid
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.db.models.query import QuerySet
from django.db.models.manager import Manager

from catalog.models import Product
from content.models import ConsentRevision
from .money import calculate_line_total

FULFILLMENT_DELIVERY = "delivery"
FULFILLMENT_PICKUP = "pickup"


def _retail_order_number():
    return f"R-{secrets.token_hex(5).upper()}"


class Order(models.Model):
    class FulfillmentMethod(models.TextChoices):
        DELIVERY = FULFILLMENT_DELIVERY, "Доставка"
        PICKUP = FULFILLMENT_PICKUP, "Самовывоз"

    order_number = models.CharField("Номер заявки", max_length=24, unique=True, default=_retail_order_number, editable=False)
    name = models.CharField("Имя", max_length=160)
    phone = models.CharField("Телефон", max_length=40)
    email = models.EmailField("Email", blank=True)
    fulfillment_method = models.CharField(
        "Способ получения",
        max_length=12,
        choices=FulfillmentMethod.choices,
    )
    delivery_address = models.TextField("Адрес доставки", blank=True)
    comment = models.TextField("Комментарий", blank=True)
    privacy_consent = models.BooleanField("Согласие на обработку данных", default=False)
    privacy_consent_at = models.DateTimeField("Время согласия")
    privacy_consent_revision = models.ForeignKey(
        ConsentRevision,
        verbose_name="Редакция согласия",
        related_name="retail_orders",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    submission_key = models.UUIDField("Ключ отправки", default=uuid.uuid4, unique=True, editable=False)
    success_token = models.UUIDField("Токен страницы успеха", default=uuid.uuid4, unique=True, editable=False)
    created_at = models.DateTimeField("Создана", auto_now_add=True)

    class Meta:
        verbose_name = "розничная заявка"
        verbose_name_plural = "розничные заявки"
        ordering = ("-created_at", "-pk")
        constraints = [
            models.CheckConstraint(condition=Q(privacy_consent=True), name="order_privacy_consent_true"),
            models.CheckConstraint(
                condition=Q(fulfillment_method__in=(FULFILLMENT_DELIVERY, FULFILLMENT_PICKUP)),
                name="order_fulfillment_method_valid",
            ),
            models.CheckConstraint(
                condition=~Q(fulfillment_method=FULFILLMENT_DELIVERY)
                | Q(delivery_address__regex=r"\S"),
                name="order_delivery_address_required",
            ),
        ]
        indexes = [models.Index(fields=("created_at",), name="order_created_idx")]

    def __str__(self):
        return self.order_number

    def clean(self):
        super().clean()
        errors = {}
        if not isinstance(self.name, str) or not self.name.strip():
            errors["name"] = "Укажите имя."
        if not isinstance(self.phone, str) or not self.phone.strip():
            errors["phone"] = "Укажите телефон."
        address = self.delivery_address if isinstance(self.delivery_address, str) else ""
        if self.fulfillment_method == self.FulfillmentMethod.DELIVERY and not address.strip():
            errors["delivery_address"] = "Укажите адрес для доставки."
        if not self.privacy_consent:
            errors["privacy_consent"] = "Для создания заявки требуется согласие на обработку данных."
        if self._state.adding and self.privacy_consent_revision_id is None:
            errors["privacy_consent_revision"] = "Для новой заявки требуется редакция принятого согласия."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class OrderItemQuerySet(QuerySet):
    _snapshot_fields = {
        "order", "order_id", "product_name_snapshot", "price_per_kg_snapshot",
        "desired_weight", "line_total",
        "external_account_id_snapshot", "external_entity_type_snapshot", "external_id_snapshot",
        "unit_id_snapshot", "price_revision_snapshot", "price_source_snapshot", "price_checked_at_snapshot",
    }

    def update(self, **kwargs):
        for name, value in kwargs.items():
            # Django's deletion collector uses QuerySet.update(product=None)
            # for SET_NULL. Allow only that referential cleanup; snapshots and
            # order membership stay immutable through the public queryset API.
            if name in {"product", "product_id"} and value is None:
                continue
            if name in self._snapshot_fields or name in {"product", "product_id"}:
                raise ValidationError("Снимки позиции заявки нельзя изменять через QuerySet.update().")
            raise ValidationError("Позиции заявки нельзя изменять через QuerySet.update().")
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError("Снимки позиции заявки нельзя изменять через bulk_update().")

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
            raise ValidationError("Позиции заявки нельзя обновлять через bulk_create(update_conflicts=True).")
        items = list(objs)
        for item in items:
            item._prepare_for_insert()
        return super().bulk_create(
            items,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_conflicts=False,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )


class OrderItemManager(Manager.from_queryset(OrderItemQuerySet)):
    _snapshot_fields = OrderItemQuerySet._snapshot_fields


class OrderItem(models.Model):
    order = models.ForeignKey(Order, verbose_name="Заявка", related_name="items", on_delete=models.CASCADE)
    product = models.ForeignKey(
        Product,
        verbose_name="Товар каталога",
        related_name="order_items",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    product_name_snapshot = models.CharField("Название товара на момент заявки", max_length=180)
    price_per_kg_snapshot = models.DecimalField(
        "Цена за кг на момент заявки",
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    desired_weight = models.DecimalField(
        "Желаемый вес, кг",
        max_digits=8,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0.001"))],
    )
    line_total = models.DecimalField(
        "Предварительная сумма",
        max_digits=16,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0"))],
    )
    external_account_id_snapshot = models.UUIDField("Аккаунт МойСклад на момент заявки", null=True, blank=True)
    external_entity_type_snapshot = models.CharField("Тип товара МойСклад на момент заявки", max_length=32, null=True, blank=True)
    external_id_snapshot = models.UUIDField("UUID товара МойСклад на момент заявки", null=True, blank=True)
    unit_id_snapshot = models.UUIDField("Единица на момент заявки", null=True, blank=True)
    price_revision_snapshot = models.PositiveIntegerField("Редакция цены на момент заявки", null=True, blank=True)
    price_source_snapshot = models.CharField("Источник цены на момент заявки", max_length=16, null=True, blank=True)
    price_checked_at_snapshot = models.DateTimeField("Проверка цены на момент заявки", null=True, blank=True)

    objects = OrderItemManager()

    class Meta:
        verbose_name = "позиция заявки"
        verbose_name_plural = "позиции заявки"
        ordering = ("pk",)
        constraints = [
            models.CheckConstraint(condition=Q(price_per_kg_snapshot__gt=0), name="order_item_price_positive"),
            models.CheckConstraint(condition=Q(desired_weight__gt=0), name="order_item_weight_positive"),
            models.CheckConstraint(condition=Q(line_total__gte=0), name="order_item_total_nonnegative"),
        ]

    def __str__(self):
        return f"{self.product_name_snapshot} — {self.desired_weight} кг"

    def _prepare_for_insert(self):
        if self.price_per_kg_snapshot is not None and self.desired_weight is not None:
            try:
                if isinstance(self.price_per_kg_snapshot, float) or isinstance(self.desired_weight, float):
                    raise InvalidOperation
                price = Decimal(str(self.price_per_kg_snapshot))
                weight = Decimal(str(self.desired_weight))
                self.line_total = calculate_line_total(price, weight)
            except (InvalidOperation, TypeError, ValueError):
                # Let the field validators return a useful ValidationError for
                # malformed values instead of leaking Decimal conversion errors.
                self.full_clean()
                raise ValidationError("Укажите цену и вес как десятичные числа.")
        self.full_clean()

    def save(self, *args, **kwargs):
        using = kwargs.get("using") or self._state.db or "default"
        if not self._state.adding or (self.pk is not None and type(self).objects.using(using).filter(pk=self.pk).exists()):
            raise ValidationError("Исторические позиции заявки неизменяемы.")
        self._prepare_for_insert()
        return super().save(*args, **kwargs)


__all__ = ["Order", "OrderItem", "calculate_line_total"]
