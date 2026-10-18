"""Bounded durable queue execution with an independent PostgreSQL wake-up hint."""

from contextlib import suppress
from datetime import timedelta
import logging
import threading
import time

import psycopg
from django.core.exceptions import ObjectDoesNotExist
from django.db import DatabaseError, connections, transaction
from django.db.models import Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from integrations.models import MoySkladApiState, MoySkladIncident, MoySkladOrderLink, MoySkladSyncState, MoySkladTask
from integrations.notifications.worker import PsycopgNotificationListener
from .catalog_sync import process_catalog_task
from .checks import check_connection
from .config import get_config
from .contracts import LeaseLost, MoySkladError, safe_error_code
from .controls import effective_config, locked_state
from .incidents import _close_observed_incident, open_incident
from .order_export import process_export_task
from .price_sync import process_price_reconciliation, process_price_task, start_price_reconciliation
from .queue import claim_task, complete_task, enqueue_task, ensure_lease, retry_task
from .status_sync import process_status_task
from .throttling import ApiThrottle
from .webhooks import process_inbox


logger = logging.getLogger(__name__)
CHANNEL = "moysklad_tasks"
SCAN_SECONDS = 30


class MoySkladListener(PsycopgNotificationListener):
    def listen(self):
        self.connection.execute(f'LISTEN "{CHANNEL}"')


