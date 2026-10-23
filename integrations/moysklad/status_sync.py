"""Read accounting status without modifying the accepted local order."""

from collections.abc import Mapping
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from integrations.models import MoySkladApiState, MoySkladOrderLink, MoySkladSyncState
from .config import get_config
from .contracts import LeaseLost, MoySkladError
from .mapping import API_BASE, uuid_value
from .order_export import _fail, _owned_link
from .queue import complete_task, enqueue_task, ensure_lease, retry_task

TERMINAL_TYPES = frozenset(("Successful", "Unsuccessful"))


def status_interval(link):
    return timedelta(days=1) if link.state_type in TERMINAL_TYPES else timedelta(minutes=5)


def _state_id(state):
    if state.get("id") is not None:
        return uuid_value(state["id"])
    meta = state.get("meta")
    prefix = f"{API_BASE}entity/customerorder/metadata/states/"
    href = meta.get("href") if isinstance(meta, Mapping) else None
    if not isinstance(href, str) or not href.startswith(prefix):
        raise MoySkladError("invalid_state")
    identifier = uuid_value(href[len(prefix):])
    if href != f"{prefix}{identifier}" or meta.get("type") != "state":
        raise MoySkladError("invalid_state")
    return identifier


def _state_values(document, task, client):
    state = document.get("state")
    if state is None:
        return None, "", ""
    if not isinstance(state, Mapping):
        raise MoySkladError("invalid_state")
    identifier = _state_id(state)
    name = state.get("name")
    state_type = state.get("stateType")
    if name is None or state_type is None:
        if not ensure_lease(task):
            raise LeaseLost()
        metadata = client.get("entity/customerorder/metadata")
        if not isinstance(metadata, Mapping) or not isinstance(metadata.get("states"), list):
            raise MoySkladError("invalid_state_metadata")
        match = next((row for row in metadata["states"] if isinstance(row, Mapping) and _state_id(row) == identifier), None)
        if match is not None:
            name, state_type = match.get("name", name), match.get("stateType", state_type)
    if isinstance(state_type, Mapping):
        state_type = state_type.get("name", "")
    name, state_type = name or "", state_type or ""
    if not isinstance(name, str) or len(name) > 255 or not isinstance(state_type, str) or len(state_type) > 32:
        raise MoySkladError("invalid_state")
    return identifier, name, state_type


def process_status_task(task, *, client):
    if task is None or task.kind != "status_sync" or not task.order_link_id or not ensure_lease(task):
        return
    try:
        config = get_config()
        link = MoySkladOrderLink.objects.filter(pk=task.order_link_id).first()
        if link is None:
            return
        if link.deletion_requested_at is not None:
            complete_task(task)
            return
        state = MoySkladSyncState.objects.filter(account_id=task.account_id, environment=task.environment).first()
        if link.account_id != task.account_id or link.environment != task.environment:
            raise MoySkladError("account_mismatch")
        enabled = state.statuses_enabled if state and state.statuses_enabled is not None else config.statuses_enabled
        api = MoySkladApiState.objects.filter(account_id=task.account_id).first()
        if config.account_id != task.account_id or config.environment != task.environment or not enabled or (api and api.access_error_code):
            retry_task(task, "stream_paused")
            return
        if link.external_id is None:
            complete_task(task)
            return
        document = client.get(f"entity/customerorder/{link.external_id}")
        if not isinstance(document, Mapping) or uuid_value(document.get("id")) != link.external_id:
            raise MoySkladError("external_identity_mismatch")
        if document.get("accountId") is not None and uuid_value(document["accountId"]) != link.account_id:
            raise MoySkladError("account_mismatch")
        if document.get("deleted") or document.get("deletedAt"):
            raise MoySkladError("external_order_deleted")
        identifier, name, state_type = _state_values(document, task, client)
        with transaction.atomic():
            current = _owned_link(task)
            current.state_id, current.state_name, current.state_type = identifier, name, state_type
            current.status_checked_at = timezone.now()
            current.last_error_code = ""
            current.save(update_fields=("state_id", "state_name", "state_type", "status_checked_at", "last_error_code", "updated_at"))
            complete_task(task)
        from .incidents import close_recovered_order_incidents

        close_recovered_order_incidents(account_id=link.account_id, environment=link.environment)
    except LeaseLost:
        return
    except MoySkladError as error:
        if error.status_code == 404:
            error = MoySkladError("external_order_deleted")
        try:
            _fail(task, error)
        except LeaseLost:
            return


def schedule_status_tasks(config=None, *, now=None):
    config = get_config(config)
    now = now or timezone.now()
    if config.account_id is None:
        return 0
    state = MoySkladSyncState.objects.filter(account_id=config.account_id, environment=config.environment).first()
    enabled = state.statuses_enabled if state and state.statuses_enabled is not None else config.statuses_enabled
    if not enabled:
        return 0
    count = 0
    for link in MoySkladOrderLink.objects.filter(account_id=config.account_id, environment=config.environment, external_id__isnull=False, deletion_requested_at__isnull=True).iterator():
        if link.status_checked_at is None or link.status_checked_at <= now - status_interval(link):
            count += enqueue_status_task(link.pk)
    return count


def enqueue_status_task(link_id):
    """Recheck a scheduled snapshot under the link lock before inserting work."""
    with transaction.atomic():
        link = MoySkladOrderLink.objects.select_for_update().filter(
            pk=link_id, external_id__isnull=False, deletion_requested_at__isnull=True,
        ).first()
        if link is None:
            return 0
        enqueue_task(kind="status_sync", dedupe_key=f"status:{link.pk}", account_id=link.account_id,
                     environment=link.environment, order_link=link, object_id=str(link.order_id))
        return 1
