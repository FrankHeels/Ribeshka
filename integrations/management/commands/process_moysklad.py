"""Run the durable API worker; --once executes one bounded iteration."""

import signal
import threading

from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from integrations.moysklad.contracts import MoySkladError
from integrations.moysklad.worker import MoySkladWorker


class Command(BaseCommand):
    help = "Process saved MoySklad tasks and scheduled recovery checks."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Execute at most one ready task, then exit.")

    def handle(self, *args, **options):
        stop_event = threading.Event()
        previous = {}
        worker = None

        def stop(signum, frame):
            stop_event.set()

        try:
            if threading.current_thread() is threading.main_thread():
                for signum in (signal.SIGTERM, signal.SIGINT):
                    previous[signum] = signal.getsignal(signum)
                    signal.signal(signum, stop)
            worker = MoySkladWorker(stop_event=stop_event)
            if options["once"]:
                count = int(bool(worker.run_once()))
                self.stdout.write(f"Processed {count} MoySklad task(s).")
            else:
                worker.run()
        except ImproperlyConfigured:
            raise CommandError("invalid_moysklad_configuration") from None
        except DatabaseError:
            raise CommandError("moysklad_database_unavailable") from None
        except MoySkladError as error:
            raise CommandError(error.code) from None
        finally:
            try:
                if worker is not None:
                    worker.close()
            finally:
                for signum, handler in previous.items():
                    signal.signal(signum, handler)
