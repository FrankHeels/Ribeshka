"""Durable remote deletion without creating or rewriting a customer order."""

from collections.abc import Mapping

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from integrations.models import MoySkladOrderLink, MoySkladTask
from orders.models import Order
from orders.services.deletion import locked_order_work, purge_order, validate_deletion_target
from integrations.request_deletion import locked_request_outbox

from .contracts import LeaseLost, MoySkladError
from .mapping import uuid_value
from .queue import complete_task, ensure_lease, retry_task
from .throttling import ApiThrottle


def _owned_deletion(task):
    """All deletion mutations acquire Order → link → task, then check lease."""
    order_id = MoySkladOrderLink.objects.filter(pk=task.order_link_id).values_list("order_id", flat=True).first()
    if order_id is None:
        raise LeaseLost()
    order = Order.objects.select_for_update().filter(pk=order_id).first()
    link = MoySkladOrderLink.objects.select_for_update().filter(pk=task.order_link_id).first()
    current = MoySkladTask.objects.select_for_update().filter(pk=task.pk).first()
    if order is None or link is None or current is None or current.lease_token != task.lease_token or not ensure_lease(task):
        raise LeaseLost()
    if link.deletion_requested_at is None:
        raise LeaseLost()
    return order, link


def _verify_identity(document, link, *, require_sync=False):
    if not isinstance(document, Mapping):
        raise MoySkladError("invalid_order_response")
    identifier = uuid_value(document.get("id"))
    if uuid_value(document.get("accountId")) != link.account_id:
        raise MoySkladError("account_mismatch")
    if link.external_id and identifier != link.external_id:
        raise MoySkladError("external_identity_mismatch")
    if require_sync and uuid_value(document.get("syncId")) != link.sync_id:
        raise MoySkladError("sync_id_mismatch")
    return identifier


def _verify_absence_account(link, task, client):
    """A negative order lookup alone cannot identify the credential's account."""
    identifier = uuid_value(link.config_snapshot.get("organization_id"))
    if not ensure_lease(task):
        raise LeaseLost()
    document = client.get(f"entity/organization/{identifier}")
    if not isinstance(document, Mapping) or uuid_value(document.get("id")) != identifier:
        raise MoySkladError("identity_mismatch")
    if uuid_value(document.get("accountId")) != link.account_id:
        raise MoySkladError("account_mismatch")


def _find_for_deletion(link, task, client):
    if not ensure_lease(task):
        raise LeaseLost()
    data = client.get("entity/customerorder", params={
        "filter": f"syncId={link.sync_id};isDeleted=true;isDeleted=false", "limit": 100,
    })
    if not isinstance(data, Mapping) or not isinstance(data.get("rows"), list):
        raise MoySkladError("invalid_order_response")
    rows, meta = data["rows"], data.get("meta")
    if not isinstance(meta, Mapping):
        raise MoySkladError("ambiguous_sync_id")
    size = meta.get("size")
    if (isinstance(size, bool) or not isinstance(size, int) or size != len(rows) or len(rows) > 1
            or meta.get("offset", 0) != 0 or meta.get("nextHref") or data.get("nextHref")):
        raise MoySkladError("ambiguous_sync_id")
    return rows[0] if rows else None


def _finish(task):
    with transaction.atomic():
        order, link = _owned_deletion(task)
        tasks = locked_order_work(link)
        outbox = locked_request_outbox(order=order)
        # Deleting the task itself is the successful terminal result: retaining
        # it would protect the link and retain the deleted request's data.
        purge_order(order, link, tasks=tasks, outbox=outbox)


def _failure(task, error):
    if not ensure_lease(task):
        return
    if error.status_code in (401, 403):
        ApiThrottle().pause_access()
    elif error.status_code == 429:
        ApiThrottle().defer(error.retry_after if error.retry_after is not None else 1.0)
    with transaction.atomic():
        _order, link = _owned_deletion(task)
        link.last_error_code = error.code
        link.save(update_fields=("last_error_code", "updated_at"))
        if error.retryable or error.code == "access_denied" or error.status_code in (401, 403, 429):
            retry_task(task, error.code, retry_after=error.retry_after)
        else:
            MoySkladTask.objects.filter(pk=task.pk, lease_token=task.lease_token).update(
                status="manual_review", lease_token=None, lease_expires_at=None,
                last_error_code=error.code, first_error_at=task.first_error_at or timezone.now(),
                completed_at=timezone.now(), updated_at=timezone.now(),
            )


def process_delete_task(task, *, client):
    if task is None or task.kind != "delete_order" or not task.order_link_id or not ensure_lease(task):
        return
    try:
        link = MoySkladOrderLink.objects.filter(pk=task.order_link_id).first()
        if link is None:
            return
        if link.account_id != task.account_id or link.environment != task.environment:
            raise MoySkladError("account_mismatch")
        if link.deletion_requested_at is None:
            complete_task(task)
            return
        try:
            validate_deletion_target(link)
        except ValidationError:
            raise MoySkladError("target_changed") from None
        identifier = link.external_id
        if identifier is None:
            # This is the persisted identity from the original attempt. Never
            # inspect an unverified existing_external_id override candidate.
            found = _find_for_deletion(link, task, client)
            if found is None:
                _verify_absence_account(link, task, client)
                _finish(task)
                return
            identifier = _verify_identity(found, link, require_sync=True)
            with transaction.atomic():
                _order, current = _owned_deletion(task)
                current.external_id = identifier
                current.save(update_fields=("external_id", "updated_at"))
        else:
            if not ensure_lease(task):
                return
            try:
                document = client.get(f"entity/customerorder/{identifier}")
            except MoySkladError as error:
                if error.status_code == 404:
                    _verify_absence_account(link, task, client)
                    _finish(task)
                    return
                raise
            _verify_identity(document, link)
        if not ensure_lease(task):
            return
        try:
            client.delete(f"entity/customerorder/{identifier}")
        except MoySkladError as error:
            if error.status_code != 404:
                raise
        _finish(task)
    except LeaseLost:
        return
    except (IntegrityError, ValidationError, MoySkladError) as error:
        if isinstance(error, IntegrityError):
            error = MoySkladError("external_order_already_linked")
        elif isinstance(error, ValidationError):
            error = MoySkladError("request_busy", retryable=True)
        try:
            _failure(task, error)
        except LeaseLost:
            return
