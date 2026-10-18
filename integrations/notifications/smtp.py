"""Django SMTP adapter for seller request notifications."""

from email.utils import parseaddr
import hashlib
import ipaddress
import math
import smtplib
import socket
import ssl

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.mail import EmailMessage
from django.core.validators import validate_email
from django.template.loader import render_to_string

from .contracts import (
    DeliveryResult,
    NotificationConfigurationError,
    PermanentDeliveryError,
    TemporaryDeliveryError,
)


def _single_email(value, *, allow_display_name=False):
    if not isinstance(value, str) or not value or "\r" in value or "\n" in value:
        raise NotificationConfigurationError("email_address_invalid")
    display_name, address = parseaddr(value)
    if (
        not address
        or value.count("@") != 1
        or (not allow_display_name and (display_name or address != value))
        or "," in address
    ):
        raise NotificationConfigurationError("email_address_invalid")
    try:
        validate_email(address)
    except ValidationError as exc:
        raise NotificationConfigurationError("email_address_invalid") from exc
    return address


class SMTPNotificationBackend:
    """Send one plain-text message to the single server-configured destination."""

    backend_name = "smtp"

    def __init__(self):
        self.validate_configuration()

    def validate_configuration(self):
        if settings.EMAIL_BACKEND != "django.core.mail.backends.smtp.EmailBackend":
            raise NotificationConfigurationError("smtp_backend_invalid")
        if not isinstance(settings.EMAIL_HOST, str) or not settings.EMAIL_HOST.strip() or any(
            character in settings.EMAIL_HOST for character in "\r\n \t"
        ):
            raise NotificationConfigurationError("smtp_host_missing")
        try:
            port = int(settings.EMAIL_PORT)
            timeout = float(settings.EMAIL_TIMEOUT)
        except (TypeError, ValueError, OverflowError) as exc:
            raise NotificationConfigurationError("smtp_options_invalid") from exc
        if not 1 <= port <= 65535 or not math.isfinite(timeout) or timeout <= 0:
            raise NotificationConfigurationError("smtp_options_invalid")
        if settings.NOTIFICATION_LEASE_SECONDS < timeout * 10:
            raise NotificationConfigurationError("smtp_lease_too_short")
        if settings.EMAIL_USE_TLS and settings.EMAIL_USE_SSL:
            raise NotificationConfigurationError("smtp_tls_conflict")
        username = settings.EMAIL_HOST_USER
        password = settings.EMAIL_HOST_PASSWORD
        if bool(username) != bool(password):
            raise NotificationConfigurationError("smtp_auth_options_incomplete")
        if username and not (settings.EMAIL_USE_TLS or settings.EMAIL_USE_SSL):
            allow_local_auth = settings.SMTP_ALLOW_INSECURE_LOCAL_AUTH
            is_loopback = False
            if allow_local_auth and "%" not in settings.EMAIL_HOST:
                try:
                    is_loopback = ipaddress.ip_address(settings.EMAIL_HOST).is_loopback
                except ValueError:
                    pass
            if not (allow_local_auth and is_loopback):
                raise NotificationConfigurationError("smtp_auth_requires_encryption")

        sender = _single_email(settings.DEFAULT_FROM_EMAIL, allow_display_name=True)
        recipient = _single_email(settings.NOTIFICATION_TO_EMAIL)
        self.from_email = settings.DEFAULT_FROM_EMAIL
        self.sender_domain = sender.rsplit("@", 1)[1].lower()
        self.recipient = recipient
        self.configuration_validated = True

    def send(self, notification, *, delivery_key):
        context = {"notification": notification}
        if notification.kind == "retail":
            subject = f"Розничная заявка {notification.request_number}"
            template_name = "notifications/retail.txt"
            context["fulfillment_label"] = {
                "delivery": "Доставка",
                "pickup": "Самовывоз",
            }.get(notification.fulfillment_method, "Неизвестный способ получения")
        elif notification.kind == "wholesale":
            subject = f"Оптовый запрос {notification.request_number}"
            template_name = "notifications/wholesale.txt"
        elif notification.kind == "moysklad_incident":
            subject = f"МойСклад: требуется проверка ({notification.code})"
            template_name = "notifications/moysklad_incident.txt"
        else:
            raise PermanentDeliveryError("notification_kind_unknown")

        body = render_to_string(template_name, context)
        message_id = hashlib.sha256(delivery_key.encode("utf-8")).hexdigest()
        message = EmailMessage(
            subject=subject,
            body=body,
            from_email=self.from_email,
            to=[self.recipient],
            headers={"Message-ID": f"<{message_id}@{self.sender_domain}>"},
        )
        try:
            accepted_count = message.send(fail_silently=False)
        except smtplib.SMTPRecipientsRefused as exc:
            codes = [int(result[0]) for result in exc.recipients.values() if result and str(result[0]).isdigit()]
            if codes and all(400 <= code < 500 for code in codes):
                raise TemporaryDeliveryError("smtp_recipient_temporary") from exc
            raise PermanentDeliveryError("smtp_recipient_refused") from exc
        except smtplib.SMTPResponseException as exc:
            code = int(exc.smtp_code)
            if 400 <= code < 500:
                raise TemporaryDeliveryError("smtp_response_temporary") from exc
            raise PermanentDeliveryError("smtp_response_permanent") from exc
        except (smtplib.SMTPServerDisconnected, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            if isinstance(exc, ssl.SSLCertVerificationError):
                raise PermanentDeliveryError("smtp_tls_certificate_invalid") from exc
            raise TemporaryDeliveryError("smtp_connection_unavailable") from exc
        except (smtplib.SMTPException, ImproperlyConfigured, ValidationError, ValueError) as exc:
            raise PermanentDeliveryError("smtp_configuration_or_message_invalid") from exc

        if accepted_count != 1:
            raise PermanentDeliveryError("smtp_message_not_accepted")
        return DeliveryResult(accepted=True)
