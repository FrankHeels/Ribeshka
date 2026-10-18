"""Immutable customer-order export with durable identity and uncertain recovery."""

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
import hashlib
import json
import uuid
from zoneinfo import ZoneInfo

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from integrations.models import MoySkladOrderLink, MoySkladSyncState, MoySkladTask
from .config import get_config
from .contracts import LeaseLost, MoySkladError
from .incidents import open_incident
from .mapping import API_BASE, reference_id, uuid_value
from .queue import complete_task, enqueue_task, ensure_lease, retry_task
from .throttling import ApiThrottle

_SNAPSHOT_FIELDS = (
    "account_id", "environment", "namespace", "currency_id", "unit_id",
    "organization_id", "agent_id", "initial_state_id",
)


def _config_snapshot(config, namespace):
    result = {name: str(getattr(config, name)) if getattr(config, name) is not None else None for name in _SNAPSHOT_FIELDS}
    result["namespace"] = str(namespace)
    return result


def enqueue_order_export(order, *, using="default", historical=False, actor=None, reason=""):
    """Register after local acceptance, even while network delivery is paused."""
    existing = MoySkladOrderLink.objects.using(using).filter(order=order).first()
    if existing is not None:
        return existing
    config = get_config()
    if config.account_id is None:
        return None
    if historical:
        _require_repair(actor)
        if not reason.strip():
            raise ValidationError("Укажите причину передачи исторической заявки.")
    with transaction.atomic(using=using):
        state = MoySkladSyncState.objects.using(using).select_for_update().filter(
            account_id=config.account_id, environment=config.environment,
        ).first()
        if state is None or state.export_started_at is None:
            return None
        if order.created_at < state.export_started_at and not historical:
            return None
        sync_id = uuid.uuid5(state.namespace, f"{config.account_id}:{order.submission_key}")
        snapshot = _config_snapshot(config, state.namespace)
        snapshot.update(
            consent_revision_id=order.privacy_consent_revision_id,
            consent_revision=(order.privacy_consent_revision.revision_id
                              if order.privacy_consent_revision_id else None),
            consent_approved=(order.privacy_consent_revision_id is not None and
                              order.privacy_consent_revision_id == state.export_consent_revision_id),
        )
        link, created = MoySkladOrderLink.objects.using(using).get_or_create(
            order=order,
            defaults={"account_id": config.account_id, "environment": config.environment,
                      "sync_id": sync_id, "config_snapshot": snapshot},
        )
        if created and historical:
            link.audit_log = [{"action": "historical_export", "actor": actor.pk, "reason": reason, "at": timezone.now().isoformat()}]
            link.save(using=using, update_fields=("audit_log", "updated_at"))
        enqueue_task(kind="export_order", dedupe_key=f"order:{order.pk}", account_id=link.account_id,
                     environment=link.environment, order_link=link, object_id=str(order.pk))
        return link


def _reference(entity_type, identifier):
    path = "customerorder/metadata/states" if entity_type == "state" else entity_type
    return {"meta": {"href": f"{API_BASE}entity/{path}/{uuid_value(identifier)}", "type": entity_type, "mediaType": "application/json"}}


def _description(order):
    label = {"delivery": "Доставка", "pickup": "Самовывоз"}.get(order.fulfillment_method)
    if label is None:
        raise MoySkladError("invalid_fulfillment")
    text = f"Имя: {order.name}\nТелефон: {order.phone}\nEmail: {order.email or 'не указан'}\nПолучение: {label}\nКомментарий:\n{order.comment}"
    if len(text) > 4096 or len(order.delivery_address) > 255:
        raise MoySkladError("order_text_too_long")
    return text


def _minor(value):
    if isinstance(value, (float, bool)):
        raise MoySkladError("invalid_order_price")
    try:
        minor = Decimal(value) * 100
        if not minor.is_finite() or minor <= 0 or minor != minor.to_integral_value():
            raise InvalidOperation
        return int(minor)
    except (InvalidOperation, ValueError, TypeError):
        raise MoySkladError("invalid_order_price") from None


