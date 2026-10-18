"""Fast durable price signals, bounded inbox processing and owned registration."""

from datetime import timedelta
import hashlib
import hmac
import json

from django.core.exceptions import RequestDataTooBig
from django.db import connection, transaction
from django.http import HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from catalog.models import Product
from integrations.models import MoySkladProduct, MoySkladSyncState, MoySkladTask, MoySkladWebhookInbox
from .catalog_sync import _yield_task
from .config import get_config
from .contracts import LeaseLost, MoySkladError
from .mapping import reference_id, uuid_value
from .queue import complete_task, enqueue_task, ensure_lease


MAX_BODY_BYTES = 1024 * 1024
# Hints are diagnostic only. Unknown fields still cause a fresh product read.
FIELD_HINTS = frozenset(("salePrices", "name", "code", "article", "uom", "archived", "updated",
                        "description", "buyPrice", "minPrice", "barcodes", "attributes", "images",
                        "supplier", "productFolder", "weight", "volume", "vat", "vatEnabled"))


def normalize_events(payload, config):
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        raise MoySkladError("invalid_webhook")
    events = []
    for event in payload["events"]:
        if not isinstance(event, dict) or uuid_value(event.get("accountId")) != config.account_id:
            raise MoySkladError("account_mismatch")
        if event.get("action") != "UPDATE":
            raise MoySkladError("invalid_webhook_action")
        identifier = reference_id(event, "product")
        fields = event.get("updatedFields", [])
        if not isinstance(fields, list) or any(not isinstance(field, str) for field in fields):
            raise MoySkladError("invalid_webhook_fields")
        events.append({"external_id": str(identifier),
                       "updated_fields": sorted(set(fields).intersection(FIELD_HINTS))})
    return events


