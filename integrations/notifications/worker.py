"""Outbox worker with independent PostgreSQL listener and ORM connections."""

from contextlib import suppress
from datetime import timedelta
import logging
import time

import psycopg
from django.conf import settings
from django.db import DatabaseError, connections

from .contracts import (
    DeliveryResult,
    NotificationConfigurationError,
    PermanentDeliveryError,
    TemporaryDeliveryError,
)
from .events import log_notification_event
from .payloads import build_notification
from .queue import NotificationQueue


logger = logging.getLogger(__name__)
CHANNEL = "notification_outbox"


def load_backend():
    """Import and validate one configured backend before any queue claim."""
    from django.utils.module_loading import import_string

    try:
        backend_class = import_string(settings.NOTIFICATION_BACKEND)
        backend = backend_class()
        validator = getattr(backend, "validate_configuration", None)
        if callable(validator) and not getattr(backend, "configuration_validated", False):
            validator()
    except NotificationConfigurationError:
        raise
    except Exception as exc:
        raise NotificationConfigurationError("backend_unavailable") from exc
    if not callable(getattr(backend, "send", None)):
        raise NotificationConfigurationError("backend_contract_invalid")
    return backend


class PsycopgNotificationListener:
    """Dedicated autocommit LISTEN connection; its payload is only a wake-up hint."""

    def __init__(self, *, using="default", monotonic=time.monotonic):
        self.using = using
        self.monotonic = monotonic
        orm_connection = connections[using]
        self.connection = psycopg.connect(**orm_connection.get_connection_params(), autocommit=True)

    def listen(self):
        self.connection.execute(f'LISTEN "{CHANNEL}"')

    def wait(self, timeout, stop_event):
        deadline = self.monotonic() + max(0, timeout)
        while not stop_event.is_set():
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                return False
            notifications = self.connection.notifies(timeout=min(1.0, remaining), stop_after=1)
            try:
                next(notifications)
                return True
            except StopIteration:
                continue
            finally:
                notifications.close()
        return False

    def close(self):
        self.connection.close()


class NotificationWorker:
    def __init__(
        self,
        backend,
        *,
        queue=None,
        listener_factory=None,
        scan_interval=None,
        stop_event=None,
        now=None,
        monotonic=time.monotonic,
    ):
        import threading

        self.backend = backend
        self.queue = queue or NotificationQueue()
        self.monotonic = monotonic
        self.listener_factory = listener_factory or (
            lambda: PsycopgNotificationListener(using=self.queue.using, monotonic=self.monotonic)
        )
        self.scan_interval = int(
            scan_interval if scan_interval is not None else settings.NOTIFICATION_SCAN_INTERVAL_SECONDS
        )
        if self.scan_interval <= 0:
            raise ValueError("Notification scan interval must be positive")
        self.stop_event = stop_event or threading.Event()
        self.now = now or self.queue.now
        self.backend_name = getattr(backend, "backend_name", backend.__class__.__name__)

    def _deliver(self, claim):
        started = self.monotonic()
        result_name = "unknown_failure"
        error_code = ""
        send_started = False
        try:
            try:
                notification = build_notification(claim.outbox_id, using=self.queue.using)
                send_started = True
                result = self.backend.send(notification, delivery_key=notification.delivery_key)
                if not isinstance(result, DeliveryResult) or not result.accepted:
                    raise PermanentDeliveryError("backend_result_invalid")
            except TemporaryDeliveryError as exc:
                error_code = exc.code
                result_name = (
                    "manual_review"
                    if claim.attempt_count >= self.queue.max_attempts
                    else "retry_scheduled"
                )
                finalized = self.queue.mark_temporary_failure(claim, error_code)
            except PermanentDeliveryError as exc:
                error_code = exc.code
                result_name = "manual_review"
                finalized = self.queue.mark_permanent_failure(claim, error_code)
            except DatabaseError:
                # Preserve DB failures for the outer logging/recovery boundary.
                raise
            except Exception:
                error_code = "unexpected_delivery_failure"
                result_name = "manual_review"
                finalized = self.queue.mark_permanent_failure(claim, error_code)
            else:
                finalized = self.queue.mark_sent(claim)
                result_name = "sent"
        except DatabaseError:
            # A delivery or its terminal transition may have reached the
            # transport/DB independently. Keep the lease for recovery and emit
            # one safe warning for every database failure in this attempt.
            log_notification_event(
                logger,
                logging.WARNING,
                event="database_failure_during_delivery",
                outbox_id=claim.outbox_id,
                backend=self.backend_name,
                result="uncertain_database_failure" if send_started else "database_failure_before_send",
                duration_seconds=round(max(0, self.monotonic() - started), 3),
                error_code="database_failure",
            )
            raise

        if not finalized:
            result_name = "stale_owner"
        log_notification_event(
            logger,
            logging.INFO if finalized else logging.WARNING,
            event="delivery_attempt",
            outbox_id=claim.outbox_id,
            backend=self.backend_name,
            result=result_name,
            duration_seconds=round(max(0, self.monotonic() - started), 3),
            error_code=error_code if finalized else (error_code or "lease_not_owned"),
        )

    def drain_ready(self):
        processed = 0
        while not self.stop_event.is_set():
            claim = self.queue.claim_next()
            if claim is None:
                break
            self._deliver(claim)
            processed += 1
        return processed

    def run_once(self):
        return self.drain_ready()

    def run(self):
        reconnect_delay = 1.0
        log_notification_event(
            logger, logging.INFO, event="worker_started", backend=self.backend_name, result="started"
        )
        while not self.stop_event.is_set():
            listener = None
            try:
                listener = self.listener_factory()
                listener.listen()
                next_scan_at = self.now()
                while not self.stop_event.is_set():
                    now = self.now()
                    if now >= next_scan_at:
                        self.drain_ready()
                        next_scan_at = self.now() + timedelta(seconds=self.scan_interval)
                    if self.stop_event.is_set():
                        break

                    due_at = self.queue.next_due_at()
                    deadlines = [next_scan_at]
                    if due_at is not None:
                        deadlines.append(due_at)
                    delay = (min(deadlines) - self.now()).total_seconds()
                    # A ready row may be briefly locked by another worker. Poll at
                    # a bounded cadence instead of spinning on the same SKIP LOCKED row.
                    if delay <= 0:
                        delay = min(1.0, float(self.scan_interval))
                    notified = listener.wait(delay, self.stop_event)
                    # Reset only after both the ORM scan and listener wait have
                    # completed a useful cycle.
                    reconnect_delay = 1.0
                    # Both a wake-up notification and the nearest due/scan
                    # deadline require another ready-task drain.
                    if notified or not self.stop_event.is_set():
                        next_scan_at = self.now()
            except (psycopg.Error, DatabaseError):
                log_notification_event(
                    logger,
                    logging.WARNING,
                    event="worker_database_reconnect",
                    backend=self.backend_name,
                    result="reconnecting",
                    error_code="database_connection_failure",
                )
                with suppress(Exception):
                    connections.close_all()
                if not self.stop_event.is_set():
                    self.stop_event.wait(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, 60.0)
            finally:
                if listener is not None:
                    with suppress(Exception):
                        listener.close()
        connections.close_all()
        log_notification_event(
            logger, logging.INFO, event="worker_stopped", backend=self.backend_name, result="stopped"
        )
