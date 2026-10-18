"""Read-only verification of the configured external account."""

from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from integrations.moysklad.checks import check_connection, discover_configuration
from integrations.moysklad.contracts import MoySkladError


class Command(BaseCommand):
    help = "Verify configured MoySklad IDs using GET requests only."

    def add_arguments(self, parser):
        parser.add_argument("--discover", action="store_true", help="Read at most 100 candidate IDs per dictionary.")

    def handle(self, *args, **options):
        from integrations.moysklad.client import MoySkladClient

        try:
            with MoySkladClient(readonly_probe=True) as client:
                operation = discover_configuration if options["discover"] else check_connection
                result = operation(client)
        except ImproperlyConfigured:
            raise CommandError("invalid_moysklad_configuration") from None
        except DatabaseError:
            raise CommandError("moysklad_database_unavailable") from None
        except MoySkladError as error:
            raise CommandError(error.code) from None
        self.stdout.write(result["code"])
        for field, value in result.items():
            if field != "code":
                self.stdout.write(f"{field}={value}")
