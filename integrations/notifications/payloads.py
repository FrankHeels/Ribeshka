"""Immutable snapshots of the request data rendered into seller notifications."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from django.core.exceptions import ImproperlyConfigured, ObjectDoesNotExist
from django.db.models import Prefetch
from django.urls import reverse

from orders.models import OrderItem

from integrations.models import NotificationOutbox
from integrations.moysklad.config import get_config


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


@dataclass(frozen=True, slots=True)
class IncidentNotificationDTO:
    incident_id: int
    created_at: datetime
    scope: str
    code: str
    object_ids: tuple[str, ...]
    diagnostics_url: str
    kind: str = "moysklad_incident"

    @property
    def delivery_key(self):
        return f"moysklad_incident:{self.incident_id}"


def _incident_diagnostics_url():
    path = reverse("admin:moysklad_status")
    try:
        receiver = urlsplit(get_config().webhook_url)
    except (ImproperlyConfigured, ValueError):
        return path
    if receiver.scheme == "https" and receiver.netloc:
        return urlunsplit(("https", receiver.netloc, path, "", ""))
    return path


def build_notification(outbox_id, *, using="default"):
    """Read a saved request and its immutable retail item snapshots."""
    row = (
        NotificationOutbox.objects.using(using)
        .select_related("order", "inquiry", "incident")
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

    if row.incident_id:
        incident = row.incident
        return IncidentNotificationDTO(
            incident_id=incident.pk, created_at=incident.opened_at,
            scope=incident.scope, code=incident.code, object_ids=tuple(incident.object_ids),
            diagnostics_url=_incident_diagnostics_url(),
        )

    raise ObjectDoesNotExist("Outbox request is unavailable")