def _prepare_payload(link):
    config = get_config(link.config_snapshot)
    if not all(getattr(config, name) for name in ("account_id", "namespace", "currency_id", "unit_id", "organization_id", "agent_id", "initial_state_id")):
        raise MoySkladError("incomplete_configuration")
    order = link.order
    positions = []
    overrides = link.override_payload.get("positions", {})
    for item in order.items.order_by("pk"):
        override = overrides.get(str(item.pk), {})
        account_id = override.get("account_id", item.external_account_id_snapshot)
        entity_type = override.get("entity_type", item.external_entity_type_snapshot)
        external_id = override.get("external_id", item.external_id_snapshot)
        unit_id = override.get("unit_id", item.unit_id_snapshot)
        if not account_id or not external_id or not unit_id or entity_type != "product":
            raise MoySkladError("incomplete_snapshot")
        if uuid_value(account_id) != config.account_id:
            raise MoySkladError("account_mismatch")
        if uuid_value(unit_id) != config.unit_id:
            raise MoySkladError("invalid_unit")
        weight = item.desired_weight
        if weight <= 0 or weight != weight.to_integral_value():
            raise MoySkladError("invalid_order_quantity")
        positions.append({"assortment": _reference("product", external_id), "quantity": int(weight),
                          "price": _minor(item.price_per_kg_snapshot), "discount": 0, "reserve": 0,
                          "vat": 0, "vatEnabled": False})
    if not positions:
        raise MoySkladError("empty_order")
    return {
        "syncId": str(link.sync_id), "name": order.order_number,
        "externalCode": f"fishsite:{link.environment}:{order.order_number}",
        "moment": order.created_at.astimezone(ZoneInfo("Europe/Moscow")).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "organization": _reference("organization", config.organization_id),
        "agent": _reference("counterparty", config.agent_id),
        "state": _reference("state", config.initial_state_id),
        "rate": {"currency": _reference("currency", config.currency_id)},
        "applicable": False, "vatEnabled": False, "positions": positions,
        "shipmentAddress": order.delivery_address if order.fulfillment_method == "delivery" else "",
        "description": _description(order),
    }


def _owned_link(task):
    """Lock the link then the task; every result mutation rechecks ownership."""
    link = MoySkladOrderLink.objects.select_for_update().select_related("order").get(pk=task.order_link_id)
    current = MoySkladTask.objects.select_for_update().get(pk=task.pk)
    if not ensure_lease(task) or current.lease_token != task.lease_token:
        raise LeaseLost()
    return link


def _save_link(task, **changes):
    with transaction.atomic():
        link = _owned_link(task)
        if changes.get("external_id") is not None and link.override_payload.get("resolution_requested") is True:
            override = dict(link.override_payload)
            override.pop("resolution_requested")
            changes["override_payload"] = override
        for name, value in changes.items():
            setattr(link, name, value)
        link.save(update_fields=(*changes, "updated_at"))
    return link


def _stream_allowed(task, config):
    if config.account_id != task.account_id or config.environment != task.environment:
        return False
    state = MoySkladSyncState.objects.filter(account_id=task.account_id, environment=task.environment).first()
    if state is None or state.namespace != config.namespace or state.access_error_code or state.export_paused:
        return False
    return state.export_enabled if state.export_enabled is not None else config.export_enabled


def _preflight(payload, snapshot, task, client):
    from .catalog_sync import record_preflight_product_error, snapshot_product_mapping

    config = get_config(snapshot)
    seen = set()
    for position in payload["positions"]:
        identifier = reference_id(position["assortment"], "product")
        if identifier in seen:
            continue
        seen.add(identifier)
        if not ensure_lease(task):
            raise LeaseLost()
        mapping = snapshot_product_mapping(identifier, config)
        try:
            data = client.get(f"entity/product/{identifier}")
        except MoySkladError as error:
            if error.status_code == 404:
                record_preflight_product_error(task, mapping, "deleted", config)
            raise
        if not isinstance(data, Mapping) or uuid_value(data.get("id")) != identifier:
            raise MoySkladError("identity_mismatch")
        if uuid_value(data.get("accountId")) != uuid_value(snapshot["account_id"]):
            raise MoySkladError("account_mismatch")
        if reference_id(data, "product") != identifier:
            raise MoySkladError("identity_mismatch")
        if data.get("archived") is not False or data.get("deleted"):
            if data.get("archived") is True or data.get("deleted"):
                record_preflight_product_error(task, mapping,
                    "deleted" if data.get("deleted") else "archived", config, data=data)
            raise MoySkladError("unavailable_product")
        try:
            unit_id = reference_id(data.get("uom"), "uom")
        except MoySkladError:
            unit_id = None
        if unit_id != uuid_value(snapshot["unit_id"]):
            record_preflight_product_error(task, mapping, "invalid_unit", config, data=data)
            raise MoySkladError("invalid_unit")


