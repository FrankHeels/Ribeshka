"""Explicit administrative operation for this environment's price webhook."""

from dataclasses import replace

from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, transaction

from integrations.moysklad.config import get_config
from integrations.moysklad.contracts import MoySkladError
from integrations.moysklad.controls import locked_state
from integrations.moysklad.webhooks import configure_webhook


class Command(BaseCommand):
    help = "Register this environment's price webhook with an administrative credential."

    def handle(self, *args, **options):
        from integrations.moysklad.client import MoySkladClient

        try:
            config = get_config()
            if not config.account_id or not config.namespace:
                raise MoySkladError("invalid_configuration")
            if not config.webhook_url or len(config.webhook_url) > 255:
                raise MoySkladError("invalid_webhook_url")
            with transaction.atomic():
                locked_state(config)
            with MoySkladClient(
                replace(config, api_token=config.admin_token or config.api_token), webhook_management=True,
            ) as client:
                identifier = configure_webhook(client=client)
        except ImproperlyConfigured:
            raise CommandError("invalid_moysklad_configuration") from None
        except DatabaseError:
            raise CommandError("moysklad_database_unavailable") from None
        except MoySkladError as error:
            raise CommandError(error.code) from None
        self.stdout.write(f"webhook_configured id={identifier}")
