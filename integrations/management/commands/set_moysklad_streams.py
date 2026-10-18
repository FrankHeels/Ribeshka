"""Apply local stream switches under a technical administrator (superuser)."""

from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, transaction

from integrations.moysklad.contracts import MoySkladError
from integrations.moysklad.controls import STREAMS, command_actor, set_stream_enabled


class Command(BaseCommand):
    help = "Enable or stop named exchange streams without making API requests."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)
        for stream in STREAMS:
            parser.add_argument(f"--{stream}", choices=("on", "off"))

    def handle(self, *args, **options):
        selected = [(stream, options[stream] == "on") for stream in STREAMS if options[stream] is not None]
        if not selected:
            raise CommandError("stream_required")
        try:
            actor = command_actor(options["username"], required=True)
            with transaction.atomic():
                for stream, enabled in selected:
                    set_stream_enabled(stream, enabled, actor=actor)
        except PermissionDenied:
            raise CommandError("permission_denied") from None
        except ImproperlyConfigured:
            raise CommandError("invalid_moysklad_configuration") from None
        except DatabaseError:
            raise CommandError("moysklad_database_unavailable") from None
        except MoySkladError as error:
            raise CommandError(error.code) from None
        self.stdout.write("streams_updated")