class MoySkladWorker:
    def __init__(self, config=None, client=None, stop_event=None):
        self.config = get_config(config)
        self._client = client
        self.stop_event = stop_event or threading.Event()
        self._group = 0
        self._short = 0
        self._parent = 0
        self._database_recovery = False

    @property
    def client(self):
        if self._client is None:
            from .client import MoySkladClient

            self._client = MoySkladClient(self.config)
        return self._client

    def close(self):
        if self._client is not None:
            self._client.close()
            self._client = None

    def _heartbeat(self):
        MoySkladSyncState.objects.filter(
            account_id=self.config.account_id, environment=self.config.environment,
            namespace=self.config.namespace,
        ).update(heartbeat_at=timezone.now())

    def _schedule_statuses(self, state, config, now):
        if not config.statuses_enabled or (state.status_next_at and state.status_next_at > now):
            return
        terminal = ("Successful", "Unsuccessful")
        due = Q(status_checked_at__isnull=True) | (
            Q(state_type__in=terminal, status_checked_at__lte=now - timedelta(days=1))
        ) | (~Q(state_type__in=terminal) & Q(status_checked_at__lte=now - timedelta(minutes=5)))
        links = MoySkladOrderLink.objects.filter(
            due, account_id=config.account_id, environment=config.environment, external_id__isnull=False,
        ).exclude(tasks__kind="status_sync", tasks__status__in=("pending", "processing"))
        for link in links.order_by("status_checked_at", "pk")[:100]:
            enqueue_task(kind="status_sync", dedupe_key=f"status:{link.pk}",
                         account_id=config.account_id, environment=config.environment,
                         order_link=link, object_id=str(link.order_id))
        state.status_next_at = now + timedelta(seconds=SCAN_SECONDS)

    def _schedule(self):
        now = timezone.now()
        with transaction.atomic():
            state = locked_state(self.config)
            config = effective_config(self.config, state)
            stale = state.heartbeat_at is None or state.heartbeat_at <= now - timedelta(minutes=2)
            if config.prices_enabled:
                first_enable = state.prices_enabled_at is None
                if first_enable:
                    state.prices_enabled_at = now
                recovery = first_enable or stale or state.prices_next_at is None or self._database_recovery
                scheduled = state.prices_next_at is not None and state.prices_next_at <= now
                if recovery or scheduled:
                    # Commit the next due date and the coalesced run together.
                    state.prices_next_at = now + timedelta(days=1)
                    state.save(update_fields=("prices_next_at", "prices_enabled_at", "updated_at"))
                    start_price_reconciliation(reason="recovery" if recovery else "scheduled")
            self._schedule_statuses(state, config, now)
            state.heartbeat_at = now
            state.save(update_fields=("heartbeat_at", "status_next_at", "prices_enabled_at", "updated_at"))
        self._database_recovery = False
        # Expired manual work must fail even if its stream was subsequently stopped.
        expired = MoySkladTask.objects.filter(
            account_id=config.account_id, environment=config.environment, kind="catalog_sync",
            status="processing", lease_expires_at__lte=now,
        ).order_by("pk")[:100]
        for task in expired:
            if not ensure_lease(task):
                open_incident(scope="catalog", code="lease_expired", object_ids=[str(task.sync_run_id)],
                              account_id=config.account_id, environment=config.environment)
        return state, config

    def _claim(self, state, config):
        now = timezone.now()
        api = MoySkladApiState.objects.filter(account_id=config.account_id).first()
        access_paused = (api.access_error_code if api else state.access_error_code)
        export_paused = (api.export_paused if api else state.export_paused)
        rate_until = api.api_paused_until if api else state.api_paused_until
        busy_until = api.api_lease_expires_at if api else state.api_lease_expires_at
        available = not ((rate_until and rate_until > now) or (busy_until and busy_until > now))
        network = available and not access_paused

        def claim(kinds):
            if not kinds:
                return None
            return claim_task(account_id=config.account_id, environment=config.environment, kinds=kinds)

        if available:
            task = claim(("check_access",))
            if task:
                return task
        short = []
        if network and config.export_enabled and not export_paused:
            short.append("export_order")
        if network and config.prices_enabled:
            short.append("price")
        parents = []
        if network and config.catalog_enabled:
            parents.append("catalog_sync")
        if config.prices_enabled:
            parents.append("price_reconciliation")
        if network and config.statuses_enabled:
            parents.append("status_sync")
        # A bounded group turn prevents a continuous short queue starving parents.
        for offset in range(3):
            group = (self._group + offset) % 3
            kinds = ["webhook"] if group == 0 else short if group == 1 else parents
            start = 0 if group == 0 else self._short if group == 1 else self._parent
            for index in range(len(kinds)):
                position = (start + index) % len(kinds)
                task = claim((kinds[position],))
                if task:
                    self._group = (group + 1) % 3
                    if group == 1:
                        self._short = position + 1
                    elif group == 2:
                        self._parent = position + 1
                    return task
        return None

    def _check_access(self, task):
        from .client import MoySkladClient

        throttle = ApiThrottle(self.config)
        revisions = throttle.pause_revisions()
        observed = list(MoySkladIncident.objects.filter(
            account_id=task.account_id, environment=task.environment, closed_at__isnull=True,
        ).filter(Q(scope="access") | Q(scope="order_export", code="document_limit")))
        # The probe bypasses only the access gate, retaining rate and lease limits.
        self.close()
        with MoySkladClient(self.config, readonly_probe=True) as client:
            check_connection(client, self.config)
        with transaction.atomic():
            # Lock account/workflow state before task changes. A later failure
            # invalidates this probe without clearing either pause or waking work.
            if throttle.pause_revisions() != revisions:
                now = timezone.now()
                changed = MoySkladTask.objects.filter(
                    pk=task.pk, status="processing", lease_token=task.lease_token,
                    lease_expires_at__gt=now,
                ).update(
                    status="manual_review", lease_token=None, lease_expires_at=None,
                    last_error_code="access_state_changed", first_error_at=Coalesce("first_error_at", now),
                    completed_at=now, updated_at=now,
                )
                if changed != 1:
                    raise LeaseLost()
                return
            if not ensure_lease(task):
                raise LeaseLost()
            throttle.clear_access_pause(expected_revision=revisions[0])
            throttle.clear_export_pause(expected_revision=revisions[1])
            complete_task(task)
            for incident in observed:
                _close_observed_incident(incident)
            MoySkladTask.objects.filter(
                account_id=task.account_id, environment=task.environment, status="pending",
                last_error_code__in=("access_denied", "document_limit", "stream_paused"),
            ).exclude(kind="catalog_sync").update(next_attempt_at=timezone.now())
            if self._prices_enabled():
                start_price_reconciliation(reason="recovery")

    def _prices_enabled(self):
        state = MoySkladSyncState.objects.get(
            account_id=self.config.account_id, environment=self.config.environment,
        )
        return effective_config(self.config, state).prices_enabled

    def _dispatch(self, task):
        if task.kind == "webhook":
            process_inbox(task)
        elif task.kind == "price_reconciliation":
            process_price_reconciliation(task)
        elif task.kind == "check_access":
            self._check_access(task)
        else:
            handlers = {"price": process_price_task, "export_order": process_export_task,
                        "catalog_sync": process_catalog_task, "status_sync": process_status_task}
            handlers[task.kind](task, client=self.client)

    def _failure(self, task, error):
        if not ensure_lease(task):
            return
        code = safe_error_code(error.code)
        access = code == "access_denied" or error.status_code in (401, 403)
        if access:
            ApiThrottle(self.config).pause_access()
            open_incident(scope="access", code="access_denied", account_id=task.account_id,
                          environment=task.environment)
        if task.kind == "catalog_sync" or error.retryable or access:
            retry_task(task, code, retry_after=error.retry_after)
        else:
            now = timezone.now()
            changed = MoySkladTask.objects.filter(
                pk=task.pk, status="processing", lease_token=task.lease_token, lease_expires_at__gt=now,
            ).update(status="manual_review", lease_token=None, lease_expires_at=None,
                     last_error_code=code, first_error_at=Coalesce("first_error_at", now), updated_at=now)
            if not changed:
                raise LeaseLost()
        if task.kind == "catalog_sync" or (not error.retryable and not access):
            open_incident(scope="catalog" if task.kind == "catalog_sync" else "task", code=code,
                          object_ids=[str(task.pk)], account_id=task.account_id, environment=task.environment)

    def run_once(self):
        """Schedule locally, then execute at most one ready task in this target."""
        if self.stop_event.is_set() or not self.config.account_id or not self.config.namespace:
            return False
        state, config = self._schedule()
        task = self._claim(state, config)
        if task is None:
            return False
        started = time.monotonic()
        code = "processed"
        try:
            self._dispatch(task)
            result = MoySkladTask.objects.filter(pk=task.pk).values("status", "last_error_code").first()
            if result:
                code = result["last_error_code"] or result["status"]
        except LeaseLost:
            code = "lease_lost"
        except DatabaseError:
            # Keep the lease recoverable; the process reconnects outside the task.
            raise
        except MoySkladError as error:
            code = error.code
            with suppress(LeaseLost):
                self._failure(task, error)
        except ObjectDoesNotExist:
            code = "task_object_missing"
            with suppress(LeaseLost):
                self._failure(task, MoySkladError(code))
        except Exception:
            code = "worker_error"
            with suppress(LeaseLost):
                self._failure(task, MoySkladError(code, retryable=True))
        finally:
            self._heartbeat()
            logger.info("moysklad_task task_id=%s kind=%s code=%s duration_ms=%s", task.pk,
                        task.kind, safe_error_code(code), int((time.monotonic() - started) * 1000))
        return True

    def run(self):
        reconnect_delay = 1.0
        try:
            while not self.stop_event.is_set():
                listener = None
                try:
                    listener = MoySkladListener()
                    listener.listen()
                    # Startup, reconnect, notification and timeout always rescan the durable queue.
                    while not self.stop_event.is_set():
                        worked = self.run_once()
                        reconnect_delay = 1.0
                        if not worked:
                            listener.wait(SCAN_SECONDS, self.stop_event)
                except (psycopg.Error, DatabaseError):
                    logger.warning("moysklad_worker code=database_connection_failure")
                    self._database_recovery = True
                    with suppress(Exception):
                        connections.close_all()
                    if not self.stop_event.is_set():
                        self.stop_event.wait(reconnect_delay)
                        reconnect_delay = min(reconnect_delay * 2, 60.0)
                finally:
                    if listener is not None:
                        with suppress(Exception):
                            listener.close()
        finally:
            try:
                self.close()
            finally:
                connections.close_all()
