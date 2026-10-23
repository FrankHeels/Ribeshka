"""Short atomic claims and lease guarded queue transitions."""

from datetime import datetime, timedelta
import random
import uuid

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from integrations.models import MoySkladSyncRun, MoySkladTask
from .contracts import LeaseLost, safe_error_code


def enqueue_task(*, kind, dedupe_key, account_id, environment, **kwargs):
    """Deduplicate active work; export and inbox identities survive completion."""
    if kind not in MoySkladTask.Kind.values:
        raise ValueError("Unknown MoySklad task kind.")
    identity = {"account_id": account_id, "environment": environment, "kind": kind, "dedupe_key": dedupe_key}
    query = MoySkladTask.objects.filter(**identity)
    if kind not in (MoySkladTask.Kind.EXPORT_ORDER, MoySkladTask.Kind.DELETE_ORDER, MoySkladTask.Kind.WEBHOOK):
        query = query.filter(status__in=(MoySkladTask.Status.PENDING, MoySkladTask.Status.PROCESSING))
    # get_or_create retries its lookup after a concurrent unique conflict.
    task, _created = query.get_or_create(defaults=kwargs, **identity)
    if kwargs.get("force_apply") and not task.force_apply:
        MoySkladTask.objects.filter(pk=task.pk).update(force_apply=True)
        task.force_apply = True
    return task


def _finish_manual_run(task, code, now):
    if task.sync_run_id:
        run = MoySkladSyncRun.objects.filter(pk=task.sync_run_id, status=MoySkladSyncRun.Status.RUNNING).first()
        if run:
            MoySkladSyncRun.objects.filter(pk=run.pk, status=MoySkladSyncRun.Status.RUNNING).update(
                status=MoySkladSyncRun.Status.PARTIAL if run.processed_count else MoySkladSyncRun.Status.FAILED,
                last_error_code=code, completed_at=now,
            )


def _expire_manual(task, now):
    changed = MoySkladTask.objects.filter(
        pk=task.pk, status=MoySkladTask.Status.PROCESSING,
        lease_token=task.lease_token, lease_expires_at__lte=now,
    ).update(
        status=MoySkladTask.Status.FAILED, lease_token=None, lease_expires_at=None,
        last_error_code="lease_expired", first_error_at=Coalesce("first_error_at", now),
        completed_at=now, updated_at=now,
    )
    if changed:
        _finish_manual_run(task, "lease_expired", now)
    return bool(changed)


def claim_task(*, account_id=None, environment=None, kinds=None):
    """Commit ownership before returning; external I/O must follow the claim."""
    while True:
        now = timezone.now()
        ready = Q(status=MoySkladTask.Status.PENDING, next_attempt_at__lte=now) | Q(
            status=MoySkladTask.Status.PROCESSING, lease_expires_at__lte=now,
        )
        query = MoySkladTask.objects.filter(ready)
        if account_id is not None:
            query = query.filter(account_id=account_id)
        if environment is not None:
            query = query.filter(environment=environment)
        if kinds is not None:
            query = query.filter(kind__in=kinds)
        with transaction.atomic():
            task = query.select_for_update(skip_locked=True).order_by("next_attempt_at", "created_at", "pk").first()
            if task is None:
                return None
            if task.status == MoySkladTask.Status.PROCESSING and task.kind == MoySkladTask.Kind.CATALOG_SYNC:
                _expire_manual(task, now)
                continue
            task.status = MoySkladTask.Status.PROCESSING
            task.attempt_count += 1
            task.lease_token = uuid.uuid4()
            task.lease_expires_at = now + timedelta(seconds=getattr(settings, "MOYSKLAD_LEASE_SECONDS", 300))
            task.completed_at = None
            task.save(update_fields=("status", "attempt_count", "lease_token", "lease_expires_at", "completed_at", "updated_at"))
            return task


def _owned(task, now):
    return MoySkladTask.objects.filter(
        pk=task.pk, status=MoySkladTask.Status.PROCESSING,
        lease_token=task.lease_token, lease_token__isnull=False, lease_expires_at__gt=now,
    )


def ensure_lease(task):
    """Return false for a lost lease, preserving recovery updates on manual work."""
    now = timezone.now()
    if _owned(task, now).exists():
        return True
    if task.kind == MoySkladTask.Kind.CATALOG_SYNC:
        with transaction.atomic():
            _expire_manual(task, now)
    return False


def complete_task(task):
    now = timezone.now()
    if _owned(task, now).update(
        status=MoySkladTask.Status.DONE, lease_token=None, lease_expires_at=None,
        last_error_code="", first_error_at=None, completed_at=now, updated_at=now,
    ) != 1:
        raise LeaseLost()


def retry_task(task, error_code, *, retry_after=None):
    """Automatic exchange has no attempt cap; only a new owner action repeats catalog."""
    now = timezone.now()
    code = safe_error_code(error_code)
    if isinstance(retry_after, datetime):
        due_at = max(now, retry_after)
    elif isinstance(retry_after, timedelta):
        due_at = now + max(timedelta(0), retry_after)
    elif retry_after is not None:
        due_at = now + timedelta(seconds=max(0, float(retry_after)))
    else:
        if task.attempt_count < 5:
            delay = (30, 120, 600, 1800)[max(0, task.attempt_count - 1)]
        else:
            delay = 1800 * random.uniform(0.8, 1.2)
        due_at = now + timedelta(seconds=delay)
    manual = task.kind == MoySkladTask.Kind.CATALOG_SYNC
    with transaction.atomic():
        if _owned(task, now).update(
            status=MoySkladTask.Status.FAILED if manual else MoySkladTask.Status.PENDING,
            lease_token=None, lease_expires_at=None, last_error_code=code,
            first_error_at=Coalesce("first_error_at", now),
            next_attempt_at=due_at, completed_at=now if manual else None, updated_at=now,
        ) != 1:
            raise LeaseLost()
        if manual:
            _finish_manual_run(task, code, now)
