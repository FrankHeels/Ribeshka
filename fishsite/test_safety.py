"""Fail-closed boundaries shared by the test runner, backend and workers."""
import ipaddress
import os
import sys

from django.core.exceptions import ImproperlyConfigured


def validate_database(config, *, maintenance=False):
    allowed = {"test_fishsite", "postgres"} if maintenance else {"test_fishsite"}
    if (
        config.get("ENGINE") != "fishsite.test_db"
        or config.get("HOST") != "127.0.0.1"
        or str(config.get("PORT")) != "15432"
        or config.get("USER") != "fishsite_tester"
        or config.get("NAME") not in allowed
        or config.get("TEST", {}).get("NAME") != "test_fishsite"
        or not config.get("PASSWORD")
        or set(config.get("OPTIONS", {})) - {"connect_timeout"}
    ):
        raise ImproperlyConfigured("Unsafe isolated test database configuration.")


def validate_live_connection(raw, *, maintenance=False):
    with raw.cursor() as cursor:
        cursor.execute(
            "SELECT current_database(), current_user, inet_server_port(), "
            "current_setting('cluster_name'), rolsuper, rolcreatedb, "
            "host(inet_server_addr()), host(inet_client_addr()) "
            "FROM pg_roles WHERE rolname = current_user"
        )
        identity = cursor.fetchone()
    allowed = {"postgres", "test_fishsite"} if maintenance else {"test_fishsite"}
    if not identity or identity[0] not in allowed or identity[1:] != (
        "fishsite_tester", 15432, "fishsite-tests", False, True, "127.0.0.1", "127.0.0.1"
    ):
        raise ImproperlyConfigured("Connected server is not the isolated test PostgreSQL.")


def worker_environment(database):
    validate_database(database)
    # Explicit allowlist prevents forwarding Gmail or primary DB credentials.
    env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT") if key in os.environ}
    env["DJANGO_SETTINGS_MODULE"] = "fishsite.settings_test"
    for key in ("NAME", "USER", "PASSWORD", "HOST", "PORT"):
        env[f"FISHSITE_TEST_DB_{key}"] = str(database[key])
    return env


def _network_guard(event, args):
    if event != "socket.connect":
        return
    _socket, address = args
    # Tests need loopback PostgreSQL and local dummy SMTP only. Reject DNS names
    # too: an external SMTP setting must fail before a connection is attempted.
    if not isinstance(address, tuple):
        raise RuntimeError("Unix network connections are disabled in tests.")
    try:
        allowed = ipaddress.ip_address(address[0]).is_loopback
    except ValueError:
        allowed = False
    if not allowed:
        raise RuntimeError("External network connections are disabled in tests.")


_guard_installed = False

def install_network_guard():
    global _guard_installed
    if not _guard_installed:
        sys.addaudithook(_network_guard)
        _guard_installed = True


class NoopNotificationBackend:
    backend_name = "test-noop"

    def send(self, notification, *, delivery_key):
        from integrations.notifications.contracts import DeliveryResult
        return DeliveryResult(accepted=True)