@transaction.atomic
def receive_webhook(*, request_id, events, config=None):
    """Return after a committed inbox/task pair, including duplicate deliveries."""
    config = get_config(config)
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '200ms'")
            cursor.execute("SET LOCAL statement_timeout = '1000ms'")
    canonical = json.dumps(events, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    payload_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    inbox, created = MoySkladWebhookInbox.objects.get_or_create(
        account_id=config.account_id, environment=config.environment, request_id=uuid_value(request_id),
        defaults={"payload_hash": payload_hash, "events": events},
    )
    if not created and not hmac.compare_digest(inbox.payload_hash, payload_hash):
        from .incidents import open_incident
        open_incident(scope="webhook", code="webhook_request_conflict",
                      object_ids=[str(inbox.pk)], account_id=config.account_id,
                      environment=config.environment)
        return "conflict"
    enqueue_task(kind="webhook", dedupe_key=f"inbox:{inbox.pk}", account_id=config.account_id,
                 environment=config.environment, inbox=inbox, object_id=str(inbox.pk))
    state, _ = MoySkladSyncState.objects.get_or_create(account_id=config.account_id,
        environment=config.environment, defaults={"namespace": config.namespace})
    if state.namespace != config.namespace:
        raise MoySkladError("namespace_mismatch")
    MoySkladSyncState.objects.filter(pk=state.pk).update(webhook_received_at=timezone.now())
    return "accepted"


@transaction.non_atomic_requests
@csrf_exempt
@require_POST
def price_webhook(request, secret):
    config = get_config()
    if not config.webhook_secret or not hmac.compare_digest(
        secret.encode("utf-8"), config.webhook_secret.encode("ascii"),
    ):
        return HttpResponse(status=404)
    try:
        if request.content_type != "application/json":
            return HttpResponse(status=415)
        declared = request.META.get("CONTENT_LENGTH", "")
        if declared and int(declared) > MAX_BODY_BYTES:
            return HttpResponse(status=413)
        body = request.body
        if len(body) > MAX_BODY_BYTES:
            return HttpResponse(status=413)
        if len(request.GET.getlist("requestId")) != 1:
            raise MoySkladError("invalid_request_id")
        request_id = uuid_value(request.GET.get("requestId"))
        payload = json.loads(body.decode("utf-8"))
        events = normalize_events(payload, config)
    except RequestDataTooBig:
        return HttpResponse(status=413)
    except (MoySkladError, ValueError, TypeError, UnicodeDecodeError):
        return HttpResponse(status=400)
    try:
        result = receive_webhook(request_id=request_id, events=events, config=config)
    except Exception:
        # Acknowledging storage failure would permanently lose a source signal.
        # Do not log request data or exception text containing the secret URL.
        return HttpResponse(status=503)
    return HttpResponse(status=409 if result == "conflict" else 204)


def process_inbox(task, *, batch_size=100):
    from .price_sync import request_price
    config = get_config()
    if task.kind != "webhook":
        raise MoySkladError("task_kind_mismatch")
    if task.account_id != config.account_id or task.environment != config.environment:
        raise MoySkladError("target_mismatch")
    if not ensure_lease(task):
        raise LeaseLost()
    inbox_id = task.inbox_id or task.object_id
    # All product locks precede source locks and task/inbox locks.
    snapshot = MoySkladWebhookInbox.objects.get(pk=inbox_id)
    if snapshot.account_id != config.account_id or snapshot.environment != config.environment:
        raise MoySkladError("target_mismatch")
    batch = snapshot.events[snapshot.cursor:snapshot.cursor + batch_size]
    identifiers = [uuid_value(event["external_id"]) for event in batch]
    with transaction.atomic():
        list(Product.objects.select_for_update().filter(moysklad_product__account_id=config.account_id,
            moysklad_product__external_id__in=identifiers).order_by("pk"))
        list(MoySkladProduct.objects.select_for_update().filter(account_id=config.account_id,
            entity_type="product", external_id__in=identifiers).order_by("pk"))
        MoySkladTask.objects.select_for_update().get(pk=task.pk)
        if not ensure_lease(task):
            raise LeaseLost()
        inbox = MoySkladWebhookInbox.objects.select_for_update().get(pk=inbox_id)
        if inbox.cursor != snapshot.cursor:
            raise LeaseLost()
        for identifier in identifiers:
            if MoySkladProduct.objects.filter(account_id=config.account_id, entity_type="product",
                external_id=identifier, product__isnull=False, is_deferred=False).exists():
                request_price(external_product_id=identifier, config=config)
            inbox.cursor += 1
        if inbox.cursor == len(inbox.events):
            inbox.processed_at = timezone.now()
            complete_task(task)
            MoySkladSyncState.objects.filter(account_id=config.account_id,
                environment=config.environment).update(webhook_processed_at=inbox.processed_at)
        else:
            _yield_task(task)
        inbox.save(update_fields=("cursor", "processed_at", "updated_at"))
    return "done" if inbox.processed_at else "pending"


def cleanup_processed_inbox(*, now=None):
    """Keep all unfinished work and thirty days of processed delivery identities."""
    config = get_config()
    cutoff = (now or timezone.now()) - timedelta(days=30)
    with transaction.atomic():
        inboxes = MoySkladWebhookInbox.objects.filter(account_id=config.account_id,
            environment=config.environment, processed_at__lt=cutoff).exclude(
            tasks__status__in=("pending", "processing"))
        identifiers = list(inboxes.values_list("pk", flat=True))
        MoySkladTask.objects.filter(inbox_id__in=identifiers, status__in=("done", "failed", "manual_review")).delete()
        return MoySkladWebhookInbox.objects.filter(pk__in=identifiers).delete()[0]


def configure_webhook(*, client):
    """Update only the saved registration or an exact receiver URL match."""
    config = get_config()
    if not config.webhook_url or len(config.webhook_url) > 255:
        raise MoySkladError("invalid_webhook_url")
    state, _ = MoySkladSyncState.objects.get_or_create(account_id=config.account_id,
        environment=config.environment, defaults={"namespace": config.namespace})
    payload = {"url": config.webhook_url, "entityType": "product", "action": "UPDATE", "diffType": "FIELDS"}
    identifier = state.webhook_id
    if identifier is None:
        offset = 0
        found = []
        while True:
            response = client.get("entity/webhook", params={"limit": 100, "offset": offset})
            if not isinstance(response, dict) or not isinstance(response.get("rows"), list):
                raise MoySkladError("invalid_webhook_response")
            for row in response["rows"]:
                if isinstance(row, dict) and row.get("url") == config.webhook_url:
                    if row.get("accountId") is not None and uuid_value(row["accountId"]) != config.account_id:
                        raise MoySkladError("account_mismatch")
                    found.append((uuid_value(row.get("id")), row))
            meta = response.get("meta", {})
            size = meta.get("size")
            offset += len(response["rows"])
            if size is None or offset >= size:
                break
            if not response["rows"]:
                raise MoySkladError("invalid_webhook_response")
        if len(found) > 1:
            raise MoySkladError("duplicate_owned_webhook")
        identifier = found[0][0] if found else None
        if found and all(found[0][1].get(key) == value for key, value in payload.items()):
            MoySkladSyncState.objects.filter(pk=state.pk).update(webhook_id=identifier)
            return identifier
    if identifier:
        try:
            response = client.put(f"entity/webhook/{identifier}", json=payload)
            # The fixed resource ID already identifies this owned registration.
            # Only creation requires extracting a newly allocated response UUID.
            if isinstance(response, dict):
                if response.get("id") is not None and uuid_value(response["id"]) != identifier:
                    raise MoySkladError("identity_mismatch")
                if response.get("accountId") is not None and uuid_value(response["accountId"]) != config.account_id:
                    raise MoySkladError("account_mismatch")
            MoySkladSyncState.objects.filter(pk=state.pk).update(webhook_id=identifier)
            return identifier
        except MoySkladError as exc:
            if exc.status_code != 404:
                raise
            response = client.post("entity/webhook", json=payload)
            identifier = None
    else:
        response = client.post("entity/webhook", json=payload)
    if not isinstance(response, dict):
        raise MoySkladError("invalid_webhook_response")
    response_id = uuid_value(response.get("id"))
    if identifier is not None and response_id != identifier:
        raise MoySkladError("identity_mismatch")
    if response.get("accountId") is not None and uuid_value(response["accountId"]) != config.account_id:
        raise MoySkladError("account_mismatch")
    MoySkladSyncState.objects.filter(pk=state.pk).update(webhook_id=response_id)
    return response_id
