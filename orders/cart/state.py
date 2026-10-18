"""Small helpers for the anonymous, session-backed retail cart."""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from catalog.models import Product

from ..money import calculate_line_total


CART_SESSION_KEY = "retail_cart"
MAX_PRODUCT_ID = 2**63 - 1
# Keep accepting the historical Decimal(8, 3) range in stored sessions so old
# fractional lines can be corrected or removed instead of silently discarded.
MAX_WEIGHT = Decimal("99999.999")
MIN_RETAIL_WEIGHT = Decimal("1")
MAX_RETAIL_WEIGHT = Decimal("99999")
INVALID_WEIGHT_MESSAGE = "Укажите целое количество килограммов от 1 до 99 999."
WEIGHT_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]{1,3})?\Z")
CART_ISSUES_MESSAGE = (
    "Некоторые позиции больше недоступны или были удалены из корзины. "
    "Проверьте её состав."
)


@dataclass
class CartState:
    items: list[dict]
    total: Decimal | None
    count: int
    has_issues: bool = False
    total_is_valid: bool = True


def parse_product_id(value):
    """Return a safe positive database ID, or None for malformed input."""
    if not isinstance(value, str) or not value or not value.isascii() or not value.isdigit():
        return None
    if len(value) > 19:
        return None
    product_id = int(value)
    if product_id < 1 or product_id > MAX_PRODUCT_ID or str(product_id) != value:
        return None
    return product_id


def parse_stored_weight(value):
    """Read only the bounded decimal representation written by this cart."""
    if not isinstance(value, str) or not WEIGHT_PATTERN.fullmatch(value):
        return None
    try:
        weight = Decimal(value)
    except InvalidOperation:
        return None
    if not weight.is_finite() or weight <= 0 or weight > MAX_WEIGHT:
        return None
    return weight


def is_valid_retail_weight(weight):
    """Return whether a desired retail weight is a whole number of kg."""
    return (
        isinstance(weight, Decimal)
        and weight.is_finite()
        and MIN_RETAIL_WEIGHT <= weight <= MAX_RETAIL_WEIGHT
        and weight == weight.to_integral_value()
    )


def get_stored_lines(request):
    """Return a sanitized copy of session lines, removing invalid records."""
    session = getattr(request, "session", None)
    if session is None:
        return {}, False

    raw_cart = session.get(CART_SESSION_KEY, {})
    if not isinstance(raw_cart, dict):
        session[CART_SESSION_KEY] = {}
        return {}, True

    lines = {}
    has_issues = False
    for raw_id, raw_weight in raw_cart.items():
        product_id = parse_product_id(raw_id)
        weight = parse_stored_weight(raw_weight)
        if product_id is None or weight is None:
            has_issues = True
            continue
        lines[str(product_id)] = raw_weight

    if lines != raw_cart:
        session[CART_SESSION_KEY] = lines
    return lines, has_issues


def invalidate_cart_state(request):
    """Drop this request's cached cart after changing its session lines."""
    if hasattr(request, "_retail_cart_state"):
        delattr(request, "_retail_cart_state")


def set_stored_lines(request, lines):
    """Replace only the cart session key, preserving other session values."""
    session = getattr(request, "session", None)
    if session is not None:
        session[CART_SESSION_KEY] = dict(lines)
    invalidate_cart_state(request)


def get_cart_state(request):
    """Load published products and current prices once per request."""
    cached = getattr(request, "_retail_cart_state", None)
    if cached is not None:
        return cached

    lines, has_issues = get_stored_lines(request)
    if not lines:
        state = CartState(items=[], total=Decimal("0.00"), count=0, has_issues=has_issues)
        request._retail_cart_state = state
        return state

    product_ids = [parse_product_id(product_id) for product_id in lines]
    products = Product.objects.published().select_related("category", "moysklad_product").filter(pk__in=product_ids)
    products_by_id = {str(product.pk): product for product in products}

    visible_lines = {}
    items = []
    total = Decimal("0.00")
    total_is_valid = True
    for product_id, raw_weight in lines.items():
        product = products_by_id.get(product_id)
        if product is None:
            has_issues = True
            continue

        weight = Decimal(raw_weight)
        valid_weight = is_valid_retail_weight(weight)
        line_total = calculate_line_total(product.price_per_kg, weight) if valid_weight else None
        if not valid_weight:
            total_is_valid = False
        else:
            total += line_total

        visible_lines[product_id] = raw_weight
        items.append(
            {
                "key": product_id,
                "product": product,
                "price_per_kg": product.price_per_kg,
                "weight": weight,
                "weight_display": (
                    format(weight.to_integral_value(), "f")
                    if valid_weight
                    else raw_weight
                ),
                "weight_errors": (
                    []
                    if valid_weight
                    else [INVALID_WEIGHT_MESSAGE]
                ),
                "line_total": line_total,
            }
        )

    if visible_lines != lines:
        set_stored_lines(request, visible_lines)

    state = CartState(
        items=items,
        total=total if total_is_valid else None,
        count=len(items),
        has_issues=has_issues,
        total_is_valid=total_is_valid,
    )
    request._retail_cart_state = state
    return state


def cart_count(request):
    """Return the number of distinct currently published cart lines."""
    return get_cart_state(request).count
