"""Validate identities and money without following source-provided links."""

from collections.abc import Mapping
from decimal import Decimal, DecimalException, ROUND_HALF_UP
from urllib.parse import urlsplit
import uuid
from zoneinfo import ZoneInfo

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .contracts import MoySkladError


API_BASE = "https://api.moysklad.ru/api/remap/1.2/"


def uuid_value(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise MoySkladError("invalid_uuid") from None


def reference_id(value, entity_type):
    """Extract an ID only from an exact fixed-origin resource reference."""
    if not isinstance(value, Mapping):
        raise MoySkladError("invalid_reference")
    meta = value.get("meta", value)
    if not isinstance(meta, Mapping) or meta.get("type") != entity_type:
        raise MoySkladError("invalid_reference_type")
    href = meta.get("href")
    if not isinstance(href, str):
        raise MoySkladError("invalid_reference")
    resource = "context/companysettings/pricetype" if entity_type == "pricetype" else f"entity/{entity_type}"
    prefix = f"{API_BASE}{resource}/"
    if not href.startswith(prefix):
        raise MoySkladError("invalid_reference")
    parts = urlsplit(href)
    if parts.query or parts.fragment:
        raise MoySkladError("invalid_reference")
    identifier = uuid_value(href[len(prefix):])
    if href != f"{prefix}{identifier}":
        raise MoySkladError("invalid_reference")
    if value.get("id") is not None and uuid_value(value["id"]) != identifier:
        raise MoySkladError("identity_mismatch")
    return identifier


def source_timestamp(value):
    if not isinstance(value, str):
        raise MoySkladError("invalid_source_timestamp")
    try:
        stamp = parse_datetime(value)
    except ValueError:
        stamp = None
    if stamp is None:
        raise MoySkladError("invalid_source_timestamp")
    return timezone.make_aware(stamp, ZoneInfo("Europe/Moscow")) if timezone.is_naive(stamp) else stamp


def selected_price(data, config):
    """Select the configured RUB price and convert kopecks using Decimal."""
    prices = data.get("salePrices")
    if not isinstance(prices, list):
        raise MoySkladError("missing_price")
    selected = []
    for item in prices:
        if not isinstance(item, Mapping):
            raise MoySkladError("invalid_price")
        if reference_id(item.get("priceType"), "pricetype") == config.price_type_id:
            selected.append(item)
    if len(selected) != 1:
        raise MoySkladError("missing_price" if not selected else "duplicate_price")
    item = selected[0]
    if reference_id(item.get("currency"), "currency") != config.currency_id:
        raise MoySkladError("invalid_currency")
    raw = item.get("value")
    if isinstance(raw, (float, bool)) or not isinstance(raw, (str, int, Decimal)):
        raise MoySkladError("invalid_price")
    try:
        value = Decimal(raw)
        if not value.is_finite() or value <= 0 or value > Decimal("9999999999.99"):
            raise MoySkladError("invalid_price")
        price = (value / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (DecimalException, ValueError, OverflowError):
        raise MoySkladError("invalid_price") from None
    if not Decimal("0.01") <= price <= Decimal("99999999.99"):
        raise MoySkladError("invalid_price")
    return price


def parse_product(data, config, expected_id=None):
    """Return only safe registry fields; unsuitable source data stays visible."""
    if not isinstance(data, Mapping):
        raise MoySkladError("invalid_product")
    external_id = uuid_value(data.get("id"))
    if expected_id is not None and external_id != uuid_value(expected_id):
        raise MoySkladError("identity_mismatch")
    if uuid_value(data.get("accountId")) != config.account_id:
        raise MoySkladError("account_mismatch")
    error = ""
    try:
        if reference_id(data, "product") != external_id:
            raise MoySkladError("identity_mismatch")
    except MoySkladError as exc:
        meta = data.get("meta")
        if isinstance(meta, Mapping) and meta.get("type") != "product":
            error = "unsupported_type"
        else:
            raise exc
    unit_id = None
    try:
        unit_id = reference_id(data.get("uom"), "uom")
        if unit_id != config.unit_id:
            error = error or "invalid_unit"
    except MoySkladError:
        error = error or "invalid_unit"
    archived = data.get("archived", False)
    if not isinstance(archived, bool):
        raise MoySkladError("invalid_product")
    if archived:
        error = "archived"
    price = None
    try:
        price = selected_price(data, config)
    except MoySkladError as exc:
        error = error or exc.code
    text = {}
    for key in ("name", "code", "article"):
        value = data.get(key, "")
        if not isinstance(value, str) or len(value) > 255:
            raise MoySkladError("invalid_product")
        text[key] = value
    uom = data.get("uom")
    unit_name = uom.get("name", "") if isinstance(uom, Mapping) else ""
    if not isinstance(unit_name, str) or len(unit_name) > 255:
        raise MoySkladError("invalid_unit")
    return {
        **text, "external_id": external_id, "entity_type": "product", "unit_id": unit_id,
        "unit_name": unit_name, "archived": archived, "price": price,
        "source_updated_at": source_timestamp(data.get("updated")), "last_error_code": error,
        "is_deferred": external_id in config.deferred_product_ids,
    }


def validate_product(data, config, expected_id=None):
    parsed = parse_product(data, config, expected_id)
    if parsed["last_error_code"]:
        raise MoySkladError(parsed["last_error_code"])
    if parsed["is_deferred"]:
        raise MoySkladError("deferred_product")
    return parsed
