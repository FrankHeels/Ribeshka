"""Prepare consent and exchange; each order still needs owner approval."""

from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from integrations.moysklad.contracts import MoySkladError
from integrations.moysklad.controls import command_actor, start_export


class Command(BaseCommand):
    help = "Approve consent and enable delivery of owner-selected orders; requires a technical administrator (superuser)."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)

    def handle(self, *args, **options):
        try:
            state = start_export(actor=command_actor(options["username"], required=True))
        except PermissionDenied:
            raise CommandError("permission_denied") from None
        except ImproperlyConfigured:
            raise CommandError("invalid_moysklad_configuration") from None
        except DatabaseError:
            raise CommandError("moysklad_database_unavailable") from None
        except MoySkladError as error:
            raise CommandError(error.code) from None
        self.stdout.write(f"export_started account_id={state.account_id} environment={state.environment}")
