"""Pure acceptance tests for webhook log redaction and deferred product identity."""

import logging
import socket
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from integrations.moysklad.config import get_config
from integrations.moysklad.logging import WebhookSecretFilter
from integrations.moysklad.mapping import parse_product
from tests.moysklad_helpers import (
    UNIT_KG_ID,
    moysklad_config,
    product_payload,
)


class WebhookSecretFilterRequestTests(SimpleTestCase):
    def make_record(self, request, *, msg="path", args=()):
        record = logging.LogRecord(
            "django.server", logging.ERROR, __file__, 1, msg, args, None
        )
        record.request = request
        return record

    def test_socket_request_redacts_log_values_without_mutating_socket(self):
        from integrations.moysklad.config import get_config

        secret = get_config(moysklad_config()).webhook_secret
        url = f"https://fish.example.test/integrations/moysklad/webhooks/prices/{secret}/"
        request = socket.socket()
        socket_fd = request.fileno()
        try:
            record = self.make_record(request, msg=f"failed {url}", args=(url,))

            self.assertTrue(WebhookSecretFilter().filter(record))

            self.assertIn("[redacted]", record.msg)
            self.assertIn("[redacted]", record.args[0])
            self.assertNotIn(secret, record.msg)
            self.assertNotIn(secret, record.args[0])
            self.assertIs(record.request, request)
            self.assertEqual(request.fileno(), socket_fd)
        finally:
            request.close()

    def test_generic_partial_request_objects_do_not_crash(self):
        secret = get_config(moysklad_config()).webhook_secret
        path = f"/integrations/moysklad/webhooks/prices/{secret}/"
        for request in (object(), SimpleNamespace(path=path), SimpleNamespace(META={"PATH_INFO": path})):
            with self.subTest(request=request):
                record = self.make_record(request)
                self.assertTrue(WebhookSecretFilter().filter(record))
                if hasattr(request, "path"):
                    self.assertIn("[redacted]", request.path)
                if hasattr(request, "META"):
                    self.assertIn("[redacted]", request.META["PATH_INFO"])

    def test_http_request_paths_metadata_and_traceback_still_redact(self):
        secret = get_config(moysklad_config()).webhook_secret
        request = RequestFactory().get(
            f"/integrations/moysklad/webhooks/prices/{secret}/"
        )
        try:
            raise RuntimeError(
                f"failed at /integrations/moysklad/webhooks/prices/{secret}/"
            )
        except RuntimeError:
            exc_info = __import__("sys").exc_info()
        record = logging.LogRecord(
            "django.server", logging.ERROR, __file__, 1,
            "request %s", (request.path,), exc_info,
        )
        record.request = request

        self.assertTrue(WebhookSecretFilter().filter(record))

        self.assertIn("[redacted]", request.path)
        self.assertIn("[redacted]", request.path_info)
        for key in ("RAW_URI", "REQUEST_URI", "PATH_INFO"):
            if key in request.META:
                self.assertIn("[redacted]", request.META[key])
        self.assertIn("[redacted]", record.args[0])
        self.assertIn("[redacted]", record.exc_text)
        self.assertIsNone(record.exc_info)


