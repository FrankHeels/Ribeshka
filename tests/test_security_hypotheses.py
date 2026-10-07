# TODO: Перенести эти тесты в изолированное тестовое окружение
# с отдельной PostgreSQL до выхода проекта в production.
"""Local reproductions, not evidence of a deployment vulnerability."""

import ipaddress
import uuid
from email import policy
from email.parser import BytesParser

from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from inquiries.models import Inquiry
from integrations.models import NotificationOutbox
from integrations.notifications.payloads import RequestNotification
from integrations.notifications.smtp import SMTPNotificationBackend
from tests.test_notifications import _LocalSMTPServer


@override_settings(PUBLIC_FORM_RATE_LIMIT=2, PUBLIC_FORM_RATE_WINDOW_SECONDS=3600)
class QuotaHypothesisTests(TestCase):
    @staticmethod
    def unique_address():
        # Documentation IPv6 prefix with a random 96-bit suffix; no network traffic.
        return str(ipaddress.IPv6Address((0x20010DB8 << 96) | (uuid.uuid4().int >> 32)))

    def session_form(self, address):
        client = Client(enforce_csrf_checks=True, REMOTE_ADDR=address)
        response = client.get(reverse("inquiries:wholesale"))
        self.assertEqual(response.status_code, 200)
        data = {
            **response.context["submission"],
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
            "contact_name": f"Hypothesis {uuid.uuid4().hex}",
            "phone": "+70000000123",
            "email": "hypothesis@example.test",
            "consent": "yes",
        }
        return client, data

    def assert_absent(self, data):
        key = data["submission_key"]
        self.assertFalse(Inquiry.objects.filter(submission_key=key).exists())
        self.assertFalse(NotificationOutbox.objects.filter(inquiry__submission_key=key).exists())

    def exercise_quota(self, *, same_address):
        address = self.unique_address()
        first, first_data = self.session_form(address)
        second, second_data = self.session_form(address if same_address else self.unique_address())
        self.assertNotEqual(first.session.session_key, second.session.session_key)
        self.assertNotEqual(first_data["submission_key"], second_data["submission_key"])
        url = reverse("inquiries:wholesale")
        for _ in range(2):
            response = first.post(url, {**first_data, "phone": ""})
            self.assertEqual(response.status_code, 400)
            self.assertIn("phone", response.context["field_errors"])
        self.assert_absent(first_data)
        response = second.post(url, second_data)
        self.assertEqual(response.status_code, 429 if same_address else 302)
        if same_address:
            self.assert_absent(second_data)
        else:
            inquiry = Inquiry.objects.get(submission_key=second_data["submission_key"])
            self.assertEqual(inquiry.contact_name, second_data["contact_name"])
            self.assertEqual(NotificationOutbox.objects.filter(inquiry=inquiry).count(), 1)

    def test_same_remote_address_shares_quota_across_sessions(self):
        self.exercise_quota(same_address=True)

    def test_distinct_remote_addresses_have_independent_quota(self):
        self.exercise_quota(same_address=False)


class SMTPHypothesisTests(SimpleTestCase):
    # SimpleTestCase forbids database queries, including accidental DTO loading.
    def test_without_credentials_or_tls_sends_plaintext_data(self):
        marker = f"hypothesis-{uuid.uuid4().hex}"
        notification = RequestNotification(
            kind="wholesale",
            request_number=marker,
            submission_key=uuid.uuid4(),
            created_at=timezone.now(),
            contact_name=marker,
            phone="+70000000123",
            email="hypothesis@example.test",
        )
        with _LocalSMTPServer() as server, override_settings(
            EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
            EMAIL_HOST="127.0.0.1",
            EMAIL_PORT=server.server_address[1],
            EMAIL_HOST_USER="",
            EMAIL_HOST_PASSWORD="",
            EMAIL_USE_TLS=False,
            EMAIL_USE_SSL=False,
            SMTP_ALLOW_INSECURE_LOCAL_AUTH=False,
            EMAIL_TIMEOUT=2,
            NOTIFICATION_LEASE_SECONDS=30,
            DEFAULT_FROM_EMAIL="hypothesis-sender@example.test",
            NOTIFICATION_TO_EMAIL="hypothesis-recipient@example.test",
        ):
            result = SMTPNotificationBackend().send(notification, delivery_key=notification.delivery_key)
        self.assertTrue(result.accepted)
        self.assertEqual(len(server.messages), 1)
        verbs = [command.split(" ", 1)[0].upper() for command in server.commands]
        self.assertEqual(verbs.count("DATA"), 1)
        self.assertNotIn("STARTTLS", verbs)
        self.assertNotIn("AUTH", verbs)
        message = BytesParser(policy=policy.default).parsebytes(server.messages[0])
        self.assertIn(marker, message.get_content())
