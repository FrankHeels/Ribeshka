"""Local integration health checks and coalesced transactional email incidents."""

from datetime import timedelta
import re
import uuid

from django.db import connections, transaction
from django.utils import timezone

from integrations.models import (
    MoySkladApiState, MoySkladIncident, MoySkladOrderLink, MoySkladProduct, MoySkladSyncState, MoySkladTask,
    MoySkladWebhookInbox, NotificationOutbox,
)
from .config import get_config
from .contracts import safe_error_code

_OBJECT_ID = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")


def _target(account_id, environment):
    if account_id is not None and environment is not None:
        return account_id, environment
    config = get_config()
    return account_id or config.account_id, environment or config.environment


def open_incident(*, scope, code, object_ids=(), account_id=None, environment=None, using="default"):
    """The unique open incident and its outbox row commit together."""
    account_id, environment = _target(account_id, environment)
    if account_id is None:
        return None
    scope, code = safe_error_code(scope), safe_error_code(code)
    identifiers = {str(value) for value in object_ids if _OBJECT_ID.fullmatch(str(value))}
    identity = dict(account_id=account_id, environment=environment, scope=scope, code=code, closed_at__isnull=True)
    with transaction.atomic(using=using):
        while True:
            now = timezone.now()
            incident, created = MoySkladIncident.objects.using(using).get_or_create(
                **identity, defaults={"object_ids": sorted(identifiers), "opened_at": now, "last_seen_at": now},
            )
            if not created:
                incident = MoySkladIncident.objects.using(using).select_for_update().get(pk=incident.pk)
                # Recovery may have closed the row after get_or_create read it.
                # A new observation belongs to the current open incident.
                if incident.closed_at is not None:
                    continue
                incident.object_ids = sorted(set(incident.object_ids) | identifiers)
                incident.last_seen_at = now
                incident.save(using=using, update_fields=("object_ids", "last_seen_at"))
            else:
                NotificationOutbox.objects.using(using).create(incident=incident)
                connection = connections[using]
                if connection.vendor == "postgresql":
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT pg_notify(%s, %s)", ("notification_outbox", ""))
            break
    return incident


def close_incident(*, scope, code=None, account_id=None, environment=None, using="default"):
    account_id, environment = _target(account_id, environment)
    query = MoySkladIncident.objects.using(using).filter(
        account_id=account_id, environment=environment, scope=scope, closed_at__isnull=True,
    )
    if code is not None:
        query = query.filter(code=code)
    return query.update(closed_at=timezone.now())


def _close_observed_incident(incident):
    """Do not close observations added after this row's health was checked."""
    return MoySkladIncident.objects.using(incident._state.db).filter(
        pk=incident.pk, closed_at__isnull=True, last_seen_at=incident.last_seen_at,
        object_ids=incident.object_ids,
    ).update(closed_at=timezone.now())


def close_recovered_catalog_incidents(config, *, manual_success=False):
    """Current registry health wins over historical failed price tasks."""
    incidents = MoySkladIncident.objects.filter(
        account_id=config.account_id, environment=config.environment,
        scope="catalog", closed_at__isnull=True,
    )
    for incident in incidents:
        source_ids, run_ids = set(), set()
        for value in incident.object_ids:
            try:
                source_ids.add(uuid.UUID(str(value)))
            except (ValueError, TypeError, AttributeError):
                if str(value).isdigit():
                    run_ids.add(str(value))
                else:
                    break
        else:
            if (not source_ids and not run_ids) or (run_ids and not manual_success):
                continue
            sources = MoySkladProduct.objects.filter(
                account_id=config.account_id, entity_type="product", external_id__in=source_ids,
            )
            if sources.count() != len(source_ids):
                continue
            if sources.exclude(is_deferred=True).exclude(last_error_code="").exists():
                continue
            _close_observed_incident(incident)


def close_recovered_order_incidents(*, account_id, environment):
    """Close an object error only once every affected order has recovered."""
    incidents = MoySkladIncident.objects.filter(
        account_id=account_id, environment=environment,
        scope="order_export", closed_at__isnull=True,
    ).exclude(code__in=("document_limit", "processing_delayed"))
    for incident in incidents:
        if not incident.object_ids:
            continue
        links = MoySkladOrderLink.objects.filter(
            account_id=account_id, environment=environment,
            order__order_number__in=incident.object_ids,
        )
        if links.count() != len(set(incident.object_ids)) or links.exclude(last_error_code="").exists():
            continue
        if MoySkladTask.objects.filter(
            order_link__in=links, kind="export_order", status="manual_review",
            last_error_code=incident.code,
        ).exists():
            continue
        _close_observed_incident(incident)