def _find_sync(link, task, client):
    if not ensure_lease(task):
        raise LeaseLost()
    data = client.get("entity/customerorder", params={
        "filter": f"syncId={link.sync_id};isDeleted=true;isDeleted=false", "limit": 100,
    })
    if not isinstance(data, Mapping) or not isinstance(data.get("rows"), list):
        raise MoySkladError("invalid_order_response")
    rows = data["rows"]
    meta = data.get("meta")
    if not isinstance(meta, Mapping) or "size" not in meta:
        raise MoySkladError("ambiguous_sync_id")
    size = meta["size"]
    if isinstance(size, bool) or not isinstance(size, int) or size != len(rows) or len(rows) > 1:
        raise MoySkladError("ambiguous_sync_id")
    return rows[0] if rows else None


def _positions(document, external_id, task, client):
    value = document.get("positions")
    if isinstance(value, list):
        return value
    if isinstance(value, Mapping) and isinstance(value.get("rows"), list):
        rows = list(value["rows"])
        if value.get("meta", {}).get("size", len(rows)) <= len(rows):
            return rows
    elif isinstance(value, Mapping) and isinstance(value.get("meta"), Mapping):
        rows = []
    else:
        raise MoySkladError("response_mismatch")
    # Never follow a returned href, including pagination links.
    offset = len(rows)
    while True:
        if not ensure_lease(task):
            raise LeaseLost()
        page = client.get(f"entity/customerorder/{external_id}/positions", params={"limit": 1000, "offset": offset})
        if not isinstance(page, Mapping) or not isinstance(page.get("rows"), list):
            raise MoySkladError("response_mismatch")
        batch = page["rows"]
        rows.extend(batch)
        offset += len(batch)
        size = page.get("meta", {}).get("size", offset)
        if offset >= size:
            return rows
        if not batch:
            raise MoySkladError("response_mismatch")


def _verify_document_values(link, document, task, client, *, require_sync=True):
    if not isinstance(document, Mapping):
        raise MoySkladError("response_mismatch")
    if document.get("deleted") or document.get("deletedAt"):
        raise MoySkladError("external_order_deleted")
    if uuid_value(document.get("accountId")) != link.account_id:
        raise MoySkladError("account_mismatch")
    if require_sync and uuid_value(document.get("syncId")) != link.sync_id:
        raise MoySkladError("sync_id_mismatch")
    payload = link.prepared_payload
    if reference_id(document.get("rate", {}).get("currency"), "currency") != uuid_value(link.config_snapshot["currency_id"]):
        raise MoySkladError("response_mismatch")
    expected_sum = sum(row["price"] * row["quantity"] for row in payload["positions"])
    if document.get("sum") != expected_sum or document.get("vatEnabled") is not False:
        raise MoySkladError("response_mismatch")
    rows = _positions(document, link.external_id or uuid_value(document.get("id")), task, client)
    def signature(position):
        if not isinstance(position, Mapping):
            raise MoySkladError("response_mismatch")
        return (str(reference_id(position.get("assortment"), "product")), position.get("quantity"),
                position.get("price"), position.get("reserve"), position.get("discount"),
                position.get("vat"), position.get("vatEnabled"))
    if sorted(map(signature, rows)) != sorted(map(signature, payload["positions"])):
        raise MoySkladError("response_mismatch")


