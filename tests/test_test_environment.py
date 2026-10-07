"""Regression coverage for destructive-test and external-mail boundaries."""
import copy
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock, patch

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test import SimpleTestCase, TransactionTestCase

from fishsite.test_db.base import DatabaseWrapper, GuardedOperations
from fishsite.test_safety import (
    _network_guard, validate_database, validate_live_connection, worker_environment,
)

ROOT = Path(__file__).resolve().parents[1]


class TestEnvironmentGuards(SimpleTestCase):
    def config(self):
        config = copy.deepcopy(settings.DATABASES["default"])
        config["NAME"] = "test_fishsite"
        return config

    def test_rejects_primary_endpoint_role_database_and_connection_options(self):
        for key, value in (
            ("HOST", "localhost"), ("PORT", "5432"), ("USER", "frankheels"),
            ("NAME", "fishsite"), ("PASSWORD", ""),
            ("ENGINE", "django.db.backends.postgresql"),
            ("OPTIONS", {"service": "primary"}), ("TEST", {"NAME": "fishsite"}),
        ):
            with self.subTest(key=key):
                with self.assertRaises(ImproperlyConfigured):
                    validate_database(self.config() | {key: value}, maintenance=True)
        validate_database(self.config())
        # postgres is reserved for Django's maintenance connection, never ORM.
        with self.assertRaises(ImproperlyConfigured):
            DatabaseWrapper(self.config() | {"NAME": "postgres"}).get_connection_params()
        with self.assertRaises(ImproperlyConfigured):
            validate_database(self.config() | {"NAME": "postgres"})

    def test_live_identity_rejects_wrong_cluster_role_privileges_or_database(self):
        good = ("test_fishsite", "fishsite_tester", 15432, "fishsite-tests", False, True, "127.0.0.1", "127.0.0.1")
        for index, value in enumerate(("fishsite", "app_user", 5432, "primary", True, False, "192.0.2.1", "192.0.2.2")):
            raw = Mock()
            raw.cursor.return_value.__enter__ = Mock()
            raw.cursor.return_value.__exit__ = Mock()
            bad = list(good)
            bad[index] = value
            raw.cursor.return_value.__enter__.return_value.fetchone.return_value = tuple(bad)
            with self.subTest(index=index), self.assertRaises(ImproperlyConfigured):
                validate_live_connection(raw)

    def test_flush_checks_live_connection_before_generating_truncate(self):
        wrapper = Mock(settings_dict=self.config())
        with patch("fishsite.test_db.base.validate_live_connection", side_effect=ImproperlyConfigured), \
             patch("django.db.backends.postgresql.operations.DatabaseOperations.sql_flush") as flush:
            with self.assertRaises(ImproperlyConfigured):
                GuardedOperations(wrapper).sql_flush(None, ["orders_order"])
            flush.assert_not_called()

    def test_worker_receives_active_db_without_application_secrets(self):
        with patch.dict(os.environ, {"SMTP_PASSWORD": "secret-marker", "DJANGO_DB_NAME": "fishsite"}):
            env = worker_environment(self.config())
        self.assertEqual(env["FISHSITE_TEST_DB_NAME"], "test_fishsite")
        self.assertEqual(env["DJANGO_SETTINGS_MODULE"], "fishsite.settings_test")
        self.assertNotIn("SMTP_PASSWORD", env)
        self.assertNotIn("DJANGO_DB_NAME", env)

    def test_network_guard_blocks_external_smtp_even_with_overridden_settings(self):
        for host in ("smtp.gmail.com", "8.8.8.8", "2001:4860:4860::8888"):
            with self.subTest(host=host), self.assertRaises(RuntimeError):
                _network_guard("socket.connect", (None, (host, 587)))
        _network_guard("socket.connect", (None, ("127.0.0.1", 15432)))

    def run_python(self, args, env=None):
        return subprocess.run(
            [sys.executable, *args], cwd=ROOT, env=env,
            capture_output=True, text=True, timeout=15,
        )

    def test_retired_runner_refuses_every_old_mode_without_importing_django(self):
        for args in ([], ["--confirm-primary", "fishsite"], ["--apply-migrations", "--migrations-only"]):
            result = self.run_python(["scripts/run_primary_request_scenarios.py", *args])
            self.assertEqual(result.returncode, 2)
            self.assertIn("disabled", result.stderr)

    def test_explicit_primary_profile_is_refused_before_loading_settings(self):
        result = self.run_python(["manage.py", "test", "--settings=fishsite.settings", "--noinput"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Tests require", result.stderr)

    def test_profile_does_not_read_dotenv_or_ambient_gmail_and_requires_test_password(self):
        env = worker_environment(self.config())
        env.update({"SMTP_PASSWORD": "secret-marker", "SMTP_PORT": "invalid-primary-value"})
        code = """
from unittest.mock import patch
with patch('dotenv.load_dotenv', side_effect=AssertionError('primary dotenv read')):
    from django.conf import settings
    assert settings.EMAIL_HOST_PASSWORD == ''
    assert settings.DATABASES['default']['USER'] == 'fishsite_tester'
"""
        result = self.run_python(["-c", code], env)
        self.assertEqual(result.returncode, 0, result.stderr)
        env["FISHSITE_TEST_DB_PASSWORD"] = ""
        result = self.run_python(["-c", code], env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FISHSITE_TEST_DB_PASSWORD is required", result.stderr)
        self.assertNotIn("secret-marker", result.stderr)


class TestDatabaseIdentity(TransactionTestCase):
    def test_live_cluster_has_no_primary_database_and_role_cannot_connect_to_it(self):
        import psycopg
        validate_database(connection.settings_dict)
        connection.ensure_connection()
        validate_live_connection(connection.connection)
        with connection.cursor() as cursor:
            cursor.execute("SELECT datname FROM pg_database WHERE datname = 'fishsite'")
            self.assertIsNone(cursor.fetchone())
        params = connection.get_connection_params() | {"dbname": "fishsite"}
        with self.assertRaises(psycopg.OperationalError) as raised:
            psycopg.connect(**params)
        self.assertIn("pg_hba.conf rejects connection", str(raised.exception))
