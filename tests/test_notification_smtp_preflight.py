"""Local-only tests for the Mail.ru SMTP preflight command."""

import logging
import os
import smtplib
import socket
import ssl
import subprocess
import sys
import traceback
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.test import SimpleTestCase, override_settings


PROFILE = {
    "NOTIFICATION_BACKEND": "integrations.notifications.smtp.SMTPNotificationBackend",
    "NOTIFICATION_TO_EMAIL": "seller@example.test",
    "NOTIFICATION_LEASE_SECONDS": 300,
    "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
    "EMAIL_HOST": "smtp.mail.ru",
    "EMAIL_PORT": 465,
    "EMAIL_HOST_USER": "notifications@mail.ru",
    "EMAIL_HOST_PASSWORD": "synthetic-app-password-marker",
    "EMAIL_USE_TLS": False,
    "EMAIL_USE_SSL": True,
    "SMTP_ALLOW_INSECURE_LOCAL_AUTH": False,
    "EMAIL_TIMEOUT": 10,
    "DEFAULT_FROM_EMAIL": "FishSite <notifications@mail.ru>",
}


class _LogCollector(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(self.format(record))


class NotificationSMTPPreflightTests(SimpleTestCase):
    """These tests prohibit database access through SimpleTestCase."""

    def run_command(self, overrides=None):
        settings = PROFILE | (overrides or {})
        stdout = StringIO()
        stderr = StringIO()
        with override_settings(**settings):
            call_command("check_notification_smtp", stdout=stdout, stderr=stderr)
        return stdout.getvalue(), stderr.getvalue()

    def assert_safe_command_error(self, overrides, expected_code, *markers):
        stdout = StringIO()
        stderr = StringIO()
        collector = _LogCollector()
        monitored_loggers = (
            logging.getLogger(),
            logging.getLogger("integrations.notifications"),
            logging.getLogger("integrations.management.commands.check_notification_smtp"),
        )
        for logger in monitored_loggers:
            logger.addHandler(collector)
        try:
            with override_settings(**(PROFILE | overrides)):
                with self.assertRaises(CommandError) as raised:
                    call_command("check_notification_smtp", stdout=stdout, stderr=stderr)
        finally:
            for logger in monitored_loggers:
                logger.removeHandler(collector)

        error = raised.exception
        self.assertIn(expected_code, str(error))
        self.assertIsNone(error.__cause__)
        self.assertTrue(error.__suppress_context__)
        formatted_traceback = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
        rendered = "\n".join((stdout.getvalue(), stderr.getvalue(), str(error), *collector.records, formatted_traceback))
        for marker in markers:
            self.assertNotIn(marker, rendered)
        return rendered

    @staticmethod
    def client_mock():
        client = MagicMock(name="synthetic SMTP network client")
        for method in ("sendmail", "mail", "rcpt", "data"):
            getattr(client, method).side_effect = AssertionError(f"{method} must never be called")
        events = []
        client.starttls.side_effect = lambda **kwargs: events.append(("starttls", kwargs))
        client.login.side_effect = lambda *args: events.append(("login", args))
        client.quit.side_effect = lambda: events.append(("quit", ()))
        return client, events

    def assert_no_message_send(self, send_messages, message_send):
        send_messages.assert_not_called()
        message_send.assert_not_called()

    def test_m01_implicit_tls_uses_verified_context_before_auth_and_closes(self):
        client, events = self.client_mock()
        with (
            patch("django.core.mail.backends.smtp.smtplib.SMTP_SSL", return_value=client) as smtp_factory,
            patch(
                "django.core.mail.backends.smtp.EmailBackend.send_messages",
                side_effect=AssertionError("send_messages must never be called"),
            ) as send_messages,
            patch(
                "django.core.mail.message.EmailMessage.send",
                side_effect=AssertionError("EmailMessage.send must never be called"),
            ) as message_send,
        ):
            stdout, stderr = self.run_command()

        smtp_factory.assert_called_once()
        args, kwargs = smtp_factory.call_args
        self.assertEqual(args, ("smtp.mail.ru", 465))
        self.assertEqual(kwargs["timeout"], 10)
        self.assertEqual([event[0] for event in events], ["login", "quit"])
        tls_context = kwargs["context"]
        self.assertIs(tls_context.verify_mode, ssl.CERT_REQUIRED)
        self.assertIs(tls_context.check_hostname, True)
        client.login.assert_called_once_with("notifications@mail.ru", "synthetic-app-password-marker")
        client.starttls.assert_not_called()
        client.quit.assert_called_once_with()
        self.assertEqual(stderr, "")
        self.assertIn("no email was sent", stdout)
        self.assert_no_message_send(send_messages, message_send)
        for method in ("sendmail", "mail", "rcpt", "data"):
            getattr(client, method).assert_not_called()

    def test_g02_ssl_uses_verified_context_without_starttls(self):
        profile = {
            "EMAIL_PORT": 465,
            "EMAIL_USE_TLS": False,
            "EMAIL_USE_SSL": True,
        }
        client, events = self.client_mock()
        with (
            patch("django.core.mail.backends.smtp.smtplib.SMTP_SSL", return_value=client) as smtp_ssl_factory,
            patch("django.core.mail.backends.smtp.smtplib.SMTP") as smtp_factory,
            patch(
                "django.core.mail.backends.smtp.EmailBackend.send_messages",
                side_effect=AssertionError("send_messages must never be called"),
            ) as send_messages,
            patch(
                "django.core.mail.message.EmailMessage.send",
                side_effect=AssertionError("EmailMessage.send must never be called"),
            ) as message_send,
        ):
            stdout, stderr = self.run_command(profile)

        smtp_ssl_factory.assert_called_once()
        args, kwargs = smtp_ssl_factory.call_args
        self.assertEqual(args, ("smtp.mail.ru", 465))
        self.assertEqual(kwargs["timeout"], 10)
        tls_context = kwargs["context"]
        self.assertIs(tls_context.verify_mode, ssl.CERT_REQUIRED)
        self.assertIs(tls_context.check_hostname, True)
        self.assertEqual([event[0] for event in events], ["login", "quit"])
        client.starttls.assert_not_called()
        client.login.assert_called_once_with("notifications@mail.ru", "synthetic-app-password-marker")
        client.quit.assert_called_once_with()
        smtp_factory.assert_not_called()
        self.assertEqual(stderr, "")
        self.assertIn("no email was sent", stdout)
        self.assert_no_message_send(send_messages, message_send)

    def test_g03_invalid_profiles_fail_before_creating_a_connection(self):
        invalid_profiles = (
            ("custom notification backend", {"NOTIFICATION_BACKEND": "untrusted.backend.Class"}, "mailru_smtp_backend_not_selected"),
            ("custom Django mail backend", {"EMAIL_BACKEND": "untrusted.mail.Backend"}, "mailru_smtp_configuration_invalid"),
            ("wrong SMTP host", {"EMAIL_HOST": "smtp.attacker.example"}, "mailru_smtp_profile_invalid"),
            ("STARTTLS on SSL port", {"EMAIL_USE_TLS": True, "EMAIL_USE_SSL": False}, "mailru_smtp_profile_invalid"),
            ("SSL on STARTTLS port", {"EMAIL_PORT": 587}, "mailru_smtp_profile_invalid"),
            ("unsupported TLS port", {"EMAIL_PORT": 2525}, "mailru_smtp_profile_invalid"),
            ("TLS and SSL both enabled", {"EMAIL_USE_TLS": True}, "mailru_smtp_configuration_invalid"),
            ("neither TLS nor SSL with auth", {"EMAIL_USE_SSL": False}, "mailru_smtp_configuration_invalid"),
            ("neither TLS nor SSL", {"EMAIL_USE_SSL": False, "EMAIL_HOST_USER": "", "EMAIL_HOST_PASSWORD": ""}, "mailru_smtp_profile_invalid"),
            ("no credentials", {"EMAIL_HOST_USER": "", "EMAIL_HOST_PASSWORD": ""}, "mailru_smtp_profile_invalid"),
            ("username without password", {"EMAIL_HOST_PASSWORD": ""}, "mailru_smtp_configuration_invalid"),
            ("password without username", {"EMAIL_HOST_USER": "", "EMAIL_HOST_PASSWORD": "password-only-marker"}, "mailru_smtp_configuration_invalid"),
            ("invalid username", {"EMAIL_HOST_USER": "not an email"}, "mailru_smtp_profile_invalid"),
            ("display name in username", {"EMAIL_HOST_USER": "FishSite <notifications@mail.ru>"}, "mailru_smtp_profile_invalid"),
            ("newline in username", {"EMAIL_HOST_USER": "notifications@mail.ru\r\nBcc: attacker@example.test"}, "mailru_smtp_profile_invalid"),
            ("sender does not match login", {"DEFAULT_FROM_EMAIL": "Other <someone@mail.ru>"}, "mailru_smtp_profile_invalid"),
            ("insecure auth override enabled", {"SMTP_ALLOW_INSECURE_LOCAL_AUTH": True}, "mailru_smtp_profile_invalid"),
            ("invalid email port", {"EMAIL_PORT": "smtp-port-secret-marker"}, "mailru_smtp_configuration_invalid"),
        )
        with patch(
            "integrations.management.commands.check_notification_smtp.get_connection"
        ) as get_connection:
            for label, overrides, safe_code in invalid_profiles:
                with self.subTest(profile=label):
                    stdout = StringIO()
                    stderr = StringIO()
                    with override_settings(**(PROFILE | overrides)):
                        with self.assertRaises(CommandError) as raised:
                            call_command("check_notification_smtp", stdout=stdout, stderr=stderr)
                    self.assertIn(safe_code, str(raised.exception))
                    self.assertNotIn("smtp-port-secret-marker", str(raised.exception) + stdout.getvalue() + stderr.getvalue())
            get_connection.assert_not_called()

        # A non-SMTP notification backend is rejected before its constructor.
        with patch(
            "integrations.management.commands.check_notification_smtp.SMTPNotificationBackend"
        ) as selected_backend, patch(
            "integrations.management.commands.check_notification_smtp.get_connection"
        ) as get_connection:
            stdout = StringIO()
            with override_settings(**(PROFILE | {"NOTIFICATION_BACKEND": "untrusted.backend.Class"})):
                with self.assertRaises(CommandError) as raised:
                    call_command("check_notification_smtp", stdout=stdout)
            self.assertIn("mailru_smtp_backend_not_selected", str(raised.exception))
            selected_backend.assert_not_called()
            get_connection.assert_not_called()

    def test_m03_accepts_a_display_name_and_case_insensitive_matching_mailru_address(self):
        valid_case_variant = {
            "EMAIL_HOST_USER": "Notifications.User@MAIL.RU",
            "DEFAULT_FROM_EMAIL": "FishSite Notifications <notifications.user@mail.ru>",
        }
        client, _ = self.client_mock()
        with patch("django.core.mail.backends.smtp.smtplib.SMTP_SSL", return_value=client) as smtp_factory:
            stdout, _ = self.run_command(valid_case_variant)
        smtp_factory.assert_called_once()
        self.assertIn("no email was sent", stdout)

    def test_g04_command_skips_system_and_migration_checks_and_never_sends(self):
        from integrations.management.commands.check_notification_smtp import Command

        self.assertEqual(Command.requires_system_checks, [])
        self.assertFalse(Command.requires_migrations_checks)
        client, _ = self.client_mock()
        with (
            patch.object(BaseCommand, "check", side_effect=AssertionError("system checks ran")) as check,
            patch.object(BaseCommand, "check_migrations", side_effect=AssertionError("migration checks ran")) as migrations,
            patch("django.core.mail.backends.smtp.smtplib.SMTP_SSL", return_value=client),
            patch("django.core.mail.backends.smtp.EmailBackend.send_messages", side_effect=AssertionError("send attempted")) as send_messages,
            patch("django.core.mail.message.EmailMessage.send", side_effect=AssertionError("send attempted")) as message_send,
        ):
            stdout, _ = self.run_command()

        check.assert_not_called()
        migrations.assert_not_called()
        self.assertIn("no email was sent", stdout)
        self.assert_no_message_send(send_messages, message_send)
        for method in ("sendmail", "mail", "rcpt", "data"):
            getattr(client, method).assert_not_called()

    def test_m05_tls_handshake_and_certificate_failures_never_authenticate_or_fallback(self):
        failures = (
            ("TLS handshake", RuntimeError("TLS raw failure secret-marker")),
            ("certificate verification", ssl.SSLCertVerificationError(1, "certificate raw failure secret-marker")),
        )
        for label, failure in failures:
            with self.subTest(failure=label):
                client, _ = self.client_mock()
                with (
                    patch("django.core.mail.backends.smtp.smtplib.SMTP_SSL", side_effect=failure) as smtp_factory,
                    patch("django.core.mail.backends.smtp.EmailBackend.send_messages", side_effect=AssertionError("send attempted")) as send_messages,
                    patch("django.core.mail.message.EmailMessage.send", side_effect=AssertionError("send attempted")) as message_send,
                ):
                    rendered = self.assert_safe_command_error(
                        {},
                        "mailru_smtp_connection_failed",
                        "secret-marker",
                        "notifications@mail.ru",
                        "synthetic-app-password-marker",
                        "seller@example.test",
                    )

                smtp_factory.assert_called_once()
                client.login.assert_not_called()
                self.assert_no_message_send(send_messages, message_send)
                self.assertNotIn("no email was sent", rendered)

    def test_g06_network_and_unexpected_errors_never_escape_safe_diagnostics(self):
        secret_user = "preflight-user-secret-marker@mail.ru"
        secret_password = "preflight-password-secret-marker"
        secret_recipient = "preflight-recipient-secret-marker@example.test"
        server_text = "raw-server-reply-secret-marker"
        common = {
            "EMAIL_HOST_USER": secret_user,
            "EMAIL_HOST_PASSWORD": secret_password,
            "DEFAULT_FROM_EMAIL": f"FishSite <{secret_user}>",
            "NOTIFICATION_TO_EMAIL": secret_recipient,
        }

        cases = (
            ("client constructor", "smtp_constructor", RuntimeError(f"constructor {server_text}")),
            ("get_connection factory", "connection_factory", RuntimeError(f"factory {server_text}")),
            (
                "SMTP 535 authentication",
                "login",
                smtplib.SMTPAuthenticationError(535, f"authentication {server_text}".encode()),
            ),
            ("socket timeout", "ssl_constructor", socket.timeout(f"timeout {server_text}")),
            ("unexpected open failure", "open", RuntimeError(f"unexpected {server_text}")),
        )
        for label, operation, exception in cases:
            with self.subTest(failure=label):
                client, _ = self.client_mock()
                if operation == "login":
                    client.login.side_effect = exception
                    factory_patch = patch("django.core.mail.backends.smtp.smtplib.SMTP_SSL", return_value=client)
                elif operation == "ssl_constructor":
                    factory_patch = patch("django.core.mail.backends.smtp.smtplib.SMTP_SSL", side_effect=exception)
                elif operation == "open":
                    factory_patch = patch("integrations.management.commands.check_notification_smtp.get_connection")
                elif operation == "connection_factory":
                    factory_patch = patch(
                        "integrations.management.commands.check_notification_smtp.get_connection",
                        side_effect=exception,
                    )
                else:
                    factory_patch = patch("django.core.mail.backends.smtp.smtplib.SMTP_SSL", side_effect=exception)

                with factory_patch as factory, patch(
                    "django.core.mail.backends.smtp.EmailBackend.send_messages", side_effect=AssertionError("send attempted")
                ) as send_messages, patch(
                    "django.core.mail.message.EmailMessage.send", side_effect=AssertionError("send attempted")
                ) as message_send:
                    if operation == "open":
                        factory.return_value.open.side_effect = exception
                    rendered = self.assert_safe_command_error(
                        common,
                        "mailru_smtp_connection_failed",
                        secret_user,
                        secret_password,
                        secret_recipient,
                        server_text,
                    )

                self.assertNotIn("no email was sent", rendered)
                self.assert_no_message_send(send_messages, message_send)
                if operation in {"smtp_constructor", "connection_factory", "login", "ssl_constructor"}:
                    factory.assert_called_once()
                if operation in {"login", "ssl_constructor"}:
                    if operation == "login":
                        client.login.assert_called_once()
                    else:
                        client.login.assert_not_called()
                elif operation == "open":
                    factory.return_value.close.assert_called_once()

    def test_g07_cleanup_failure_does_not_report_success_or_hide_primary_failure(self):
        cases = (
            ("close after success", True, None, "mailru_smtp_cleanup_failed"),
            ("close after generic open failure", None, RuntimeError("primary raw secret-marker"), "mailru_smtp_connection_failed"),
            (
                "close after SMTP AUTH 535 failure",
                None,
                smtplib.SMTPAuthenticationError(535, b"primary raw secret-marker"),
                "mailru_smtp_connection_failed",
            ),
        )
        for label, open_result, primary_error, expected_code in cases:
            with self.subTest(cleanup=label):
                connection = MagicMock()
                connection.open.return_value = open_result
                connection.close.side_effect = RuntimeError("cleanup raw cleanup-marker")
                if primary_error:
                    connection.open.side_effect = primary_error
                with patch(
                    "integrations.management.commands.check_notification_smtp.get_connection",
                    return_value=connection,
                ):
                    rendered = self.assert_safe_command_error(
                        {},
                        expected_code,
                        "primary raw secret-marker",
                        "cleanup raw cleanup-marker",
                    )
                connection.close.assert_called_once_with()
                self.assertNotIn("no email was sent", rendered)

    def test_g08_false_or_none_open_results_are_closed_and_rejected(self):
        for open_result in (False, None):
            with self.subTest(open_result=open_result):
                connection = MagicMock()
                connection.open.return_value = open_result
                with patch(
                    "integrations.management.commands.check_notification_smtp.get_connection",
                    return_value=connection,
                ):
                    rendered = self.assert_safe_command_error(
                        {},
                        "mailru_smtp_connection_not_established",
                    )
                connection.close.assert_called_once_with()
                self.assertNotIn("no email was sent", rendered)

    def test_settings_parse_errors_with_traceback_name_setting_but_hide_raw_values(self):
        root = Path(__file__).resolve().parents[1]
        numeric_settings = (
            ("SMTP_PORT", "integer"),
            ("SMTP_TIMEOUT_SECONDS", "number"),
            ("NOTIFICATION_MAX_ATTEMPTS", "integer"),
            ("NOTIFICATION_RETRY_BASE_SECONDS", "integer"),
            ("NOTIFICATION_RETRY_MAX_SECONDS", "integer"),
            ("NOTIFICATION_SCAN_INTERVAL_SECONDS", "integer"),
            ("NOTIFICATION_LEASE_SECONDS", "integer"),
        )
        safe_defaults = {
            "SMTP_PORT": "465",
            "SMTP_TIMEOUT_SECONDS": "10",
            "NOTIFICATION_MAX_ATTEMPTS": "5",
            "NOTIFICATION_RETRY_BASE_SECONDS": "30",
            "NOTIFICATION_RETRY_MAX_SECONDS": "3600",
            "NOTIFICATION_SCAN_INTERVAL_SECONDS": "60",
            "NOTIFICATION_LEASE_SECONDS": "300",
        }
        for setting_name, expected_type in numeric_settings:
            with self.subTest(setting=setting_name):
                marker = f"raw-value-secret-marker-{setting_name}"
                env = os.environ.copy()
                env.update(
                    {
                        "DJANGO_SETTINGS_MODULE": "fishsite.settings",
                        "FISHSITE_SKIP_DOTENV": "1",
                        "SMTP_HOST": "localhost",
                        "SMTP_USERNAME": "",
                        "SMTP_PASSWORD": "",
                        "SMTP_USE_TLS": "false",
                        "SMTP_USE_SSL": "false",
                        "SMTP_ALLOW_INSECURE_LOCAL_AUTH": "false",
                        "NOTIFICATION_FROM_EMAIL": "notifications@example.test",
                        "NOTIFICATION_TO_EMAIL": "seller@example.test",
                    }
                )
                env.update(safe_defaults)
                env[setting_name] = marker
                completed = subprocess.run(
                    [sys.executable, "manage.py", "check_notification_smtp", "--traceback"],
                    cwd=root,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                rendered = completed.stdout + completed.stderr
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn(setting_name, completed.stderr)
                self.assertIn(expected_type, completed.stderr)
                self.assertNotIn(marker, rendered)