def _verify_document(link, document, task, client, *, require_sync=True):
    try:
        return _verify_document_values(link, document, task, client, require_sync=require_sync)
    except MoySkladError as error:
        if error.code in ("invalid_uuid", "invalid_reference", "invalid_reference_type", "identity_mismatch"):
            raise MoySkladError("response_mismatch") from None
        raise
    except (AttributeError, TypeError, ValueError, InvalidOperation):
        raise MoySkladError("response_mismatch") from None


def _record_document(task, link, document, client, *, require_sync=True):
    if not isinstance(document, Mapping):
        raise MoySkladError("invalid_order_response")
    identifier = uuid_value(document.get("id"))
    if document.get("accountId") is not None and uuid_value(document["accountId"]) != link.account_id:
        raise MoySkladError("account_mismatch")
    if document.get("syncId") is not None and require_sync and uuid_value(document["syncId"]) != link.sync_id:
        raise MoySkladError("sync_id_mismatch")
    if link.external_id is not None and link.external_id != identifier:
        raise MoySkladError("external_identity_mismatch")
    # Preserve the known UUID before checking document details. A mismatch can
    # never lead to creation of a second document or overwrite manager changes.
    try:
        link = _save_link(task, external_id=identifier)
    except IntegrityError:
        raise MoySkladError("external_order_already_linked") from None
    _verify_document(link, document, task, client, require_sync=require_sync)
    with transaction.atomic():
        current = _owned_link(task)
        current.exported_at = current.exported_at or timezone.now()
        current.last_error_code = ""
        current.save(update_fields=("exported_at", "last_error_code", "updated_at"))
        complete_task(task)
    from .incidents import close_recovered_order_incidents

    close_recovered_order_incidents(account_id=link.account_id, environment=link.environment)
    return link


def _fail(task, error):
    if not ensure_lease(task):
        return
    # Never acquire the account/workflow locks while already holding link/task
    # locks. These policies also apply when the transport did not set them.
    if error.status_code in (401, 403):
        ApiThrottle().pause_access()
    elif error.code == "document_limit":
        ApiThrottle().pause_export()
    elif error.status_code == 429:
        ApiThrottle().defer(error.retry_after if error.retry_after is not None else 1.0)
    with transaction.atomic():
        link = _owned_link(task)
        link.last_error_code = error.code
        link.save(update_fields=("last_error_code", "updated_at"))
        target = dict(account_id=link.account_id, environment=link.environment)
        if error.status_code in (401, 403):
            open_incident(scope="access", code="access_denied", **target)
        if error.code == "document_limit":
            open_incident(scope="order_export", code=error.code, **target)
        if error.retryable or error.status_code in (401, 403, 429) or error.code == "document_limit":
            retry_task(task, error.code, retry_after=error.retry_after)
        else:
            MoySkladTask.objects.filter(pk=task.pk, lease_token=task.lease_token).update(
                status="manual_review", lease_token=None, lease_expires_at=None,
                last_error_code=error.code, completed_at=timezone.now(),
            )
            open_incident(scope="order_export", code=error.code, object_ids=[link.order.order_number], **target)


def _resolve_export_attempt(task, link, client):
    """Only a validated empty syncId lookup unlocks an owner's later repair."""
    found = _find_sync(link, task, client)
    if found is not None:
        return _record_document(task, link, found, client)
    with transaction.atomic():
        current = _owned_link(task)
        if current.external_id:
            raise MoySkladError("external_identity_mismatch")
        override = dict(current.override_payload)
        override.pop("resolution_requested", None)
        current.override_payload = override
        now = timezone.now()
        current.attempted = False
        current.last_error_code = "export_absent"
        current.audit_log = [*current.audit_log, {
            "action": "resolve_export_absent", "at": now.isoformat(),
        }]
        current.save(update_fields=("attempted", "override_payload", "last_error_code", "audit_log", "updated_at"))
        MoySkladTask.objects.filter(pk=task.pk, lease_token=task.lease_token).update(
            status="manual_review", lease_token=None, lease_expires_at=None,
            last_error_code="export_absent", completed_at=now,
        )
        return current


