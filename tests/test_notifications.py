"""Real PostgreSQL and loopback SMTP coverage for the notification worker."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from email import policy
from email.parser import BytesParser
import io
import json
import os
import signal
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import time
import uuid
from unittest.mock import MagicMock, patch

import psycopg

from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError, close_old_connections, connections, transaction
from django.test import Client, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from catalog.models import Category, Product
from content.consent_revisions import get_current_consent_revision
from inquiries.models import Inquiry
from integrations.models import NotificationOutbox
from integrations.notifications.contracts import (
    DeliveryResult,
    NotificationConfigurationError,
    PermanentDeliveryError,
    TemporaryDeliveryError,
)
from integrations.notifications.payloads import RequestNotification, build_notification
from integrations.notifications.queue import NotificationQueue
from integrations.notifications.smtp import SMTPNotificationBackend
from integrations.notifications.worker import (
    NotificationWorker,
    PsycopgNotificationListener,
    load_backend,
)
from orders.models import Order, OrderItem


class _SMTPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        server = self.server
        if server.greeting_delay:
            time.sleep(server.greeting_delay)
        if not self._write(b"220 localhost test SMTP\r\n"):
            return

        while True:
            raw = self.rfile.readline(65537)
            if not raw:
                return
            command = raw.decode("ascii", errors="replace").rstrip("\r\n")
            server.record_command(command)
            verb = command.split(" ", 1)[0].upper()

            if verb in {"EHLO", "HELO"}:
                if server.auth_reply:
                    response = (
                        b"250-localhost\r\n250-SIZE 1048576\r\n"
                        b"250-AUTH PLAIN LOGIN\r\n250 HELP\r\n"
                    )
                else:
                    response = b"250-localhost\r\n250 SIZE 1048576\r\n"
                if not self._write(response):
                    return
            elif verb == "AUTH":
                if not self._write(server.auth_reply or b"235 2.7.0 authenticated\r\n"):
                    return
            elif verb == "MAIL":
                if not self._write(b"250 2.1.0 sender accepted\r\n"):
                    return
            elif verb == "RCPT":
                if not self._write(server.recipient_reply):
                    return
            elif verb == "DATA":
                if not self._write(b"354 end with dot\r\n"):
                    return
                message = []
                while True:
                    line = self.rfile.readline(65537)
                    if not line:
                        return
                    if line in {b".\r\n", b".\n"}:
                        break
                    if line.startswith(b".."):
                        line = line[1:]
                    message.append(line)
                server.record_message(b"".join(message))
                if server.disconnect_after_data:
                    try:
                        self.request.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return
                if server.data_delay:
                    time.sleep(server.data_delay)
                if not self._write(server.data_reply):
                    return
            elif verb == "RSET":
                if not self._write(b"250 reset\r\n"):
                    return
            elif verb == "NOOP":
                if not self._write(b"250 ok\r\n"):
                    return
            elif verb == "QUIT":
                self._write(b"221 bye\r\n")
                return
            else:
                if not self._write(b"250 ok\r\n"):
                    return

    def _write(self, response):
        try:
            self.wfile.write(response)
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False


class _LocalSMTPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        *,
        recipient_reply=b"250 2.1.5 recipient accepted\r\n",
        data_reply=b"250 2.0.0 accepted\r\n",
        auth_reply=None,
        greeting_delay=0,
        data_delay=0,
        disconnect_after_data=False,
    ):
        self.recipient_reply = recipient_reply
        self.data_reply = data_reply
        self.auth_reply = auth_reply
        self.greeting_delay = greeting_delay
        self.data_delay = data_delay
        self.disconnect_after_data = disconnect_after_data
        self.messages = []
        self.commands = []
        self._record_lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), _SMTPHandler)

    def record_command(self, command):
        with self._record_lock:
            self.commands.append(command)

    def record_message(self, message):
        with self._record_lock:
            self.messages.append(message)

    def __enter__(self):
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=2)


class _RecordingBackend:
    backend_name = "test-recording"

    def __init__(self, *, after_send=None):
        self.after_send = after_send
        self.notifications = []
        self._lock = threading.Lock()

    def send(self, notification, *, delivery_key):
        with self._lock:
            self.notifications.append((notification, delivery_key))
            count = len(self.notifications)
        if self.after_send:
            self.after_send(notification, count)
        return DeliveryResult()


class NotificationIntegrationTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.assertEqual(connections["default"].vendor, "postgresql")
        self.assertTrue(
            connections["default"].settings_dict["NAME"].startswith("test_"),
            "Notification integration tests must use Django's disposable test database.",
        )
        self.marker = uuid.uuid4().hex
        self.category = Category.objects.create(
            name=f"Notification category {self.marker}",
            slug=f"notification-{self.marker}",
        )
        self.product = Product.objects.create(
            name=f"Notification fish {self.marker}",
            slug=f"notification-fish-{self.marker}",
            category=self.category,
            price_per_kg="12.25",
            is_published=True,
        )
        self.consent_revision = get_current_consent_revision()

    def tearDown(self):
        close_old_connections()

    def retail_request(self, *, product=None, fulfillment="delivery", **overrides):
        values = {
            "name": f"Retail contact {self.marker}",
            "phone": "+7 (900) 111-22-33",
            "email": "retail-contact@example.test",
            "fulfillment_method": fulfillment,
            "delivery_address": "Test delivery address",
            "comment": "Test retail comment",
            "privacy_consent": True,
            "privacy_consent_at": timezone.now(),
            "privacy_consent_revision": self.consent_revision,
        }
        values.update(overrides)
        order = Order.objects.create(**values)
        item_product = product or self.product
        item = OrderItem.objects.create(
            order=order,
            product=item_product,
            product_name_snapshot=item_product.name,
            price_per_kg_snapshot=item_product.price_per_kg,
            desired_weight="2.000",
        )
        outbox = NotificationOutbox.objects.create(order=order)
        return order, item, outbox

    def wholesale_request(self, **overrides):
        values = {
            "contact_name": f"Wholesale contact {self.marker}",
            "company": f"Wholesale company {self.marker}",
            "phone": "+7 (900) 444-55-66",
            "email": "wholesale-contact@example.test",
            "privacy_consent": True,
            "privacy_consent_at": timezone.now(),
            "privacy_consent_revision": self.consent_revision,
        }
        values.update(overrides)
        inquiry = Inquiry.objects.create(**values)
        return inquiry, NotificationOutbox.objects.create(inquiry=inquiry)

    @staticmethod
    def smtp_settings(server, **overrides):
        values = {
            "NOTIFICATION_BACKEND": "integrations.notifications.smtp.SMTPNotificationBackend",
            "NOTIFICATION_TO_EMAIL": "seller@example.test",
            "NOTIFICATION_LEASE_SECONDS": 30,
            "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
            "EMAIL_HOST": "127.0.0.1",
            "EMAIL_PORT": server.server_address[1],
            "EMAIL_HOST_USER": "",
            "EMAIL_HOST_PASSWORD": "",
            "EMAIL_USE_TLS": False,
            "EMAIL_USE_SSL": False,
            "SMTP_ALLOW_INSECURE_LOCAL_AUTH": False,
            "EMAIL_TIMEOUT": 0.4,
            "DEFAULT_FROM_EMAIL": "FishSite <notifications@example.test>",
        }
        values.update(overrides)
        return override_settings(**values)

    def assert_no_smtp_sensitive_log_values(self, captured, *values):
        rendered = "\n".join(captured.output)
        for value in values:
            self.assertNotIn(value, rendered)

    def test_payload_is_an_immutable_saved_snapshot_after_catalog_edit_and_delete(self):
        order, item, outbox = self.retail_request()
        notification = build_notification(outbox.pk)

        self.product.name = "Renamed after request"
        self.product.price_per_kg = "99.99"
        self.product.is_published = False
        self.product.save()
        self.product.delete()

        refreshed = build_notification(outbox.pk)
        self.assertEqual(refreshed.items[0].product_name, f"Notification fish {self.marker}")
        self.assertEqual(refreshed.items[0].price_per_kg, item.price_per_kg_snapshot)
        self.assertEqual(refreshed.items[0].weight, item.desired_weight)
        self.assertEqual(refreshed.items[0].line_total, item.line_total)
        self.assertEqual(refreshed.preliminary_total, item.line_total)
        self.assertEqual(refreshed.fulfillment_method, Order.FulfillmentMethod.DELIVERY)
        self.assertEqual(refreshed.delivery_address, "Test delivery address")
        self.assertEqual(refreshed.comment, "Test retail comment")
        self.assertEqual(refreshed.email, "retail-contact@example.test")
        self.assertEqual(order.order_number, refreshed.request_number)
        self.assertIsNone(OrderItem.objects.get(pk=item.pk).product_id)
        self.assertIsInstance(notification, RequestNotification)
        self.assertEqual(notification.delivery_key, f"retail:{order.submission_key}")
        with self.assertRaises((AttributeError, TypeError)):
            notification.contact_name = "mutated"
        with self.assertRaises((AttributeError, TypeError)):
            notification.items[0].product_name = "mutated"

        inquiry, wholesale_outbox = self.wholesale_request(need="Historical need retained")
        wholesale = build_notification(wholesale_outbox.pk)
        self.assertEqual(wholesale.kind, "wholesale")
        self.assertEqual(wholesale.company, f"Wholesale company {self.marker}")
        self.assertEqual(wholesale.need, "Historical need retained")
        self.assertEqual(wholesale.delivery_key, f"wholesale:{inquiry.submission_key}")

    def test_smtp_delivers_retail_and_wholesale_and_reuses_message_id_for_retries(self):
        order, _, retail_outbox = self.retail_request(fulfillment=Order.FulfillmentMethod.PICKUP)
        inquiry, wholesale_outbox = self.wholesale_request(need="Historical wholesale need")
        retail = build_notification(retail_outbox.pk)
        wholesale = build_notification(wholesale_outbox.pk)

        with _LocalSMTPServer() as server, self.smtp_settings(server):
            backend = SMTPNotificationBackend()
            backend.send(retail, delivery_key=retail.delivery_key)
            backend.send(retail, delivery_key=retail.delivery_key)
            backend.send(wholesale, delivery_key=wholesale.delivery_key)

        self.assertEqual(len(server.messages), 3)
        parsed = [BytesParser(policy=policy.default).parsebytes(raw) for raw in server.messages]
        message_ids = [message["Message-ID"] for message in parsed]
        self.assertEqual(message_ids[0], message_ids[1])
        self.assertNotEqual(message_ids[0], message_ids[2])
        self.assertIn(order.order_number, str(parsed[0]["Subject"]))
        self.assertIn(inquiry.inquiry_number, str(parsed[2]["Subject"]))
        retail_body = parsed[0].get_payload(decode=True).decode(parsed[0].get_content_charset() or "utf-8")
        wholesale_body = parsed[2].get_payload(decode=True).decode(parsed[2].get_content_charset() or "utf-8")
        self.assertIn("Самовывоз", retail_body)
        self.assertIn("Test retail comment", retail_body)
        self.assertIn("12,25", retail_body)
        self.assertIn("Historical wholesale need", wholesale_body)
        self.assertIn(f"Wholesale company {self.marker}", wholesale_body)

    def test_smtp_maps_temporary_permanent_auth_timeout_and_accepted_but_unconfirmed(self):
        _, _, outbox = self.retail_request()
        notification = build_notification(outbox.pk)

        with _LocalSMTPServer(recipient_reply=b"450 4.2.0 mailbox busy\r\n") as server:
            with self.smtp_settings(server), self.assertRaises(TemporaryDeliveryError) as raised:
                SMTPNotificationBackend().send(notification, delivery_key=notification.delivery_key)
            self.assertEqual(raised.exception.code, "smtp_recipient_temporary")

        with _LocalSMTPServer(recipient_reply=b"550 5.1.1 recipient refused\r\n") as server:
            with self.smtp_settings(server), self.assertRaises(PermanentDeliveryError) as raised:
                SMTPNotificationBackend().send(notification, delivery_key=notification.delivery_key)
            self.assertEqual(raised.exception.code, "smtp_recipient_refused")

        with _LocalSMTPServer(auth_reply=b"535 5.7.8 auth rejected secret-server-text\r\n") as server:
            with self.smtp_settings(
                server,
                EMAIL_HOST_USER="test-user",
                EMAIL_HOST_PASSWORD="test-password-marker",
                SMTP_ALLOW_INSECURE_LOCAL_AUTH=True,
            ), self.assertRaises(PermanentDeliveryError) as raised:
                SMTPNotificationBackend().send(notification, delivery_key=notification.delivery_key)
            self.assertEqual(raised.exception.code, "smtp_response_permanent")

        with _LocalSMTPServer(data_delay=0.8) as server:
            with self.smtp_settings(server, EMAIL_TIMEOUT=0.1), self.assertRaises(TemporaryDeliveryError) as raised:
                SMTPNotificationBackend().send(notification, delivery_key=notification.delivery_key)
            self.assertEqual(raised.exception.code, "smtp_connection_unavailable")
            self.assertEqual(len(server.messages), 1, "The SMTP peer accepted DATA before withholding its reply.")

        retry_server = _LocalSMTPServer()
        with retry_server, self.smtp_settings(retry_server):
            SMTPNotificationBackend().send(notification, delivery_key=notification.delivery_key)
        first = BytesParser(policy=policy.default).parsebytes(server.messages[0])
        second = BytesParser(policy=policy.default).parsebytes(retry_server.messages[0])
        self.assertEqual(first["Message-ID"], second["Message-ID"])

        dropped_server = _LocalSMTPServer(disconnect_after_data=True)
        with dropped_server, self.smtp_settings(dropped_server), self.assertRaises(TemporaryDeliveryError):
            SMTPNotificationBackend().send(notification, delivery_key=notification.delivery_key)
        self.assertEqual(len(dropped_server.messages), 1)

    def test_worker_logs_safe_codes_without_smtp_error_contact_or_credential_values(self):
        order, _, outbox = self.retail_request(
            name=f"Sensitive contact {self.marker}",
            email=f"sensitive-{self.marker}@example.test",
        )
        server_error = "auth rejected raw-server-marker"
        with _LocalSMTPServer(auth_reply=f"535 5.7.8 {server_error}\r\n".encode()) as server:
            with self.smtp_settings(
                server,
                EMAIL_HOST_USER="sensitive-smtp-user",
                EMAIL_HOST_PASSWORD="sensitive-smtp-password",
                SMTP_ALLOW_INSECURE_LOCAL_AUTH=True,
            ), self.assertLogs("integrations.notifications", level="INFO") as captured:
                self.assertEqual(NotificationWorker(SMTPNotificationBackend()).run_once(), 1)

        outbox.refresh_from_db()
        self.assertEqual(outbox.status, NotificationOutbox.Status.MANUAL_REVIEW)
        self.assertEqual(outbox.last_error, "smtp_response_permanent")
        self.assert_no_smtp_sensitive_log_values(
            captured,
            order.name,
            order.email,
            order.phone,
            "sensitive-smtp-user",
            "sensitive-smtp-password",
            "raw-server-marker",
        )

    def test_plaintext_smtp_auth_requires_explicit_flag_and_numeric_loopback_host(self):
        username = "local-test-user"
        password = "local-test-password"
        with _LocalSMTPServer() as server:
            rejected = (
                ("127.0.0.1", False),
                ("mail.example.test", True),
                ("203.0.113.20", True),
            )
            for host, allow_local_auth in rejected:
                with self.subTest(host=host, allow_local_auth=allow_local_auth):
                    with self.smtp_settings(
                        server,
                        EMAIL_HOST=host,
                        EMAIL_HOST_USER=username,
                        EMAIL_HOST_PASSWORD=password,
                        SMTP_ALLOW_INSECURE_LOCAL_AUTH=allow_local_auth,
                    ), self.assertRaises(NotificationConfigurationError) as raised:
                        SMTPNotificationBackend()
                    self.assertEqual(raised.exception.code, "smtp_auth_requires_encryption")

            with self.smtp_settings(
                server,
                EMAIL_HOST="localhost",
                EMAIL_HOST_USER=username,
                EMAIL_HOST_PASSWORD=password,
                SMTP_ALLOW_INSECURE_LOCAL_AUTH=True,
            ), patch("socket.getaddrinfo", side_effect=AssertionError("DNS lookup must not be used")) as resolver:
                with self.assertRaises(NotificationConfigurationError) as raised:
                    SMTPNotificationBackend()
                self.assertEqual(raised.exception.code, "smtp_auth_requires_encryption")
                resolver.assert_not_called()

            with self.smtp_settings(
                server,
                EMAIL_HOST="127.0.0.1",
                EMAIL_HOST_USER=username,
                EMAIL_HOST_PASSWORD=password,
                SMTP_ALLOW_INSECURE_LOCAL_AUTH=True,
            ):
                backend = SMTPNotificationBackend()
                self.assertTrue(backend.configuration_validated)

    def test_worker_command_rejects_plaintext_smtp_auth_before_claiming(self):
        _, _, outbox = self.retail_request()
        with _LocalSMTPServer() as server, self.smtp_settings(
            server,
            EMAIL_HOST_USER="local-test-user",
            EMAIL_HOST_PASSWORD="local-test-password",
        ):
            with self.assertRaisesRegex(CommandError, "smtp_auth_requires_encryption"):
                call_command("process_notifications", once=True, stdout=io.StringIO())

        outbox.refresh_from_db()
        self.assertEqual(outbox.status, NotificationOutbox.Status.PENDING)
        self.assertEqual(outbox.attempt_count, 0)
        self.assertIsNone(outbox.lease_token)
        self.assertEqual(server.commands, [], "Invalid credentials must be rejected before connecting to SMTP.")

    def test_invalid_smtp_boolean_environment_values_name_only_the_setting(self):
        base_dir = os.path.dirname(os.path.dirname(__file__))
        invalid_marker = "invalid-smtp-boolean-secret-marker"
        for setting_name in ("SMTP_USE_TLS", "SMTP_USE_SSL", "SMTP_ALLOW_INSECURE_LOCAL_AUTH"):
            with self.subTest(setting_name=setting_name):
                env = os.environ.copy()
                env.update(
                    {
                        "DJANGO_SETTINGS_MODULE": "fishsite.settings",
                        "FISHSITE_SKIP_DOTENV": "1",
                        "SMTP_USE_TLS": "false",
                        "SMTP_USE_SSL": "false",
                        "SMTP_ALLOW_INSECURE_LOCAL_AUTH": "false",
                    }
                )
                env[setting_name] = invalid_marker
                completed = subprocess.run(
                    [sys.executable, "-c", "from django.conf import settings; print(settings.EMAIL_USE_TLS)"],
                    cwd=base_dir,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("ImproperlyConfigured", completed.stderr)
                self.assertIn(setting_name, completed.stderr)
                self.assertNotIn(invalid_marker, completed.stdout + completed.stderr)

    def test_invalid_default_smtp_configuration_claims_nothing_but_custom_backend_runs(self):
        _, _, outbox = self.retail_request()
        with override_settings(
            NOTIFICATION_BACKEND="integrations.notifications.smtp.SMTPNotificationBackend",
            EMAIL_HOST="",
            DEFAULT_FROM_EMAIL="",
            NOTIFICATION_TO_EMAIL="",
        ):
            with self.assertRaises(NotificationConfigurationError):
                load_backend()
            outbox.refresh_from_db()
            self.assertEqual(outbox.status, NotificationOutbox.Status.PENDING)
            self.assertEqual(outbox.attempt_count, 0)

        backend_path = f"{__name__}.NoopNotificationBackend"
        with override_settings(NOTIFICATION_BACKEND=backend_path, EMAIL_HOST="", NOTIFICATION_TO_EMAIL=""):
            backend = load_backend()
            self.assertEqual(NotificationWorker(backend).run_once(), 1)
            outbox.refresh_from_db()
            self.assertEqual(outbox.status, NotificationOutbox.Status.SENT)

    def test_claim_tokens_reject_expired_and_stale_finalization_and_retry_delay_caps(self):
        _, _, outbox = self.retail_request()
        fixed_now = timezone.now()
        queue = NotificationQueue(
            max_attempts=3,
            retry_base_seconds=2,
            retry_max_seconds=3,
            lease_seconds=10,
            now=lambda: fixed_now,
        )
        first = queue.claim_next()
        self.assertEqual(first.attempt_count, 1)
        self.assertTrue(queue.mark_temporary_failure(first, "smtp_busy"))
        outbox.refresh_from_db()
        self.assertEqual(outbox.next_attempt_at, fixed_now + timedelta(seconds=2))
        self.assertIsNone(queue.claim_next(), "A retry must wait until next_attempt_at.")

        fixed_now = outbox.next_attempt_at
        second = queue.claim_next()
        self.assertEqual(second.attempt_count, 2)
        self.assertTrue(queue.mark_temporary_failure(second, "smtp_busy"))
        outbox.refresh_from_db()
        self.assertEqual(outbox.next_attempt_at, fixed_now + timedelta(seconds=3))

        fixed_now = outbox.next_attempt_at
        third = queue.claim_next()
        self.assertEqual(third.attempt_count, 3)
        self.assertTrue(queue.mark_temporary_failure(third, "smtp_busy"))
        outbox.refresh_from_db()
        self.assertEqual(outbox.status, NotificationOutbox.Status.MANUAL_REVIEW)
        self.assertEqual(outbox.last_error, "smtp_busy")

        _, _, stale_outbox = self.retail_request()
        stale_queue = NotificationQueue(max_attempts=3, lease_seconds=10, now=lambda: fixed_now)
        old_owner = stale_queue.claim_next()
        NotificationOutbox.objects.filter(pk=stale_outbox.pk).update(
            lease_expires_at=fixed_now - timedelta(seconds=1)
        )
        self.assertFalse(stale_queue.mark_sent(old_owner), "An expired owner cannot finalize a task.")
        new_owner = stale_queue.claim_next()
        self.assertNotEqual(old_owner.lease_token, new_owner.lease_token)
        self.assertFalse(stale_queue.mark_sent(old_owner), "A stale token cannot overwrite a newer claim.")
        self.assertTrue(stale_queue.mark_sent(new_owner))

    def test_expired_exhausted_lease_moves_to_manual_review_and_once_drains_next_task(self):
        _, _, exhausted = self.retail_request()
        _, _, ready = self.retail_request()
        now = timezone.now()
        NotificationOutbox.objects.filter(pk=exhausted.pk).update(
            status=NotificationOutbox.Status.PROCESSING,
            attempt_count=1,
            lease_token=uuid.uuid4(),
            lease_expires_at=now - timedelta(seconds=1),
        )
        queue = NotificationQueue(max_attempts=1, lease_seconds=30, now=lambda: now)
        backend = _RecordingBackend()
        worker = NotificationWorker(backend, queue=queue)

        self.assertEqual(worker.run_once(), 1)
        exhausted.refresh_from_db()
        ready.refresh_from_db()
        self.assertEqual(exhausted.status, NotificationOutbox.Status.MANUAL_REVIEW)
        self.assertEqual(exhausted.last_error, "attempt_limit_reached")
        self.assertEqual(ready.status, NotificationOutbox.Status.SENT)
        self.assertEqual(len(backend.notifications), 1)

    def test_temporary_failure_at_attempt_limit_logs_manual_review_result(self):
        _, _, outbox = self.retail_request()

        class AlwaysTemporaryBackend:
            backend_name = "temporary-test"

            def send(self, notification, *, delivery_key):
                raise TemporaryDeliveryError("smtp_busy")

        queue = NotificationQueue(max_attempts=1, retry_base_seconds=1, retry_max_seconds=1)
        with self.assertLogs("integrations.notifications", level="INFO") as captured:
            self.assertEqual(NotificationWorker(AlwaysTemporaryBackend(), queue=queue).run_once(), 1)

        outbox.refresh_from_db()
        self.assertEqual(outbox.status, NotificationOutbox.Status.MANUAL_REVIEW)
        self.assertEqual(outbox.attempt_count, 1)
        delivery_log = next(
            json.loads(record.getMessage())
            for record in captured.records
            if json.loads(record.getMessage()).get("event") == "delivery_attempt"
        )
        self.assertEqual(delivery_log["result"], "manual_review")
        self.assertEqual(delivery_log["error_code"], "smtp_busy")

    def test_two_workers_claim_distinct_rows_and_smtp_callback_can_lock_claimed_row(self):
        _, _, first = self.retail_request()
        _, _, second = self.retail_request()
        first_started = threading.Event()
        release_first = threading.Event()
        callback_lock_checks = []
        callback_lock = threading.Lock()

        class ConcurrentBackend:
            backend_name = "concurrent-test"

            def __init__(self):
                self.request_numbers = []

            def send(inner_self, notification, *, delivery_key):
                close_old_connections()
                params = connections["default"].get_connection_params()
                with psycopg.connect(**params) as callback_connection:
                    row = callback_connection.execute(
                        "SELECT id FROM integrations_notificationoutbox "
                        "WHERE order_id = (SELECT id FROM orders_order WHERE submission_key = %s)",
                        (str(notification.submission_key),),
                    ).fetchone()
                    callback_connection.execute(
                        "SELECT id FROM integrations_notificationoutbox WHERE id = %s FOR UPDATE NOWAIT",
                        (row[0],),
                    ).fetchone()
                    callback_connection.commit()
                with callback_lock:
                    callback_lock_checks.append(row[0])
                    inner_self.request_numbers.append(notification.request_number)
                    first_call = len(inner_self.request_numbers) == 1
                if first_call:
                    first_started.set()
                    if not release_first.wait(5):
                        raise AssertionError("The concurrent test did not release its first delivery.")
                return DeliveryResult()

        backend = ConcurrentBackend()

        def drain_worker():
            close_old_connections()
            try:
                return NotificationWorker(backend).run_once()
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(drain_worker)
            self.assertTrue(first_started.wait(5), "First worker did not enter its SMTP callback.")
            second_future = pool.submit(drain_worker)
            self.assertEqual(second_future.result(timeout=5), 1)
            second.refresh_from_db()
            first.refresh_from_db()
            self.assertEqual(second.status, NotificationOutbox.Status.SENT)
            self.assertEqual(first.status, NotificationOutbox.Status.PROCESSING)
            release_first.set()
            self.assertEqual(first_future.result(timeout=5), 1)

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, NotificationOutbox.Status.SENT)
        self.assertEqual(second.status, NotificationOutbox.Status.SENT)
        self.assertEqual(len(set(backend.request_numbers)), 2)
        self.assertCountEqual(callback_lock_checks, [first.pk, second.pk])

    def test_public_posts_commit_outbox_notify_and_real_once_worker_delivers_both_messages(self):
        client = Client()
        client.defaults["REMOTE_ADDR"] = f"notifications-{self.marker}"
        listener = psycopg.connect(**connections["default"].get_connection_params(), autocommit=True)
        listener.execute("LISTEN notification_outbox")
        try:
            add_response = client.post(
                reverse("orders:cart"),
                {"action": "add", "product_id": str(self.product.pk), "weight": "2"},
            )
            self.assertEqual(add_response.status_code, 302)
            retail_page = client.get(reverse("orders:cart"))
            retail_submission = retail_page.context["submission"]
            retail_response = client.post(
                reverse("orders:cart"),
                {
                    "action": "submit",
                    **retail_submission,
                    "name": f"Retail POST contact {self.marker}",
                    "phone": "+7 (900) 111-22-33",
                    "email": "retail-post@example.test",
                    "pickup_method": "delivery",
                    "address": "POST delivery address",
                    "comment": "POST retail comment",
                    "consent": "yes",
                },
            )
            self.assertEqual(retail_response.status_code, 302)
            retail_order = Order.objects.get(submission_key=retail_submission["submission_key"])
            retail_outbox = NotificationOutbox.objects.get(order=retail_order)
            self.assertTrue(list(listener.notifies(timeout=2, stop_after=1)))

            self.product.name = "Catalog changed before delivery"
            self.product.price_per_kg = "88.88"
            self.product.save()
            self.product.delete()

            wholesale_page = client.get(reverse("inquiries:wholesale"))
            wholesale_submission = wholesale_page.context["submission"]
            wholesale_response = client.post(
                reverse("inquiries:wholesale"),
                {
                    **wholesale_submission,
                    "contact_name": f"Wholesale POST contact {self.marker}",
                    "company": f"Wholesale POST company {self.marker}",
                    "phone": "+7 (900) 444-55-66",
                    "email": "",
                    "consent": "yes",
                },
            )
            self.assertEqual(wholesale_response.status_code, 302)
            inquiry = Inquiry.objects.get(submission_key=wholesale_submission["submission_key"])
            wholesale_outbox = NotificationOutbox.objects.get(inquiry=inquiry)
            self.assertTrue(list(listener.notifies(timeout=2, stop_after=1)))

            output = io.StringIO()
            with _LocalSMTPServer() as server, self.smtp_settings(server):
                call_command("process_notifications", once=True, stdout=output)

            retail_outbox.refresh_from_db()
            wholesale_outbox.refresh_from_db()
            self.assertEqual(retail_outbox.status, NotificationOutbox.Status.SENT)
            self.assertEqual(wholesale_outbox.status, NotificationOutbox.Status.SENT)
            self.assertIn("Processed 2 ready notification(s).", output.getvalue())
            self.assertEqual(len(server.messages), 2)
            bodies = [
                BytesParser(policy=policy.default).parsebytes(raw).get_payload(decode=True).decode("utf-8")
                for raw in server.messages
            ]
            self.assertTrue(any("Catalog changed before delivery" not in body and f"Notification fish {self.marker}" in body for body in bodies))
            self.assertTrue(any("POST retail comment" in body for body in bodies))
            self.assertTrue(any(f"Wholesale POST company {self.marker}" in body for body in bodies))
        finally:
            listener.close()

    def test_listen_receives_only_committed_notifications_and_one_wake_drains_all_ready_rows(self):
        listener = psycopg.connect(**connections["default"].get_connection_params(), autocommit=True)
        listener.execute("LISTEN notification_outbox")
        try:
            with transaction.atomic():
                self.retail_request()
                with connections["default"].cursor() as cursor:
                    cursor.execute("SELECT pg_notify(%s, %s)", ("notification_outbox", ""))
                self.assertEqual(list(listener.notifies(timeout=0.05, stop_after=1)), [])

            first_notifications = list(listener.notifies(timeout=2, stop_after=2))
            self.assertEqual(len(first_notifications), 1)

            with transaction.atomic():
                self.retail_request()
                self.wholesale_request()
                with connections["default"].cursor() as cursor:
                    cursor.execute("SELECT pg_notify(%s, %s)", ("notification_outbox", ""))
                    cursor.execute("SELECT pg_notify(%s, %s)", ("notification_outbox", ""))
            coalesced = list(listener.notifies(timeout=1, stop_after=2))
            self.assertEqual(len(coalesced), 1, "PostgreSQL coalesces identical notifications in one transaction.")

            backend = _RecordingBackend()
            self.assertEqual(NotificationWorker(backend).run_once(), 3)
            self.assertEqual(len(backend.notifications), 3)

            with transaction.atomic():
                self.retail_request()
                with connections["default"].cursor() as cursor:
                    cursor.execute("SELECT pg_notify(%s, %s)", ("notification_outbox", ""))
                transaction.set_rollback(True)
            self.assertEqual(list(listener.notifies(timeout=0.15, stop_after=1)), [])
        finally:
            listener.close()

    def test_worker_startup_and_fallback_scan_find_rows_without_a_new_signal(self):
        backend = _RecordingBackend()
        self.retail_request()
        stop_event = threading.Event()
        backend.after_send = lambda notification, count: stop_event.set()
        worker = NotificationWorker(backend, stop_event=stop_event, scan_interval=1)
        worker.run()
        self.assertEqual(len(backend.notifications), 1)

        backend = _RecordingBackend()
        stop_event = threading.Event()
        inserted = threading.Event()
        original_listener_factory = lambda: PsycopgNotificationListener()

        class InsertWithoutNotify:
            def __init__(self):
                self.listener = original_listener_factory()
                self.first_wait = True

            def listen(self):
                self.listener.listen()

            def wait(self, timeout, stop):
                if self.first_wait:
                    self.first_wait = False
                    self_case.retail_request()
                    inserted.set()
                return self.listener.wait(timeout, stop)

            def close(self):
                self.listener.close()

        self_case = self
        backend.after_send = lambda notification, count: stop_event.set()
        worker = NotificationWorker(
            backend,
            listener_factory=InsertWithoutNotify,
            scan_interval=1,
            stop_event=stop_event,
        )
        worker.run()
        self.assertTrue(inserted.is_set())
        self.assertEqual(len(backend.notifications), 1)

    def test_worker_reconnects_listener_and_listens_before_draining_backlog(self):
        self.retail_request()
        backend = _RecordingBackend()
        stop_event = threading.Event()
        backend.after_send = lambda notification, count: stop_event.set()
        constructions = []
        listen_calls = []

        def listener_factory():
            if not constructions:
                constructions.append("failed")
                raise psycopg.OperationalError("synthetic first listener disconnect")
            constructions.append("connected")
            listener = PsycopgNotificationListener()

            class ListenerProbe:
                def listen(self):
                    listener.listen()
                    listen_calls.append("listening")

                def wait(self, timeout, stop):
                    return listener.wait(timeout, stop)

                def close(self):
                    listener.close()

            return ListenerProbe()

        worker = NotificationWorker(backend, listener_factory=listener_factory, stop_event=stop_event)
        started = time.monotonic()
        worker.run()
        self.assertGreaterEqual(time.monotonic() - started, 0.9)
        self.assertEqual(constructions, ["failed", "connected"])
        self.assertEqual(listen_calls, ["listening"])
        self.assertEqual(len(backend.notifications), 1)

    def test_scheduled_retry_wakes_before_long_scan_deadline_without_notify(self):
        _, _, outbox = self.retail_request()
        scheduled_at = timezone.now() + timedelta(milliseconds=150)
        NotificationOutbox.objects.filter(pk=outbox.pk).update(next_attempt_at=scheduled_at)
        backend = _RecordingBackend()
        stop_event = threading.Event()
        backend.after_send = lambda notification, count: stop_event.set()
        worker = NotificationWorker(backend, stop_event=stop_event, scan_interval=60)

        started = time.monotonic()
        worker.run()
        elapsed = time.monotonic() - started

        outbox.refresh_from_db()
        self.assertEqual(outbox.status, NotificationOutbox.Status.SENT)
        self.assertEqual(len(backend.notifications), 1)
        self.assertLess(elapsed, 2, "The worker should wake at next_attempt_at, not the 60-second scan.")

    def test_held_row_is_skipped_and_another_ready_row_is_claimed(self):
        _, _, locked = self.retail_request()
        _, _, available = self.retail_request()
        params = connections["default"].get_connection_params()
        locker = psycopg.connect(**params)
        try:
            locker.execute(
                "SELECT id FROM integrations_notificationoutbox WHERE id = %s FOR UPDATE",
                (locked.pk,),
            )
            claim = NotificationQueue().claim_next()
            self.assertEqual(claim.outbox_id, available.pk)
        finally:
            locker.rollback()
            locker.close()

        next_claim = NotificationQueue().claim_next()
        self.assertEqual(next_claim.outbox_id, locked.pk)

    def test_accepted_smtp_message_survives_finalization_db_error_and_recovers_by_lease(self):
        order, _, outbox = self.retail_request(
            name=f"Sensitive contact {self.marker}",
            email=f"sensitive-{self.marker}@example.test",
        )
        backend = None
        queue = NotificationQueue(lease_seconds=30)
        with _LocalSMTPServer() as server, self.smtp_settings(server), self.assertLogs(
            "integrations.notifications", level="WARNING"
        ) as captured:
            backend = SMTPNotificationBackend()
            with patch.object(
                queue,
                "mark_sent",
                side_effect=OperationalError("simulated database failure psql-password-marker"),
            ):
                with self.assertRaises(OperationalError):
                    NotificationWorker(backend, queue=queue).run_once()

            outbox.refresh_from_db()
            self.assertEqual(outbox.status, NotificationOutbox.Status.PROCESSING)
            self.assertEqual(outbox.attempt_count, 1)
            self.assertIsNotNone(outbox.lease_token)
            self.assertEqual(len(server.messages), 1)
            self.assert_no_smtp_sensitive_log_values(
                captured,
                order.name,
                order.email,
                order.phone,
                "psql-password-marker",
            )

            NotificationOutbox.objects.filter(pk=outbox.pk).update(
                lease_expires_at=timezone.now() - timedelta(seconds=1)
            )
            self.assertEqual(NotificationWorker(backend, queue=queue).run_once(), 1)

            outbox.refresh_from_db()
            self.assertEqual(outbox.status, NotificationOutbox.Status.SENT)
            self.assertEqual(outbox.attempt_count, 2)
            self.assertEqual(len(server.messages), 2)
            messages = [BytesParser(policy=policy.default).parsebytes(raw) for raw in server.messages]
            self.assertEqual(messages[0]["Message-ID"], messages[1]["Message-ID"])

            self.assert_no_smtp_sensitive_log_values(
                captured,
                order.name,
                order.email,
                order.phone,
                "psql-password-marker",
            )

    def test_terminated_listener_reconnects_relists_and_drains_backlog(self):
        backend = _RecordingBackend()
        stop_event = threading.Event()
        first_wait = threading.Event()
        second_listened = threading.Event()
        listener_pids = []
        listener_lock = threading.Lock()
        violations = []

        class ListenerProbe:
            def __init__(self, listener, generation):
                self.listener = listener
                self.generation = generation
                self.wait_announced = False

            def listen(self):
                self.listener.listen()
                with listener_lock:
                    listener_pids.append(self.listener.connection.info.backend_pid)
                if self.generation == 2:
                    second_listened.set()

            def wait(self, timeout, stop):
                if self.generation == 1 and not self.wait_announced:
                    self.wait_announced = True
                    first_wait.set()
                return self.listener.wait(timeout, stop)

            def close(self):
                self.listener.close()

        def listener_factory():
            with listener_lock:
                generation = len(listener_pids) + 1
            return ListenerProbe(PsycopgNotificationListener(), generation)

        def after_send(notification, count):
            with listener_lock:
                if len(listener_pids) < 2:
                    violations.append("delivery began before the replacement listener registered LISTEN")
            stop_event.set()

        backend.after_send = after_send
        worker = NotificationWorker(
            backend,
            listener_factory=listener_factory,
            stop_event=stop_event,
            scan_interval=30,
        )
        params = connections["default"].get_connection_params()

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(worker.run)
            try:
                self.assertTrue(first_wait.wait(5), "The first worker listener did not enter wait().")
                with listener_lock:
                    first_pid = listener_pids[0]
                with psycopg.connect(**params, autocommit=True) as killer:
                    terminated = killer.execute("SELECT pg_terminate_backend(%s)", (first_pid,)).fetchone()[0]
                self.assertTrue(terminated, "PostgreSQL did not terminate the live LISTEN connection.")
                self.retail_request()  # Deliberately do not emit NOTIFY while the listener is disconnected.
                self.assertTrue(second_listened.wait(8), "The worker did not reconnect and issue LISTEN again.")
                future.result(timeout=8)
            finally:
                stop_event.set()
                if not future.done():
                    future.result(timeout=8)

        self.assertEqual(len(listener_pids), 2)
        self.assertEqual(backend.notifications.__len__(), 1)
        self.assertEqual(violations, [])
        outbox = NotificationOutbox.objects.get(order__submission_key=backend.notifications[0][0].submission_key)
        self.assertEqual(outbox.status, NotificationOutbox.Status.SENT)

    def test_worker_subprocess_handles_sigterm_and_sigkill_lease_recovery(self):
        database = connections["default"].settings_dict
        database_name = database["NAME"]
        self.assertTrue(database_name.startswith("test_"))
        base_dir = os.path.dirname(os.path.dirname(__file__))

        def worker_environment(backend_path):
            from fishsite.test_safety import worker_environment as isolated_worker_environment
            env = isolated_worker_environment(database)
            env.update(
                {
                    "FISHSITE_TEST_NOTIFICATION_BACKEND": backend_path,
                    "FISHSITE_TEST_LEASE_SECONDS": "1",
                    "FISHSITE_TEST_SCAN_SECONDS": "1",
                }
            )
            return env

        _, _, graceful = self.retail_request()
        graceful_process = subprocess.Popen(
            [sys.executable, os.path.join(base_dir, "manage.py"), "process_notifications"],
            cwd=base_dir,
            env=worker_environment(f"{__name__}.NoopNotificationBackend"),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            self.wait_for_status(graceful.pk, NotificationOutbox.Status.SENT, graceful_process)
            graceful_process.send_signal(signal.SIGTERM)
            stdout, _ = graceful_process.communicate(timeout=5)
            self.assertEqual(graceful_process.returncode, 0, stdout)
            self.assertIn('"event":"worker_stopped"', stdout)
        finally:
            if graceful_process.poll() is None:
                graceful_process.kill()
                graceful_process.communicate(timeout=5)

        _, _, interrupted = self.retail_request()
        interrupted_process = subprocess.Popen(
            [sys.executable, os.path.join(base_dir, "manage.py"), "process_notifications"],
            cwd=base_dir,
            env=worker_environment(f"{__name__}.BlockingNotificationBackend"),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            self.wait_for_status(interrupted.pk, NotificationOutbox.Status.PROCESSING, interrupted_process)
            interrupted.refresh_from_db()
            self.assertEqual(interrupted.attempt_count, 1)
            interrupted_process.kill()
            killed_output, _ = interrupted_process.communicate(timeout=5)
            self.assertEqual(interrupted_process.returncode, -signal.SIGKILL, killed_output)
        finally:
            if interrupted_process.poll() is None:
                interrupted_process.kill()
                interrupted_process.communicate(timeout=5)

        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            interrupted.refresh_from_db()
            if interrupted.lease_expires_at and interrupted.lease_expires_at <= timezone.now():
                break
            time.sleep(0.05)
        self.assertLessEqual(interrupted.lease_expires_at, timezone.now())
        with override_settings(
            NOTIFICATION_BACKEND=f"{__name__}.NoopNotificationBackend",
            NOTIFICATION_LEASE_SECONDS=1,
        ):
            self.assertEqual(NotificationWorker(load_backend()).run_once(), 1)
        interrupted.refresh_from_db()
        self.assertEqual(interrupted.status, NotificationOutbox.Status.SENT)
        self.assertEqual(interrupted.attempt_count, 2)

    def wait_for_status(self, outbox_id, status, process, *, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            close_old_connections()
            row = NotificationOutbox.objects.get(pk=outbox_id)
            if row.status == status:
                return row
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout else ""
                self.fail(f"Worker process exited before status {status!r}: {output}")
            time.sleep(0.05)
        self.fail(f"Outbox {outbox_id} did not reach status {status!r} before timeout.")

    def test_admin_retry_requires_change_permission_and_notifies_only_eligible_rows(self):
        _, _, manual = self.retail_request()
        _, _, pending = self.retail_request()
        _, _, sent = self.retail_request()
        _, _, processing = self.retail_request()
        manual.status = NotificationOutbox.Status.MANUAL_REVIEW
        manual.attempt_count = 5
        manual.last_error = "smtp_recipient_refused"
        manual.save()
        pending.status = NotificationOutbox.Status.PENDING
        pending.save()
        sent.status = NotificationOutbox.Status.PROCESSING
        sent.attempt_count = 1
        sent.lease_token = uuid.uuid4()
        sent.lease_expires_at = timezone.now() + timedelta(minutes=5)
        sent.save()
        processing.status = NotificationOutbox.Status.SENT
        processing.attempt_count = 1
        processing.lease_token = None
        processing.lease_expires_at = None
        processing.sent_at = timezone.now()
        processing.save()

        listener = psycopg.connect(**connections["default"].get_connection_params(), autocommit=True)
        listener.execute("LISTEN notification_outbox")
        user_model = get_user_model()
        view_user = user_model.objects.create_user(
            username=f"view-{self.marker}", password="unused", is_staff=True
        )
        change_user = user_model.objects.create_user(
            username=f"change-{self.marker}", password="unused", is_staff=True
        )
        view_permission = Permission.objects.get(codename="view_notificationoutbox")
        change_permission = Permission.objects.get(codename="change_notificationoutbox")
        view_user.user_permissions.add(view_permission)
        change_user.user_permissions.add(change_permission)

        path = reverse("admin:integrations_notificationoutbox_changelist")
        view_client = Client()
        view_client.force_login(view_user)
        response = view_client.post(path, {"action": "retry_manual_review", "_selected_action": [manual.pk]})
        self.assertEqual(response.status_code, 200)
        manual.refresh_from_db()
        self.assertEqual(manual.status, NotificationOutbox.Status.MANUAL_REVIEW)
        self.assertEqual(list(listener.notifies(timeout=0.1, stop_after=1)), [])

        change_client = Client()
        change_client.force_login(change_user)
        response = change_client.post(
            path,
            {
                "action": "retry_manual_review",
                "_selected_action": [manual.pk, pending.pk, sent.pk, processing.pk],
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        manual.refresh_from_db()
        pending.refresh_from_db()
        sent.refresh_from_db()
        processing.refresh_from_db()
        self.assertEqual(manual.status, NotificationOutbox.Status.PENDING)
        self.assertEqual(manual.attempt_count, 0)
        self.assertEqual(manual.last_error, "")
        self.assertIsNone(manual.lease_token)
        self.assertEqual(pending.status, NotificationOutbox.Status.PENDING)
        self.assertEqual(sent.status, NotificationOutbox.Status.PROCESSING)
        self.assertEqual(processing.status, NotificationOutbox.Status.SENT)
        self.assertEqual(len(list(listener.notifies(timeout=1, stop_after=2))), 1)
        self.assertTrue(LogEntry.objects.filter(user=change_user, object_id=str(manual.pk)).exists())
        listener.close()

    def test_g09_smtp_preflight_success_and_failure_leave_all_request_and_outbox_rows_unchanged(self):
        order, item, retail_outbox = self.retail_request()
        inquiry, wholesale_outbox = self.wholesale_request(need="Existing wholesale need")
        retry_at = timezone.now() + timedelta(minutes=7)
        for outbox in (retail_outbox, wholesale_outbox):
            outbox.attempt_count = 2
            outbox.last_error = "previous_safe_error"
            outbox.next_attempt_at = retry_at
            outbox.save(update_fields=("attempt_count", "last_error", "next_attempt_at"))

        def current_rows():
            return {
                "order": Order.objects.filter(pk=order.pk).values().get(),
                "item": OrderItem.objects.filter(pk=item.pk).values().get(),
                "inquiry": Inquiry.objects.filter(pk=inquiry.pk).values().get(),
                "retail_outbox": NotificationOutbox.objects.filter(pk=retail_outbox.pk).values().get(),
                "wholesale_outbox": NotificationOutbox.objects.filter(pk=wholesale_outbox.pk).values().get(),
            }

        unchanged_rows = current_rows()
        profile = {
            "NOTIFICATION_BACKEND": "integrations.notifications.smtp.SMTPNotificationBackend",
            "NOTIFICATION_TO_EMAIL": "seller@example.test",
            "NOTIFICATION_LEASE_SECONDS": 300,
            "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
            "EMAIL_HOST": "smtp.mail.ru",
            "EMAIL_PORT": 465,
            "EMAIL_HOST_USER": "preflight-user@mail.ru",
            "EMAIL_HOST_PASSWORD": "preflight-password-marker",
            "EMAIL_USE_TLS": False,
            "EMAIL_USE_SSL": True,
            "SMTP_ALLOW_INSECURE_LOCAL_AUTH": False,
            "EMAIL_TIMEOUT": 10,
            "DEFAULT_FROM_EMAIL": "FishSite <preflight-user@mail.ru>",
        }

        for should_succeed in (True, False):
            with self.subTest(preflight_success=should_succeed):
                connection = MagicMock()
                if should_succeed:
                    connection.open.return_value = True
                else:
                    connection.open.side_effect = RuntimeError("synthetic-preflight-secret-marker")
                stdout = io.StringIO()
                with (
                    override_settings(**profile),
                    patch(
                        "integrations.management.commands.check_notification_smtp.get_connection",
                        return_value=connection,
                    ),
                    CaptureQueriesContext(connections["default"]) as queries,
                ):
                    if should_succeed:
                        call_command("check_notification_smtp", stdout=stdout)
                        self.assertIn("no email was sent", stdout.getvalue())
                    else:
                        with self.assertRaisesRegex(CommandError, "mailru_smtp_connection_failed") as raised:
                            call_command("check_notification_smtp", stdout=stdout)
                        self.assertNotIn("synthetic-preflight-secret-marker", str(raised.exception))
                self.assertEqual(len(queries), 0)
                connection.close.assert_called_once_with()
                self.assertEqual(current_rows(), unchanged_rows)

    def test_q02_public_form_controls_stay_in_body_and_cannot_change_mail_recipients(self):
        self.client.post(
            reverse("orders:cart"),
            {"action": "add", "product_id": str(self.product.pk), "weight": "2"},
        )
        cart = self.client.get(reverse("orders:cart"))
        submission = cart.context["submission"]
        injected_name = "Buyer Name\r\nBcc: buyer-copy@attacker.example"
        injected_comment = "Please call after noon\r\nTo: hidden-copy@attacker.example"
        response = self.client.post(
            reverse("orders:cart"),
            {
                "action": "submit",
                **submission,
                "name": injected_name,
                "phone": "+7 (900) 111-22-33",
                "email": "buyer@example.test",
                "pickup_method": "delivery",
                "address": "Safe delivery address",
                "comment": injected_comment,
                "consent": "yes",
                "from": "Attacker <attacker@attacker.example>",
                "from_email": "attacker@attacker.example",
                "to": "attacker@attacker.example",
                "recipient": "attacker@attacker.example",
                "bcc": "buyer-copy@attacker.example",
                "smtp_host": "smtp.attacker.example",
            },
        )
        self.assertEqual(response.status_code, 302)
        order = Order.objects.get(submission_key=submission["submission_key"])
        outbox = NotificationOutbox.objects.get(order=order)

        with _LocalSMTPServer() as server, self.smtp_settings(server):
            self.assertEqual(NotificationWorker(SMTPNotificationBackend()).run_once(), 1)

        outbox.refresh_from_db()
        self.assertEqual(outbox.status, NotificationOutbox.Status.SENT)
        self.assertEqual(len(server.messages), 1)
        raw = server.messages[0]
        header_blob = raw.split(b"\r\n\r\n", 1)[0].decode("utf-8", errors="replace")
        message = BytesParser(policy=policy.default).parsebytes(raw)
        body = message.get_payload(decode=True).decode(message.get_content_charset() or "utf-8")

        self.assertEqual(len(message.get_all("From", [])), 1)
        self.assertEqual(len(message.get_all("To", [])), 1)
        self.assertEqual(message["From"].addresses[0].addr_spec, "notifications@example.test")
        self.assertEqual(message["To"].addresses[0].addr_spec, "seller@example.test")
        for header in ("Bcc", "Cc", "Reply-To"):
            self.assertIsNone(message.get(header))
        self.assertRegex(str(message["Message-ID"]), r"^<[0-9a-f]{64}@example\.test>$")
        self.assertIn(order.order_number, str(message["Subject"]))
        self.assertNotIn("attacker@attacker.example", header_blob)
        self.assertNotIn("buyer-copy@attacker.example", header_blob)
        self.assertIn("Bcc: buyer-copy@attacker.example", body)
        self.assertIn("To: hidden-copy@attacker.example", body)
        mail_commands = [command for command in server.commands if command.upper().startswith("MAIL FROM:")]
        recipient_commands = [command for command in server.commands if command.upper().startswith("RCPT TO:")]
        self.assertEqual(len(mail_commands), 1)
        self.assertEqual(len(recipient_commands), 1)
        self.assertTrue(mail_commands[0].upper().startswith("MAIL FROM:<NOTIFICATIONS@EXAMPLE.TEST>"))
        self.assertTrue(recipient_commands[0].upper().startswith("RCPT TO:<SELLER@EXAMPLE.TEST>"))
        self.assertNotIn("attacker@attacker.example", mail_commands[0] + recipient_commands[0])

    def test_q04_auth_and_certificate_failures_preserve_retail_and_wholesale_requests(self):
        for scenario in ("smtp_authentication", "smtp_certificate"):
            with self.subTest(scenario=scenario):
                marker = f"{self.marker}-{scenario}"
                buyer_name = f"Private buyer {marker}"
                buyer_email = f"private-{marker}@example.test"
                server_error = f"raw SMTP diagnostic {marker}"
                order, item, retail_outbox = self.retail_request(
                    name=buyer_name,
                    email=buyer_email,
                    delivery_address=f"Private address {marker}",
                    comment=f"Private comment {marker}",
                )
                inquiry, wholesale_outbox = self.wholesale_request(
                    contact_name=f"Private wholesale contact {marker}",
                    company=f"Private company {marker}",
                    email=f"wholesale-{marker}@example.test",
                    need=f"Private need {marker}",
                )

                before = {
                    "order": Order.objects.filter(pk=order.pk).values().get(),
                    "item": OrderItem.objects.filter(pk=item.pk).values().get(),
                    "inquiry": Inquiry.objects.filter(pk=inquiry.pk).values().get(),
                    "retail_outbox": NotificationOutbox.objects.filter(pk=retail_outbox.pk).values().get(),
                    "wholesale_outbox": NotificationOutbox.objects.filter(pk=wholesale_outbox.pk).values().get(),
                }
                with self.assertLogs("integrations.notifications", level="INFO") as captured:
                    if scenario == "smtp_authentication":
                        with _LocalSMTPServer(
                            auth_reply=f"535 5.7.8 {server_error}\r\n".encode()
                        ) as server, self.smtp_settings(
                            server,
                            EMAIL_HOST_USER=f"user-{marker}",
                            EMAIL_HOST_PASSWORD=f"password-{marker}",
                            SMTP_ALLOW_INSECURE_LOCAL_AUTH=True,
                        ):
                            self.assertEqual(NotificationWorker(SMTPNotificationBackend()).run_once(), 2)
                        self.assertEqual(server.messages, [])
                        self.assertFalse(any(command.upper().startswith("DATA") for command in server.commands))
                        expected_error = "smtp_response_permanent"
                    else:
                        smtp_profile = {
                            "NOTIFICATION_BACKEND": "integrations.notifications.smtp.SMTPNotificationBackend",
                            "NOTIFICATION_TO_EMAIL": "seller@example.test",
                            "NOTIFICATION_LEASE_SECONDS": 300,
                            "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
                            "EMAIL_HOST": "smtp.mail.ru",
                            "EMAIL_PORT": 465,
                            "EMAIL_HOST_USER": f"user-{marker}@mail.ru",
                            "EMAIL_HOST_PASSWORD": f"password-{marker}",
                            "EMAIL_USE_TLS": False,
                            "EMAIL_USE_SSL": True,
                            "SMTP_ALLOW_INSECURE_LOCAL_AUTH": False,
                            "EMAIL_TIMEOUT": 10,
                            "DEFAULT_FROM_EMAIL": f"FishSite <user-{marker}@mail.ru>",
                        }
                        with override_settings(**smtp_profile), patch(
                            "django.core.mail.backends.smtp.smtplib.SMTP_SSL",
                            side_effect=ssl.SSLCertVerificationError(
                                1, f"certificate verification failed {server_error}"
                            ),
                        ):
                            self.assertEqual(NotificationWorker(SMTPNotificationBackend()).run_once(), 2)
                        expected_error = "smtp_tls_certificate_invalid"

                self.assert_no_smtp_sensitive_log_values(
                    captured,
                    buyer_name,
                    buyer_email,
                    "+7 (900) 111-22-33",
                    f"user-{marker}",
                    f"password-{marker}",
                    server_error,
                )
                self.assertEqual(
                    {
                        "order": Order.objects.filter(pk=order.pk).values().get(),
                        "item": OrderItem.objects.filter(pk=item.pk).values().get(),
                        "inquiry": Inquiry.objects.filter(pk=inquiry.pk).values().get(),
                    },
                    {key: before[key] for key in ("order", "item", "inquiry")},
                )
                for outbox, key in (
                    (retail_outbox, "retail_outbox"),
                    (wholesale_outbox, "wholesale_outbox"),
                ):
                    current = NotificationOutbox.objects.filter(pk=outbox.pk).values().get()
                    self.assertEqual(current["order_id"], before[key]["order_id"])
                    self.assertEqual(current["inquiry_id"], before[key]["inquiry_id"])
                    self.assertEqual(current["created_at"], before[key]["created_at"])
                    self.assertEqual(current["status"], NotificationOutbox.Status.MANUAL_REVIEW)
                    self.assertEqual(current["attempt_count"], before[key]["attempt_count"] + 1)
                    self.assertEqual(current["last_error"], expected_error)
                    self.assertIsNone(current["lease_token"])
                    self.assertIsNone(current["lease_expires_at"])
                    self.assertIsNone(current["sent_at"])
                    self.assertNotIn(server_error, repr(current))


class NoopNotificationBackend:
    def send(self, notification, *, delivery_key):
        return DeliveryResult()


class BlockingNotificationBackend:
    def send(self, notification, *, delivery_key):
        time.sleep(60)
        return DeliveryResult()
