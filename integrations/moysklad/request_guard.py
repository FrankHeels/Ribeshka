"""Bind HTTP requests to the task that still owns their execution."""

from contextlib import contextmanager
from contextvars import ContextVar

from integrations.models import MoySkladOrderLink
from .contracts import LeaseLost
from .queue import ensure_lease


_request_task = ContextVar("moysklad_request_task", default=None)


@contextmanager
def task_request_scope(task):
    token = _request_task.set(task)
    try:
        yield
    finally:
        _request_task.reset(token)


def ensure_request_task():
    """Recheck after acquiring the HTTP slot, without locks across provider I/O."""
    task = _request_task.get()
    if task is None:
        return
    if not ensure_lease(task):
        raise LeaseLost()
    if task.kind in ("export_order", "status_sync") and not MoySkladOrderLink.objects.filter(
        pk=task.order_link_id, deletion_requested_at__isnull=True,
    ).exists():
        raise LeaseLost()
