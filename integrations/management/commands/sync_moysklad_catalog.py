"""Enqueue an explicit manual catalog pass without network activity."""

from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from integrations.moysklad.catalog_sync import start_catalog_sync
from integrations.moysklad.contracts import MoySkladError
from integrations.moysklad.controls import command_actor


class Command(BaseCommand):
    help = "Queue a manual catalog reconciliation."

    def add_arguments(self, parser):
        parser.add_argument("--username", help="Attribute the manual run to this authorized owner.")

    def handle(self, *args, **options):
        try:
            run = start_catalog_sync(actor=command_actor(options["username"]))
        except PermissionDenied:
            raise CommandError("permission_denied") from None
        except ImproperlyConfigured:
            raise CommandError("invalid_moysklad_configuration") from None
        except DatabaseError:
            raise CommandError("moysklad_database_unavailable") from None
        except MoySkladError as error:
            raise CommandError(error.code) from None
        self.stdout.write(f"catalog_queued run_id={run.pk}")
