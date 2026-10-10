"""Pure decisions and transformations for retail order submission."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import Order


@dataclass(frozen=True)
class RetailSubmissionResult:
    status: str
    order: Order | None = None
    snapshot: dict | None = None


def retail_snapshot_for_state(state):
    """Build the stable, displayable cart snapshot that is signed into the form."""
    items = []
    for item in sorted(state.items, key=lambda entry: int(entry["key"])):
        items.append(
            {
                "product_id": item["key"],
                "name": item["product"].name,
                "price_per_kg": format(item["price_per_kg"], ".2f"),
                "weight": item["weight_display"],
            }
        )
    return {"items": items}


def retail_submission_rejection(
    *,
    claim,
    current_snapshot,
    current_items,
    unavailable,
    invalid_weight,
    cleaned_data,
):
    """Return a rejection status, or None when the cart may be submitted."""
    if unavailable or current_snapshot != claim["snapshot"]:
        return "changed"
    if not current_items:
        return "empty"
    if invalid_weight:
        return "invalid_cart"
    if claim["requires_confirmation"] and not cleaned_data["confirm_updated_cart"]:
        return "confirmation_required"
    return None


def build_retail_order_data(cleaned_data, key, current_items, created_at, *, consent_revision):
    """Convert validated form and locked cart values into persistence payloads."""
    order_data = {
        "name": cleaned_data["name"],
        "phone": cleaned_data["phone"],
        "email": cleaned_data["email"],
        "fulfillment_method": cleaned_data["pickup_method"],
        "delivery_address": cleaned_data["address"],
        "comment": cleaned_data["comment"],
        "privacy_consent": True,
        "privacy_consent_at": created_at,
        "privacy_consent_revision": consent_revision,
        "submission_key": key,
    }
    item_data = [
        {
            "product": product,
            "product_name_snapshot": product.name,
            "price_per_kg_snapshot": product.price_per_kg,
            "desired_weight": weight,
        }
        for product, weight, _raw_weight, _valid in current_items
    ]
    return order_data, item_data
