"""Read-only account checks; return identifiers and safe diagnostic codes only."""

from collections.abc import Mapping

from .config import get_config
from .contracts import MoySkladError
from .mapping import uuid_value


def _object(data, config, *, identifier=None):
    if not isinstance(data, Mapping):
        raise MoySkladError("invalid_check_response")
    if identifier is not None and uuid_value(data.get("id")) != identifier:
        raise MoySkladError("identity_mismatch")
    if data.get("accountId") is not None and uuid_value(data["accountId"]) != config.account_id:
        raise MoySkladError("account_mismatch")
    if data.get("archived") or data.get("deleted") or data.get("deletedAt"):
        raise MoySkladError("configured_object_unavailable")
    return data


def check_connection(client, config=None):
    """Verify the configured account and required dictionaries without writes."""
    config = get_config(config)
    required = (
        "account_id", "namespace", "price_type_id", "currency_id", "unit_id",
        "organization_id", "agent_id", "initial_state_id",
    )
    if any(getattr(config, field) is None for field in required):
        raise MoySkladError("incomplete_check_configuration")
    products = _object(client.get("entity/product", params={"limit": 1, "offset": 0}), config)
    rows = products.get("rows")
    if not isinstance(rows, list) or len(rows) > 1:
        raise MoySkladError("invalid_check_response")
    account_verified = False
    for row in rows:
        row = _object(row, config)
        if row.get("accountId") is not None:
            account_verified = True

    currency = _object(client.get(f"entity/currency/{config.currency_id}"), config,
                       identifier=config.currency_id)
    if currency.get("isoCode") != "RUB" and str(currency.get("code", "")) not in ("643", "RUB"):
        raise MoySkladError("invalid_currency")
    unit = _object(client.get(f"entity/uom/{config.unit_id}"), config, identifier=config.unit_id)
    if str(unit.get("code", "")) != "166" and str(unit.get("name", "")).strip().lower() not in (
        "кг", "кг.", "килограмм", "kg",
    ):
        raise MoySkladError("invalid_unit")

    for resource, identifier in (
        ("organization", config.organization_id), ("counterparty", config.agent_id),
    ):
        value = _object(client.get(f"entity/{resource}/{identifier}"), config, identifier=identifier)
        account_verified |= value.get("accountId") is not None
    _object(client.get(f"context/companysettings/pricetype/{config.price_type_id}"), config,
            identifier=config.price_type_id)
    metadata = _object(client.get("entity/customerorder/metadata"), config)
    states = metadata.get("states")
    if not isinstance(states, list):
        raise MoySkladError("invalid_check_response")
    selected = [row for row in states if isinstance(row, Mapping) and row.get("id") == str(config.initial_state_id)]
    if len(selected) != 1:
        raise MoySkladError("invalid_initial_state")
    _object(selected[0], config, identifier=config.initial_state_id)
    if not account_verified:
        raise MoySkladError("account_not_verified")
    return {"code": "connection_verified", **{field: str(getattr(config, field)) for field in required}}


def discover_configuration(client, config=None):
    """List at most one bounded page per dictionary; never expose contacts or URLs."""
    config = get_config(config)
    result = {"code": "discovery_completed"}
    accounts = set()
    for resource, key in (
        ("product", "product"), ("currency", "currency"), ("uom", "unit"),
        ("organization", "organization"), ("counterparty", "agent"),
    ):
        response = client.get(f"entity/{resource}", params={"limit": 100, "offset": 0})
        if not isinstance(response, Mapping) or not isinstance(response.get("rows"), list):
            raise MoySkladError("invalid_check_response")
        rows = response["rows"]
        if len(rows) > 100:
            raise MoySkladError("invalid_check_response")
        identifiers = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise MoySkladError("invalid_check_response")
            identifier = uuid_value(row.get("id"))
            if row.get("accountId") is not None:
                account = uuid_value(row["accountId"])
                if config.account_id is not None and account != config.account_id:
                    raise MoySkladError("account_mismatch")
                accounts.add(str(account))
            if row.get("archived"):
                continue
            if resource == "currency" and row.get("isoCode") != "RUB" and str(row.get("code", "")) not in ("643", "RUB"):
                continue
            if resource == "uom" and str(row.get("code", "")) != "166" and str(row.get("name", "")).strip().lower() not in (
                "кг", "кг.", "килограмм", "kg",
            ):
                continue
            identifiers.append(str(identifier))
        if resource != "product":
            result[f"{key}_ids"] = identifiers
        meta = response.get("meta", {})
        size = meta.get("size") if isinstance(meta, Mapping) else None
        result[f"{key}_page_full"] = len(rows) == 100 or (isinstance(size, int) and size > len(rows))
    prices = client.get("context/companysettings/pricetype")
    if not isinstance(prices, list) or len(prices) > 100:
        raise MoySkladError("invalid_check_response")
    result["price_type_ids"] = [str(uuid_value(row.get("id"))) for row in prices if isinstance(row, Mapping)]
    metadata = client.get("entity/customerorder/metadata")
    if not isinstance(metadata, Mapping) or not isinstance(metadata.get("states"), list) or len(metadata["states"]) > 100:
        raise MoySkladError("invalid_check_response")
    result["initial_state_ids"] = [str(uuid_value(row.get("id"))) for row in metadata["states"] if isinstance(row, Mapping)]
    if len(accounts) > 1:
        raise MoySkladError("account_mismatch")
    result["account_ids"] = sorted(accounts)
    return result
