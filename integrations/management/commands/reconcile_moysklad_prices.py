"""Queue the same mapped-product pass used by automatic recovery."""

from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from integrations.moysklad.contracts import MoySkladError
from integrations.moysklad.controls import command_actor
from integrations.moysklad.price_sync import start_price_reconciliation


class Command(BaseCommand):
    help = "Queue a price reconciliation for mapped products only."

    def add_arguments(self, parser):
        parser.add_argument("--username", help="Attribute the manual run to this authorized owner.")

    def handle(self, *args, **options):
        try:
            run = start_price_reconciliation(actor=command_actor(options["username"]))
        except PermissionDenied:
            raise CommandError("permission_denied") from None
        except ImproperlyConfigured:
            raise CommandError("invalid_moysklad_configuration") from None
        except DatabaseError:
            raise CommandError("moysklad_database_unavailable") from None
        except MoySkladError as error:
            raise CommandError(error.code) from None
        self.stdout.write(f"prices_queued run_id={run.pk}")