def retire_deleted_order_incidents(*, account_id, environment, order_number):
    """Retire only this order's object errors, serializing with coalescing."""
    incidents = MoySkladIncident.objects.select_for_update().filter(
        account_id=account_id, environment=environment, scope="order_export",
        closed_at__isnull=True, object_ids__contains=[order_number],
    ).exclude(code__in=("document_limit", "processing_delayed", "access_denied")).order_by("pk")
    for incident in incidents:
        # The row lock makes open_incident re-read our result before merging
        # another observation; a shared incident keeps all its other orders.
        incident.object_ids = [value for value in incident.object_ids if value != order_number]
        fields = ["object_ids"]
        if not incident.object_ids:
            incident.closed_at = timezone.now()
            fields.append("closed_at")
        incident.save(update_fields=fields)


def monitor_integration(*, using="default", now=None):
    """Inspect only local durable state, independently of the API worker."""
    config = get_config()
    if config.account_id is None:
        return
    # Capture versions before any health read. A concurrent failure may create
    # or extend an incident while those reads still describe a healthy account.
    observed = list(MoySkladIncident.objects.using(using).filter(
        account_id=config.account_id, environment=config.environment, closed_at__isnull=True,
    ))
    now = now or timezone.now()
    state = MoySkladSyncState.objects.using(using).filter(
        account_id=config.account_id, environment=config.environment,
    ).first()
    if state is None:
        return
    api_state = MoySkladApiState.objects.using(using).filter(account_id=state.account_id).first()
    access_error_code = api_state.access_error_code if api_state else ""
    export_paused = api_state.export_paused if api_state else False
    target = dict(account_id=state.account_id, environment=state.environment, using=using)
    tasks = MoySkladTask.objects.using(using).filter(
        account_id=state.account_id, environment=state.environment,
        status__in=(MoySkladTask.Status.PENDING, MoySkladTask.Status.PROCESSING),
    )
    def close_observed(scope, code=None):
        for incident in observed:
            if incident.scope == scope and (code is None or incident.code == code):
                _close_observed_incident(incident)

    def observe(scope, code, condition, objects=()):
        if condition:
            open_incident(scope=scope, code=code, object_ids=objects, **target)
        else:
            close_observed(scope, code)

    enabled = {
        name: getattr(state, name) if getattr(state, name) is not None else getattr(config, name)
        for name in ("catalog_enabled", "prices_enabled", "export_enabled", "statuses_enabled")
    }
    active = any(enabled.values()) or tasks.exists()
    worker_stale = active and (state.heartbeat_at or state.created_at) < now - timedelta(minutes=2)
    observe("worker", "heartbeat_stale", worker_stale)
    observe("access", access_error_code or "access_denied", bool(access_error_code))
    if not access_error_code:
        close_observed("access")
    observe("order_export", "document_limit", export_paused)
    # A single common outage explains downstream delays; close obsolete local
    # derivative incidents so recovery can open a new incident if necessary.
    explained = worker_stale or bool(access_error_code)
    inbox = MoySkladWebhookInbox.objects.using(using).filter(
        account_id=state.account_id, environment=state.environment,
        processed_at__isnull=True, received_at__lt=now - timedelta(minutes=5),
    )
    pending = MoySkladProduct.objects.using(using).filter(
        account_id=state.account_id, product__isnull=False,
        price_pending_since__lt=now - timedelta(minutes=5),
    )
    observe("webhook", "processing_delayed", not explained and enabled["prices_enabled"] and inbox.exists(), inbox.values_list("pk", flat=True))
    observe("prices", "processing_delayed", not explained and enabled["prices_enabled"] and pending.exists(), pending.values_list("external_id", flat=True))
    mapped = MoySkladProduct.objects.using(using).filter(account_id=state.account_id, product__isnull=False).exists()
    reconciled_since = state.prices_reconciled_at or state.prices_enabled_at
    overdue = enabled["prices_enabled"] and mapped and reconciled_since is not None and reconciled_since < now - timedelta(hours=26)
    observe("prices", "reconciliation_overdue", not explained and overdue)
    exports = tasks.filter(kind="export_order", last_error_code="", created_at__lt=now - timedelta(minutes=5))
    observe("order_export", "processing_delayed", not explained and enabled["export_enabled"] and not export_paused and exports.exists(), exports.values_list("order_link__order__order_number", flat=True))
    # first_error_at is deliberately used rather than task age: a recently
    # failed old task must not look like a fifteen-minute API outage.
    failures = tasks.filter(first_error_at__lt=now - timedelta(minutes=15)).exclude(last_error_code="")
    observe("api", "temporary_failure", not explained and failures.exists(), failures.values_list("pk", flat=True))
