"""Check the selected Mail.ru SMTP profile without sending a message."""

from django.conf import settings
from django.core.mail import get_connection
from django.core.management.base import BaseCommand, CommandError

from integrations.notifications.smtp import SMTPNotificationBackend, _single_email


SMTP_BACKEND_PATH = "integrations.notifications.smtp.SMTPNotificationBackend"


class Command(BaseCommand):
    help = "Check Mail.ru SMTP connection, TLS, and authentication without sending email."
    requires_system_checks = []
    requires_migrations_checks = False

    def handle(self, *args, **options):
        if settings.NOTIFICATION_BACKEND != SMTP_BACKEND_PATH:
            self._raise_safe_error("mailru_smtp_backend_not_selected")

        try:
            SMTPNotificationBackend()
        except Exception:
            self._raise_safe_error("mailru_smtp_configuration_invalid")

        if not self._is_supported_mailru_profile():
            self._raise_safe_error("mailru_smtp_profile_invalid")

        connection = None
        connection_opened = False
        operation_error = False
        cleanup_error = False
        try:
            connection = get_connection(fail_silently=False)
            connection_opened = connection.open() is True
        except Exception:
            operation_error = True
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    cleanup_error = True

        if operation_error:
            self._raise_safe_error("mailru_smtp_connection_failed")
        if cleanup_error:
            self._raise_safe_error("mailru_smtp_cleanup_failed")
        if not connection_opened:
            self._raise_safe_error("mailru_smtp_connection_not_established")

        self.stdout.write(
            self.style.SUCCESS(
                "Mail.ru SMTP connection, TLS, and authentication succeeded; no email was sent."
            )
        )

    @staticmethod
    def _is_supported_mailru_profile():
        if settings.EMAIL_HOST != "smtp.mail.ru":
            return False
        if (settings.EMAIL_PORT, settings.EMAIL_USE_TLS, settings.EMAIL_USE_SSL) != (465, False, True):
            return False
        if not settings.EMAIL_HOST_USER or not settings.EMAIL_HOST_PASSWORD:
            return False
        if settings.SMTP_ALLOW_INSECURE_LOCAL_AUTH is not False:
            return False

        try:
            username = _single_email(settings.EMAIL_HOST_USER)
            sender = _single_email(settings.DEFAULT_FROM_EMAIL, allow_display_name=True)
        except Exception:
            return False

        return username.casefold() == sender.casefold()

    @staticmethod
    def _raise_safe_error(code):
        raise CommandError(f"Mail.ru SMTP preflight failed ({code}).") from None