def process_export_task(task, *, client):
    """Claimed tasks are processed without HTTP inside database transactions."""
    if task is None or task.kind != "export_order" or not task.order_link_id or not ensure_lease(task):
        return
    try:
        config = get_config()
        link = MoySkladOrderLink.objects.select_related("order").get(pk=task.order_link_id)
        if link.account_id != task.account_id or link.environment != task.environment:
            raise MoySkladError("account_mismatch")
        if config.account_id != link.account_id or config.environment != link.environment:
            raise MoySkladError("target_changed")
        if not _stream_allowed(task, config):
            retry_task(task, "stream_paused")
            return
        if link.config_snapshot.get("namespace") != str(config.namespace):
            raise MoySkladError("namespace_mismatch")
        if link.override_payload.get("resolution_requested") is True:
            return _resolve_export_attempt(task, link, client)
        if link.config_snapshot.get("consent_approved") is not True:
            # Resolve a prior uncertain send before making any new delivery
            # decision. An ordinary retry/override never approves consent.
            if link.attempted and not link.external_id:
                found = _find_sync(link, task, client)
                if found is not None:
                    return _record_document(task, link, found, client)
            if not link.external_id:
                raise MoySkladError("consent_revision_not_approved")
        if not link.prepared_payload:
            payload = _prepare_payload(link)
            _preflight(payload, link.config_snapshot, task, client)
            digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            link = _save_link(task, prepared_payload=payload, payload_hash=digest)
        if link.external_id:
            if not ensure_lease(task):
                return
            try:
                document = client.get(f"entity/customerorder/{link.external_id}")
            except MoySkladError as error:
                if error.status_code == 404:
                    raise MoySkladError("external_order_deleted") from None
                raise
            manually_linked = link.override_payload.get("existing_external_id") == str(link.external_id)
            return _record_document(task, link, document, client, require_sync=not manually_linked)
        candidate = link.override_payload.get("existing_external_id")
        if link.attempted:
            found = _find_sync(link, task, client)
            if found is not None:
                return _record_document(task, link, found, client)
        if candidate:
            # Only after resolving all uncertain syncId attempts may an owner
            # requested existing order be inspected and linked in background.
            if not ensure_lease(task):
                return
            document = client.get(f"entity/customerorder/{uuid_value(candidate)}")
            if not isinstance(document, Mapping):
                raise MoySkladError("invalid_order_response")
            foreign_marker = document.get("externalCode", "")
            own_marker = f"fishsite:{link.environment}:{link.order.order_number}"
            if isinstance(foreign_marker, str) and foreign_marker.startswith("fishsite:") and foreign_marker != own_marker:
                raise MoySkladError("external_order_already_linked")
            if document.get("syncId") and uuid_value(document["syncId"]) != link.sync_id:
                if MoySkladOrderLink.objects.filter(account_id=link.account_id, sync_id=uuid_value(document["syncId"])).exclude(pk=link.pk).exists():
                    raise MoySkladError("external_order_already_linked")
            _verify_document(link, document, task, client, require_sync=False)
            return _record_document(task, link, document, client, require_sync=False)
        link = _save_link(task, attempted=True)
        if not ensure_lease(task):
            return
        try:
            document = client.post("entity/customerorder", json=link.prepared_payload)
        except MoySkladError as error:
            if error.retryable:
                found = _find_sync(link, task, client)
                if found is not None:
                    return _record_document(task, link, found, client)
            raise
        return _record_document(task, link, document, client)
    except LeaseLost:
        return
    except MoySkladError as error:
        try:
            _fail(task, error)
        except LeaseLost:
            return


def _require_repair(actor):
    if not actor or not actor.is_active or not actor.is_staff or not actor.has_perm("integrations.repair_moysklad_order"):
        raise PermissionDenied


def _repair_link(link_id, actor, action, reason):
    _require_repair(actor)
    link = MoySkladOrderLink.objects.select_for_update().get(pk=link_id)
    config = get_config()
    if link.account_id != config.account_id or link.environment != config.environment:
        raise ValidationError("Заявка относится к другой конфигурации обмена.")
    if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 3000:
        raise ValidationError("Укажите корректную причину действия.")
    reason = reason.strip()
    link.audit_log = [*link.audit_log, {"action": action, "actor": actor.pk, "reason": reason, "at": timezone.now().isoformat()}]
    return link


