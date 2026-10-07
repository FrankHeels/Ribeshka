"""Run the transactional outbox delivery worker."""

import signal
import threading

from django.core.management.base import BaseCommand, CommandError

from integrations.notifications.contracts import NotificationConfigurationError
from integrations.notifications.worker import NotificationWorker, load_backend


class Command(BaseCommand):
    help = "Deliver saved retail and wholesale request notifications."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Drain tasks ready now, then exit without waiting for scheduled retries.",
        )

    def handle(self, *args, **options):
        try:
            backend = load_backend()
        except NotificationConfigurationError as exc:
            raise CommandError(f"Notification backend configuration is invalid ({exc.code}).") from exc

        stop_event = threading.Event()
        previous_handlers = {}

        def request_stop(signum, frame):
            stop_event.set()

        try:
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, request_stop)

            worker = NotificationWorker(backend, stop_event=stop_event)
            if options["once"]:
                count = worker.run_once()
                self.stdout.write(f"Processed {count} ready notification(s).")
                return
            worker.run()
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)