class DeferredProductIdentityTests(SimpleTestCase):
    DEFERRED_ID = uuid.UUID("20000000-0000-4000-8000-000000000077")
    OTHER_ID = uuid.UUID("20000000-0000-4000-8000-000000000078")

    def config(self, deferred_ids):
        return get_config(moysklad_config() | {"deferred_product_ids": deferred_ids})

    def test_deferred_ids_parse_csv_list_tuple_and_uuid_values(self):
        expected = (self.DEFERRED_ID, self.OTHER_ID)
        self.assertEqual(
            self.config(f"{self.DEFERRED_ID}, {self.OTHER_ID}").deferred_product_ids,
            expected,
        )
        self.assertEqual(
            self.config([str(self.DEFERRED_ID), self.OTHER_ID]).deferred_product_ids,
            expected,
        )
        self.assertEqual(
            self.config((self.DEFERRED_ID, self.OTHER_ID)).deferred_product_ids,
            expected,
        )

    def test_setting_supplies_csv_and_defaults_to_empty_tuple(self):
        with override_settings(MOYSKLAD_DEFERRED_PRODUCT_IDS=str(self.DEFERRED_ID)):
            self.assertEqual(
                get_config(moysklad_config()).deferred_product_ids,
                (self.DEFERRED_ID,),
            )
        with override_settings(MOYSKLAD_DEFERRED_PRODUCT_IDS=""):
            self.assertEqual(get_config(moysklad_config()).deferred_product_ids, ())

    def test_invalid_deferred_uuid_is_rejected_without_echoing_value(self):
        raw = "not-a-real-product-id"
        with self.assertRaises(ImproperlyConfigured) as caught:
            self.config(raw)
        self.assertNotIn(raw, str(caught.exception))

    def test_configured_uuid_is_deferred_even_when_code_changes_and_unit_is_kg(self):
        config = self.config([self.DEFERRED_ID])
        payload = product_payload(self.DEFERRED_ID, code="NEW-CODE", unit_id=UNIT_KG_ID)

        parsed = parse_product(payload, config)

        self.assertTrue(parsed["is_deferred"])

    def test_legacy_code_does_not_defer_an_unconfigured_uuid(self):
        config = self.config([self.DEFERRED_ID])
        payload = product_payload(self.OTHER_ID, code="00077", unit_id=UNIT_KG_ID)

        parsed = parse_product(payload, config)

        self.assertFalse(parsed["is_deferred"])
        self.assertEqual(parsed["last_error_code"], "")

    def test_linked_product_uuid_is_not_deferred_after_code_changes_to_legacy_code(self):
        config = self.config([self.DEFERRED_ID])
        payload = product_payload(self.OTHER_ID, code="00126", unit_id=UNIT_KG_ID)

        parsed = parse_product(payload, config, expected_id=self.OTHER_ID)

        self.assertFalse(parsed["is_deferred"])
        self.assertEqual(parsed["last_error_code"], "")


class DeferredProductPersistenceTests(TestCase):
    DEFERRED_ID = uuid.UUID("30000000-0000-4000-8000-000000000077")
    HEALTHY_ID = uuid.UUID("30000000-0000-4000-8000-000000000078")
    OTHER_DEFERRED_ID = uuid.UUID("30000000-0000-4000-8000-000000000079")

    def setUp(self):
        from django.contrib.auth import get_user_model
        from catalog.models import Category

        self.category = Category.objects.create(
            name="Тестовая рыба", slug=f"deferred-{uuid.uuid4().hex}"
        )
        self.owner = get_user_model().objects.create_superuser(
            username=f"deferred-{uuid.uuid4().hex}",
            email=f"{uuid.uuid4().hex}@example.test",
            password="test-password",
        )

    def make_site_product(self):
        from catalog.models import Product

        return Product.objects.create(
            name="Карточка сайта", slug=f"deferred-product-{uuid.uuid4().hex}",
            category=self.category, price_per_kg="87.00", is_published=True,
        )

    def test_catalog_upsert_uses_uuid_for_deferred_registry_and_linking(self):
        from django.core.exceptions import ValidationError
        from integrations.moysklad.catalog_sync import link_product, upsert_product
        from integrations.moysklad.config import get_config

        with override_settings(
            MOYSKLAD=moysklad_config(),
            MOYSKLAD_DEFERRED_PRODUCT_IDS=str(self.DEFERRED_ID),
        ):
            config = get_config()
            deferred = upsert_product(
                product_payload(self.DEFERRED_ID, code="00077", updated="2026-10-15 10:00:00")
            )
            self.assertTrue(deferred.is_deferred)

            deferred = upsert_product(
                product_payload(self.DEFERRED_ID, code="FISH-RENAMED", updated="2026-10-15 10:01:00"),
                config=config,
            )
            self.assertTrue(deferred.is_deferred)
            with self.assertRaises(ValidationError):
                link_product(
                    product_id=self.make_site_product().pk,
                    external_product_id=self.DEFERRED_ID,
                    actor=self.owner,
                )
            healthy = upsert_product(
                product_payload(self.HEALTHY_ID, code="00077", updated="2026-10-15 10:02:00"),
                config=config,
            )
            self.assertFalse(healthy.is_deferred)
            healthy_product = self.make_site_product()
            linked = link_product(
                product_id=healthy_product.pk,
                external_product_id=self.HEALTHY_ID,
                actor=self.owner,
            )
            self.assertEqual(linked.product_id, healthy_product.pk)

    def test_successful_price_read_clears_sticky_deferred_flag_and_applies_price(self):
        from unittest.mock import Mock
        from catalog.models import Product
        from integrations.moysklad.catalog_sync import link_product, upsert_product
        from integrations.moysklad.price_sync import process_price_task, request_price
        from integrations.moysklad.queue import claim_task
        from integrations.models import MoySkladProduct

        with override_settings(
            MOYSKLAD=moysklad_config(),
            MOYSKLAD_DEFERRED_PRODUCT_IDS=str(self.OTHER_DEFERRED_ID),
        ):
            source = upsert_product(
                product_payload(self.HEALTHY_ID, code="FISH-1", price="125.50")
            )
            product = self.make_site_product()
            link_product(product_id=product.pk, external_product_id=source.external_id, actor=self.owner)

            MoySkladProduct.objects.filter(pk=source.pk).update(is_deferred=True)
            request_price(external_product_id=source.external_id, force_apply=True)
            task = claim_task(kinds=("price",))
            result = process_price_task(
                task,
                client=Mock(get=Mock(return_value=product_payload(
                    self.HEALTHY_ID,
                    code="00126",
                    price="145.00",
                    updated="2026-10-15 10:01:00",
                ))),
            )

            source.refresh_from_db()
            product.refresh_from_db()
            self.assertEqual(result, "updated")
            self.assertFalse(source.is_deferred)
            self.assertEqual(product.price_per_kg, Decimal("145.00"))


