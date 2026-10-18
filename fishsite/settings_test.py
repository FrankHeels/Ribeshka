"""Isolated PostgreSQL test profile. No application .env or credentials."""
import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import dotenv_values

from .settings import *  # noqa: F403
from .test_safety import install_network_guard, validate_database

_values = dotenv_values(Path(__file__).resolve().parent.parent / ".env.test")

def _required(name):
    key = f"FISHSITE_TEST_DB_{name}"
    value = os.environ.get(key, _values.get(key))
    if not value:
        raise ImproperlyConfigured(f"{key} is required for isolated tests.")
    return value

DATABASES = {
    "default": {
        "ENGINE": "fishsite.test_db",
        **{name: _required(name) for name in ("HOST", "PORT", "USER", "PASSWORD", "NAME")},
        "CONN_MAX_AGE": 0,
        "OPTIONS": {"connect_timeout": 5},
        "TEST": {"NAME": "test_fishsite"},
    }
}
validate_database(DATABASES["default"], maintenance=True)
SECRET_KEY = "synthetic-fishsite-test-secret"
DEBUG = False
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
EMAIL_HOST = "127.0.0.1"
EMAIL_PORT = 1
EMAIL_HOST_USER = EMAIL_HOST_PASSWORD = ""
EMAIL_USE_TLS = EMAIL_USE_SSL = SMTP_ALLOW_INSECURE_LOCAL_AUTH = False
DEFAULT_FROM_EMAIL = "notifications@example.test"
NOTIFICATION_TO_EMAIL = "seller@example.test"
NOTIFICATION_BACKEND = os.environ.get(
    "FISHSITE_TEST_NOTIFICATION_BACKEND", "fishsite.test_safety.NoopNotificationBackend"
)
# Subprocess tests may choose only these synthetic adapters.
if NOTIFICATION_BACKEND not in {
    "fishsite.test_safety.NoopNotificationBackend",
    "tests.test_notifications.NoopNotificationBackend",
    "tests.test_notifications.BlockingNotificationBackend",
}:
    raise ImproperlyConfigured("Unsafe test notification backend.")
NOTIFICATION_LEASE_SECONDS = int(os.environ.get("FISHSITE_TEST_LEASE_SECONDS", "300"))
NOTIFICATION_SCAN_INTERVAL_SECONDS = int(os.environ.get("FISHSITE_TEST_SCAN_SECONDS", "60"))
TEST_RUNNER = "fishsite.test_runner.IsolatedTestRunner"
MOYSKLAD_TEST_MODE = True
MOYSKLAD = {
    **{name: "" for name in (
        "account_id", "namespace", "api_token", "admin_token", "price_type_id", "currency_id",
        "unit_id", "organization_id", "agent_id", "initial_state_id", "webhook_secret", "webhook_url",
    )},
    "environment": "test",
    "catalog_enabled": False,
    "prices_enabled": False,
    "export_enabled": False,
    "statuses_enabled": False,
}
install_network_guard()
