"""Pure decisions and transformations for retail order submission."""

from __future__ import annotations

from dataclasses import dataclass
from django.core.exceptions import ObjectDoesNotExist
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import Order


@dataclass(frozen=True)
class RetailSubmissionResult:
    status: str
    order: Order | None = None
    snapshot: dict | None = None


def external_snapshot(product):
    """Read the identity frozen by the same product/registry locks as its price."""
    try:
        external = product.moysklad_product
    except ObjectDoesNotExist:
        external = None
    return {
        "external_account_id": str(external.account_id) if external else None,
        "external_entity_type": external.entity_type if external else None,
        "external_id": str(external.external_id) if external else None,
        "unit_id": str(external.unit_id) if external and external.unit_id else None,
        "mapping_revision": product.mapping_revision,
    }


def cart_line_snapshot(product, product_id, weight):
    """One serializer for the displayed and transactionally checked form."""
    return {
        "product_id": str(product_id), "name": product.name,
        "price_per_kg": format(product.price_per_kg, ".2f"), "weight": weight,
        **external_snapshot(product),
    }


def retail_snapshot_for_state(state):
    """Build the stable, displayable cart snapshot that is signed into the form."""
    items = []
    for item in sorted(state.items, key=lambda entry: int(entry["key"])):
        items.append(
            cart_line_snapshot(item["product"], item["key"], item["weight_display"])
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
    item_data = []
    for product, weight, _raw_weight, _valid in current_items:
        identity = external_snapshot(product)
        try:
            checked_at = product.moysklad_product.price_checked_at
        except ObjectDoesNotExist:
            checked_at = None
        item_data.append({
            "product": product,
            "product_name_snapshot": product.name,
            "price_per_kg_snapshot": product.price_per_kg,
            "desired_weight": weight,
            **{f"{name}_snapshot": identity[name] for name in (
                "external_account_id", "external_entity_type", "external_id", "unit_id",
            )},
            "price_revision_snapshot": product.price_revision,
            "price_source_snapshot": product.price_source,
            "price_checked_at_snapshot": checked_at,
        })
    return order_data, item_data
