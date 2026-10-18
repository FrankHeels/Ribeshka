"""Validated server configuration, disabled until an account is configured."""

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
import re
from urllib.parse import urlsplit
import uuid

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


@dataclass(frozen=True, slots=True)
class MoySkladConfig:
    account_id: uuid.UUID | None = None
    environment: str = "production"
    namespace: uuid.UUID | None = None
    api_token: str = field(default="", repr=False)
    admin_token: str = field(default="", repr=False)
    price_type_id: uuid.UUID | None = None
    currency_id: uuid.UUID | None = None
    unit_id: uuid.UUID | None = None
    organization_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    initial_state_id: uuid.UUID | None = None
    deferred_product_ids: tuple[uuid.UUID, ...] = ()
    webhook_secret: str = field(default="", repr=False)
    webhook_url: str = field(default="", repr=False)
    catalog_enabled: bool = False
    prices_enabled: bool = False
    export_enabled: bool = False
    statuses_enabled: bool = False


_UUID_FIELDS = (
    "account_id", "namespace", "price_type_id", "currency_id", "unit_id",
    "organization_id", "agent_id", "initial_state_id",
)
_FLAGS = ("catalog_enabled", "prices_enabled", "export_enabled", "statuses_enabled")


def _deferred_product_ids(value):
    if isinstance(value, str):
        value = value.split(",") if value.strip() else ()
    if not isinstance(value, (list, tuple)):
        raise ImproperlyConfigured("MOYSKLAD.deferred_product_ids must contain UUIDs.")
    try:
        identifiers = []
        for item in value:
            if not isinstance(item, (str, uuid.UUID)):
                raise ValueError
            identifiers.append(uuid.UUID(item.strip()) if isinstance(item, str) else item)
        return tuple(identifiers)
    except (TypeError, ValueError, AttributeError):
        raise ImproperlyConfigured("MOYSKLAD.deferred_product_ids must contain UUIDs.") from None


def get_config(value=None):
    """Accept settings or an explicit mapping without retaining raw secrets in repr."""
    source = settings.MOYSKLAD if value is None else value
    if isinstance(source, MoySkladConfig):
        source = {item.name: getattr(source, item.name) for item in fields(MoySkladConfig)}
    if not isinstance(source, Mapping):
        raise ImproperlyConfigured("MOYSKLAD must be a configuration mapping.")
    known = {item.name for item in fields(MoySkladConfig)}
    values = {name: source[name] for name in known if name in source}
    values["deferred_product_ids"] = _deferred_product_ids(values.get(
        "deferred_product_ids", getattr(settings, "MOYSKLAD_DEFERRED_PRODUCT_IDS", ()),
    ))
    for name in _FLAGS:
        if name in values and not isinstance(values[name], bool):
            raise ImproperlyConfigured(f"MOYSKLAD.{name} must be a boolean.")
    for name in _UUID_FIELDS:
        raw = values.get(name)
        try:
            values[name] = uuid.UUID(str(raw)) if raw else None
        except (TypeError, ValueError, AttributeError):
            raise ImproperlyConfigured(f"MOYSKLAD.{name} must be a UUID.") from None
    for name in ("api_token", "admin_token", "webhook_secret", "webhook_url", "environment"):
        raw = values.get(name, "production" if name == "environment" else "")
        if not isinstance(raw, str):
            raise ImproperlyConfigured(f"MOYSKLAD.{name} must be a string.")
        values[name] = raw.strip()
    config = MoySkladConfig(**values)
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", config.environment):
        raise ImproperlyConfigured("MOYSKLAD.environment is invalid.")
    if getattr(settings, "MOYSKLAD_TEST_MODE", False):
        if config.environment != "test" or any(
            token and not token.startswith("synthetic-") for token in (config.api_token, config.admin_token)
        ):
            raise ImproperlyConfigured("Only synthetic MoySklad credentials are allowed in tests.")
    required = set()
    if any(getattr(config, flag) for flag in _FLAGS):
        required.update(("account_id", "namespace", "api_token"))
    if config.catalog_enabled or config.prices_enabled:
        required.update(("price_type_id", "currency_id", "unit_id"))
    if config.export_enabled:
        required.update(("currency_id", "unit_id", "organization_id", "agent_id", "initial_state_id"))
    for name in sorted(required):
        if not getattr(config, name):
            raise ImproperlyConfigured(f"MOYSKLAD.{name} is required for enabled exchange.")
    if config.webhook_secret and not re.fullmatch(r"[0-9a-fA-F]{64,}", config.webhook_secret):
        raise ImproperlyConfigured("MOYSKLAD.webhook_secret must contain at least 256 bits in hexadecimal.")
    if config.prices_enabled and (not config.webhook_secret or not config.webhook_url):
        raise ImproperlyConfigured("MOYSKLAD webhook receiver configuration is required for prices.")
    if config.webhook_url:
        expected_path = f"/integrations/moysklad/webhooks/prices/{config.webhook_secret}/"
        try:
            url = urlsplit(config.webhook_url)
            invalid_url = (
                url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment or not config.webhook_secret or url.path != expected_path
            )
        except ValueError:
            invalid_url = True
        if invalid_url:
            raise ImproperlyConfigured("MOYSKLAD.webhook_url must be the complete HTTPS receiver URL.") from None
    return config
