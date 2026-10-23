"""Database and session adapters used by the retail order service."""

from django.db import transaction

from catalog.models import Product
from integrations.public_forms import (
    acquire_advisory_transaction_lock,
    enqueue_notification,
)

from ..cart.state import (
    get_stored_lines,
    is_valid_retail_weight,
    parse_product_id,
    parse_stored_weight,
    set_stored_lines,
)
from ..models import Order, OrderItem
from .logic import cart_line_snapshot


def lock_retail_submission(key, *, using="default"):
    """Serialize transactions that are submitting the same retail form key."""
    acquire_advisory_transaction_lock("retail-submission", key, using=using)


def get_retail_order_by_key(key, *, using="default"):
    return Order.objects.using(using).filter(submission_key=key).first()


def find_existing_retail_order(key, *, using="default"):
    """Look up a prior submission under its transaction-scoped lock."""
    with transaction.atomic(using=using):
        lock_retail_submission(key, using=using)
        return get_retail_order_by_key(key, using=using)


def load_locked_cart_snapshot(request, *, using):
    """Read session lines and lock/reload their products for submission."""
    lines, had_invalid_lines = get_stored_lines(request)
    product_ids = [parse_product_id(value) for value in lines]
    locked_products = list(
        Product.objects.using(using)
        .select_for_update()
        .filter(pk__in=product_ids)
        .order_by("pk")
    )
    # Acquire registry locks only after every Product lock, matching remapping.
    from integrations.models import MoySkladProduct
    external_rows = MoySkladProduct.objects.using(using).select_for_update().filter(
        product_id__in=[product.pk for product in locked_products],
    ).order_by("pk")
    external_by_product = {row.product_id: row for row in external_rows}
    for product in locked_products:
        product._state.fields_cache["moysklad_product"] = external_by_product.get(product.pk)
    products = {str(product.pk): product for product in locked_products}
    snapshot_items = []
    snapshots = []
    has_unavailable = False
    has_invalid_weight = False

    for product_id, raw_weight in sorted(lines.items(), key=lambda pair: int(pair[0])):
        product = products.get(product_id)
        if product is None or not product.is_published or product.integration_block_reason:
            has_unavailable = True
            continue
        weight = parse_stored_weight(raw_weight)
        valid_weight = is_valid_retail_weight(weight) if weight is not None else False
        if not valid_weight:
            has_invalid_weight = True
        weight_display = format(weight.to_integral_value(), "f") if valid_weight else raw_weight
        snapshot_items.append(
            cart_line_snapshot(product, product_id, weight_display)
        )
        snapshots.append((product, weight, raw_weight, valid_weight))

    return (
        {"items": snapshot_items},
        snapshots,
        has_unavailable,
        has_invalid_weight or had_invalid_lines,
    )


def persist_retail_order(order_data, item_data, *, using="default"):
    """Persist an order, its item snapshots, and its transactional outbox row."""
    order = Order(**order_data)
    order.save(using=using, force_insert=True)

    order_items = [OrderItem(order=order, **item) for item in item_data]
    OrderItem.objects.using(using).bulk_create(order_items)
    enqueue_notification(order=order, using=using)
    return order


def clear_retail_cart(request):
    """Clear the session cart after its order transaction has committed."""
    set_stored_lines(request, {})