def _reset_export_task(link):
    task = enqueue_task(kind="export_order", dedupe_key=f"order:{link.order_id}", account_id=link.account_id,
                        environment=link.environment, order_link=link, object_id=str(link.order_id))
    task = MoySkladTask.objects.select_for_update().get(pk=task.pk)
    if task.status == "processing" and task.lease_expires_at > timezone.now():
        raise ValidationError("Передача уже выполняется.")
    task.status, task.lease_token, task.lease_expires_at = "pending", None, None
    task.next_attempt_at, task.completed_at = timezone.now(), None
    task.last_error_code = ""
    changed = ["status", "lease_token", "lease_expires_at", "next_attempt_at", "completed_at", "last_error_code", "updated_at"]
    if hasattr(task, "first_error_at"):
        task.first_error_at = None
        changed.append("first_error_at")
    task.save(update_fields=changed)
    return task


def request_order_retry(link_id, *, actor, reason=""):
    with transaction.atomic():
        link = _repair_link(link_id, actor, "retry_export", reason)
        link.save(update_fields=("audit_log", "updated_at"))
        return _reset_export_task(link)


def request_export_resolution(link_id, *, actor, reason):
    """Queue an explicit result lookup without resetting the attempt guard."""
    with transaction.atomic():
        link = _repair_link(link_id, actor, "request_export_resolution", reason)
        if link.external_id:
            raise ValidationError("Результат передачи уже связан с учётным заказом.")
        if not link.attempted:
            raise ValidationError("Передача этой заявки ещё не запускалась.")
        link.override_payload = {**link.override_payload, "resolution_requested": True}
        link.save(update_fields=("override_payload", "audit_log", "updated_at"))
        return _reset_export_task(link)


def request_existing_order_link(link_id, external_id, *, actor, reason=""):
    identifier = uuid_value(external_id)
    with transaction.atomic():
        link = _repair_link(link_id, actor, "link_existing", reason)
        if link.external_id and link.external_id != identifier:
            raise ValidationError("Заявка уже связана с другим заказом.")
        if MoySkladOrderLink.objects.filter(account_id=link.account_id, external_id=identifier).exclude(pk=link.pk).exists():
            raise ValidationError("Учётный заказ уже связан с другой заявкой.")
        link.override_payload = {**link.override_payload, "existing_external_id": str(identifier)}
        link.save(update_fields=("override_payload", "audit_log", "updated_at"))
        return _reset_export_task(link)


def set_export_override(link_id, positions, *, actor, reason):
    """Audit repaired identities separately; accepted OrderItem rows never change."""
    if not isinstance(reason, str) or not reason.strip() or not isinstance(positions, dict):
        raise ValidationError("Укажите причину и исправленные связи позиций.")
    with transaction.atomic():
        link = _repair_link(link_id, actor, "snapshot_override", reason)
        if link.attempted or link.external_id:
            raise ValidationError("Сначала необходимо установить результат предыдущей передачи.")
        item_ids = {str(value) for value in link.order.items.values_list("pk", flat=True)}
        if not set(positions).issubset(item_ids):
            raise ValidationError("Неизвестная позиция заявки.")
        safe = {}
        for key, value in positions.items():
            if not isinstance(value, Mapping) or value.get("entity_type") != "product":
                raise ValidationError("Допустимы только товары МойСклад.")
            if uuid_value(value.get("account_id")) != link.account_id or uuid_value(value.get("unit_id")) != uuid_value(link.config_snapshot["unit_id"]):
                raise ValidationError("Аккаунт или единица товара не соответствуют заявке.")
            safe[key] = {"account_id": str(link.account_id), "entity_type": "product",
                         "external_id": str(uuid_value(value.get("external_id"))), "unit_id": str(uuid_value(value["unit_id"]))}
        link.override_payload = {**link.override_payload, "positions": safe}
        link.override_reason, link.override_by, link.override_at = reason, actor, timezone.now()
        link.prepared_payload, link.payload_hash = {}, ""
        link.save(update_fields=("override_payload", "override_reason", "override_by", "override_at", "prepared_payload", "payload_hash", "audit_log", "updated_at"))
        return _reset_export_task(link)
