"""Immutable snapshots of the request data rendered into seller notifications."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Prefetch

from orders.models import OrderItem

from integrations.models import NotificationOutbox


@dataclass(frozen=True, slots=True)
class NotificationItem:
    product_name: str
    price_per_kg: Decimal
    weight: Decimal
    line_total: Decimal


@dataclass(frozen=True, slots=True)
class RequestNotification:
    kind: str
    request_number: str
    submission_key: UUID
    created_at: datetime
    contact_name: str
    phone: str
    email: str
    items: tuple[NotificationItem, ...] = ()
    preliminary_total: Decimal = Decimal("0.00")
    fulfillment_method: str = ""
    delivery_address: str = ""
    comment: str = ""
    company: str = ""
    need: str = ""

    @property
    def delivery_key(self):
        return f"{self.kind}:{self.submission_key}"


def build_notification(outbox_id, *, using="default"):
    """Read a saved request and its immutable retail item snapshots."""
    row = (
        NotificationOutbox.objects.using(using)
        .select_related("order", "inquiry")
        .prefetch_related(Prefetch("order__items", queryset=OrderItem.objects.using(using).order_by("pk")))
        .get(pk=outbox_id)
    )

    if row.order_id:
        order = row.order
        items = tuple(
            NotificationItem(
                product_name=item.product_name_snapshot,
                price_per_kg=item.price_per_kg_snapshot,
                weight=item.desired_weight,
                line_total=item.line_total,
            )
            for item in order.items.all()
        )
        return RequestNotification(
            kind="retail",
            request_number=order.order_number,
            submission_key=order.submission_key,
            created_at=order.created_at,
            contact_name=order.name,
            phone=order.phone,
            email=order.email,
            items=items,
            preliminary_total=sum((item.line_total for item in items), Decimal("0.00")),
            fulfillment_method=order.fulfillment_method,
            delivery_address=order.delivery_address,
            comment=order.comment,
        )

    if row.inquiry_id:
        inquiry = row.inquiry
        return RequestNotification(
            kind="wholesale",
            request_number=inquiry.inquiry_number,
            submission_key=inquiry.submission_key,
            created_at=inquiry.created_at,
            contact_name=inquiry.contact_name,
            phone=inquiry.phone,
            email=inquiry.email,
            company=inquiry.company,
            need=inquiry.need,
        )

    raise ObjectDoesNotExist("Outbox request is unavailable")
