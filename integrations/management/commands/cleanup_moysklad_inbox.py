"""Remove only finished inbox identities older than thirty days."""

from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from integrations.moysklad.contracts import MoySkladError
from integrations.moysklad.webhooks import cleanup_processed_inbox


class Command(BaseCommand):
    help = "Delete processed MoySklad inbox records older than 30 days."

    def handle(self, *args, **options):
        try:
            count = cleanup_processed_inbox()
        except ImproperlyConfigured:
            raise CommandError("invalid_moysklad_configuration") from None
        except DatabaseError:
            raise CommandError("moysklad_database_unavailable") from None
        except MoySkladError as error:
            raise CommandError(error.code) from None
        self.stdout.write(f"inbox_removed count={count}")