class IncidentNotificationLinkTests(TestCase):
    API_TOKEN = "synthetic-test-token"
    ADMIN_TOKEN = "synthetic-admin-token"

    def render_incident_email(self, *, webhook_url):
        from integrations.models import NotificationOutbox
        from integrations.moysklad.incidents import open_incident
        from integrations.notifications.payloads import build_notification
        from integrations.notifications.smtp import SMTPNotificationBackend

        config = moysklad_config() | {
            "webhook_url": webhook_url,
            "admin_token": self.ADMIN_TOKEN,
        }
        if webhook_url:
            config["webhook_secret"] = webhook_url.rstrip("/").rsplit("/", 1)[-1]
        if not webhook_url:
            # A missing receiver URL is valid when automatic price updates are
            # disabled; diagnostics should still be delivered through SMTP.
            config["prices_enabled"] = False
        mail_settings = {
            "MOYSKLAD": config,
            "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
            "EMAIL_HOST": "127.0.0.1",
            "EMAIL_PORT": 2525,
            "EMAIL_TIMEOUT": 10,
            "EMAIL_USE_TLS": False,
            "EMAIL_USE_SSL": False,
            "EMAIL_HOST_USER": "",
            "EMAIL_HOST_PASSWORD": "",
            "SMTP_ALLOW_INSECURE_LOCAL_AUTH": False,
            "NOTIFICATION_LEASE_SECONDS": 300,
            "DEFAULT_FROM_EMAIL": "notifications@example.test",
            "NOTIFICATION_TO_EMAIL": "seller@example.test",
        }
        with override_settings(**mail_settings):
            incident = open_incident(
                scope="catalog", code="temporary_api_error", object_ids=["PRODUCT-1"],
            )
            outbox = NotificationOutbox.objects.get(incident=incident)
            notification = build_notification(outbox.pk)
            backend = SMTPNotificationBackend()
            with patch("integrations.notifications.smtp.EmailMessage") as email_class:
                email_class.return_value.send.return_value = 1
                result = backend.send(notification, delivery_key=notification.delivery_key)
                email_body = email_class.call_args.kwargs["body"]
                email_class.return_value.send.assert_called_once_with(fail_silently=False)
        return notification, email_body, result

    def test_configured_receiver_origin_builds_absolute_secret_free_incident_link(self):
        secret = "d" * 64
        webhook_url = (
            f"https://fish.example.test:8443/integrations/moysklad/webhooks/prices/{secret}/"
        )

        notification, email_body, result = self.render_incident_email(webhook_url=webhook_url)

        expected = "https://fish.example.test:8443/admin/moysklad/"
        self.assertEqual(notification.diagnostics_url, expected)
        self.assertIn(expected, email_body)
        self.assertNotIn(secret, email_body)
        self.assertNotIn(secret, notification.diagnostics_url)
        self.assertNotIn("/integrations/moysklad/webhooks/prices/", email_body)
        self.assertNotIn(self.API_TOKEN, email_body)
        self.assertNotIn(self.ADMIN_TOKEN, email_body)
        self.assertTrue(result.accepted)

    def test_missing_receiver_url_keeps_local_admin_path_and_email_delivery(self):
        from django.urls import reverse

        notification, email_body, result = self.render_incident_email(webhook_url="")

        local_path = reverse("admin:moysklad_status")
        self.assertEqual(notification.diagnostics_url, local_path)
        self.assertIn(local_path, email_body)
        self.assertNotIn("https://", notification.diagnostics_url)
        self.assertNotIn("/integrations/moysklad/webhooks/prices/", email_body)
        self.assertTrue(result.accepted)
