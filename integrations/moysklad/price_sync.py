"""Revision-fenced prices shared by webhook, catalog and recovery work."""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from catalog.models import Product
from catalog.pricing import change_product_price
from integrations.models import MoySkladProduct, MoySkladSyncRun, MoySkladSyncState, MoySkladTask
from .catalog_sync import BLOCKING_ERRORS, _yield_task, report_source_error, require_owner, stream_enabled
from .config import get_config
from .contracts import LeaseLost, MoySkladError
from .mapping import parse_product, uuid_value
from .queue import complete_task, enqueue_task, ensure_lease, retry_task
from .throttling import ApiThrottle


@transaction.atomic
def request_price(*, external_product_id, force_apply=False, config=None):
    """Advance the durable request and coalesce active work in one transaction."""
    config = get_config(config)
    identity = {"account_id": config.account_id, "entity_type": "product",
                "external_id": uuid_value(external_product_id)}
    product_id = MoySkladProduct.objects.values_list("product_id", flat=True).get(**identity)
    if product_id:
        Product.objects.select_for_update().get(pk=product_id)
    source = MoySkladProduct.objects.select_for_update().get(**identity)
    if source.product_id != product_id:
        raise MoySkladError("mapping_conflict", retryable=True)
    if not source.product_id:
        raise MoySkladError("unlinked_product")
    if source.price_requested_revision > source.price_processed_revision:
        force_apply = force_apply or source.tasks.filter(kind="price", force_apply=True).exists()
    source.price_requested_revision += 1
    if source.price_pending_since is None:
        source.price_pending_since = timezone.now()
    source.save(update_fields=("price_requested_revision", "price_pending_since", "updated_at"))
    return enqueue_task(
        kind="price", dedupe_key=f"price:{source.external_id}", account_id=config.account_id,
        environment=config.environment, object_id=str(source.external_id), external_product=source,
        force_apply=force_apply,
    )


def _check_task(task, config):
    if task.account_id != config.account_id or task.environment != config.environment:
        raise MoySkladError("target_mismatch")
    if not ensure_lease(task):
        raise LeaseLost()


def _snapshot(source):
    source.refresh_from_db()
    if not source.product_id:
        return None
    product = Product.objects.get(pk=source.product_id)
    return {
        "source_id": source.pk, "external_id": source.external_id, "product_id": product.pk,
        "price_revision": product.price_revision, "mapping_revision": product.mapping_revision,
        "request_revision": source.price_requested_revision, "source_updated_at": source.source_updated_at,
        "metadata_updated_at": source.metadata_updated_at,
    }


def _locked_read(task, snapshot, config, stream):
    product = Product.objects.select_for_update().get(pk=snapshot["product_id"])
    source = MoySkladProduct.objects.select_for_update().get(pk=snapshot["source_id"])
    owned = MoySkladTask.objects.select_for_update().get(pk=task.pk)
    _check_task(task, config)
    if not stream_enabled(config, stream):
        raise MoySkladError(f"{stream}_disabled", retryable=True)
    stale = (
        source.account_id != config.account_id or source.entity_type != "product" or
        source.external_id != snapshot["external_id"] or source.product_id != product.pk or
        product.price_revision != snapshot["price_revision"] or
        product.mapping_revision != snapshot["mapping_revision"] or
        source.price_requested_revision != snapshot["request_revision"] or
        source.source_updated_at != snapshot["source_updated_at"] or
        source.metadata_updated_at != snapshot["metadata_updated_at"]
    )
    return product, source, owned, stale


def _apply_response(task, snapshot, data, config, *, stream, force_apply, finish_task):
    parsed = parse_product(data, config, expected_id=snapshot["external_id"])
    with transaction.atomic():
        product, source, owned, stale = _locked_read(task, snapshot, config, stream)
        if stale:
            if finish_task:
                _yield_task(task)
            return "stale"
        if any(version and parsed["source_updated_at"] < version for version in (
            source.metadata_updated_at, source.source_updated_at,
        )):
            if finish_task:
                retry_task(task, "older_source_version")
            return "stale"
        error = parsed["last_error_code"]
        # Deferred identities remain excluded even if their accounting code changes.
        deferred = parsed["is_deferred"]
        if deferred:
            error = "deferred_product"
        for name in ("name", "article", "code", "unit_id", "unit_name", "archived"):
            setattr(source, name, parsed[name])
        source.is_deferred = deferred
        source.deleted_at = None
        source.last_error_code = error
        source.metadata_updated_at = parsed["source_updated_at"]
        metadata_fields = ("name", "article", "code", "unit_id", "unit_name", "archived",
                           "is_deferred", "deleted_at", "last_error_code", "metadata_updated_at", "updated_at")
        if error:
            source.save(update_fields=metadata_fields)
            if error in BLOCKING_ERRORS:
                Product.objects.filter(pk=product.pk).update(integration_block_reason=error)
            report_source_error(config, error, source.external_id)
            if finish_task:
                MoySkladTask.objects.filter(pk=owned.pk).update(
                    status="manual_review", lease_token=None, lease_expires_at=None,
                    last_error_code=error, completed_at=timezone.now(),
                )
            return "invalid"
        should_apply = force_apply or parsed["price"] != source.last_checked_price
        changed = False
        if should_apply:
            _changed_product, changed = change_product_price(
                product_id=product.pk, price=parsed["price"], source=Product.PriceSource.MOYSKLAD,
                expected_revision=snapshot["price_revision"],
            )
        source.last_checked_price = parsed["price"]
        source.price_checked_at = timezone.now()
        source.source_updated_at = parsed["source_updated_at"]
        source.price_processed_revision = snapshot["request_revision"]
        source.price_pending_since = None
        source.save(update_fields=(*metadata_fields, "last_checked_price", "price_checked_at",
                                  "source_updated_at", "price_processed_revision", "price_pending_since"))
        if finish_task:
            complete_task(task)
        from .incidents import close_recovered_catalog_incidents

        close_recovered_catalog_incidents(config)
        return "updated" if changed else "unchanged"


def _record_read_error(task, snapshot, exc, config, *, stream, finish_task):
    _check_task(task, config)
    # Account policy is recorded before taking product/task locks, matching the
    # transport's lock order even when a caller supplies a test/fake client.
    if exc.status_code in (401, 403):
        ApiThrottle(config).pause_access()
        from .incidents import open_incident

        open_incident(scope="access", code="access_denied", account_id=config.account_id,
                      environment=config.environment)
    elif exc.status_code == 429:
        ApiThrottle(config).defer(exc.retry_after if exc.retry_after is not None else 1.0)
    temporary = exc.retryable or exc.status_code in (401, 403, 429)
    with transaction.atomic():
        product, source, owned, stale = _locked_read(task, snapshot, config, stream)
        if stale:
            if finish_task:
                _yield_task(task)
            return "stale"
        code = "deleted" if exc.status_code == 404 else exc.code
        source.last_error_code = code
        if code == "deleted" and not source.is_deferred:
            source.deleted_at = timezone.now()
            Product.objects.filter(pk=product.pk).update(integration_block_reason=code)
        source.save(update_fields=("last_error_code", "deleted_at", "updated_at"))
        if not temporary and not source.is_deferred:
            report_source_error(config, code, source.external_id)
        if finish_task:
            if temporary:
                retry_task(task, code, retry_after=exc.retry_after)
            else:
                MoySkladTask.objects.filter(pk=owned.pk).update(
                    status="manual_review", lease_token=None, lease_expires_at=None,
                    last_error_code=code, completed_at=timezone.now(),
                )
        return "pending" if temporary else "invalid"


def process_price_task(task, *, client):
    config = get_config()
    if task.kind != "price":
        raise MoySkladError("task_kind_mismatch")
    _check_task(task, config)
    if not stream_enabled(config, "prices"):
        retry_task(task, "prices_disabled")
        return "pending"
    source = MoySkladProduct.objects.get(account_id=config.account_id, entity_type="product",
                                        external_id=uuid_value(task.object_id))
    if task.external_product_id is not None and task.external_product_id != source.pk:
        raise MoySkladError("task_object_mismatch")
    snapshot = _snapshot(source)
    if snapshot is None:
        with transaction.atomic():
            _check_task(task, config)
            complete_task(task)
        return "unlinked"
    force_apply = MoySkladTask.objects.get(pk=task.pk).force_apply
    try:
        data = client.get(f"entity/product/{snapshot['external_id']}")
        return _apply_response(task, snapshot, data, config, stream="prices",
                               force_apply=force_apply, finish_task=True)
    except LeaseLost:
        raise
    except MoySkladError as exc:
        if exc.code == "prices_disabled":
            retry_task(task, exc.code)
            return "pending"
        return _record_read_error(task, snapshot, exc, config, stream="prices", finish_task=True)
    except (TimeoutError, OSError):
        return _record_read_error(task, snapshot, MoySkladError("network_error", retryable=True),
                                  config, stream="prices", finish_task=True)


def process_catalog_price(task, *, source, client):
    """Read and force a single price under the enclosing manual run's lease."""
    config = get_config()
    _check_task(task, config)
    snapshot = _snapshot(source)
    if snapshot is None:
        return "unlinked"
    try:
        data = client.get(f"entity/product/{snapshot['external_id']}")
        return _apply_response(task, snapshot, data, config, stream="catalog",
                               force_apply=True, finish_task=False)
    except LeaseLost:
        raise
    except MoySkladError as exc:
        if exc.retryable or exc.status_code in (401, 403, 429):
            raise
        return _record_read_error(task, snapshot, exc, config, stream="catalog", finish_task=False)


@transaction.atomic
def start_price_reconciliation(*, reason="manual", actor=None):
    """Coalesce recovery/daily/manual passes; actor=None is a trusted worker/CLI."""
    config = get_config()
    if reason not in MoySkladSyncRun.Reason.values:
        raise ValueError("Unknown reconciliation reason.")
    if actor is not None:
        require_owner(actor, "integrations.reconcile_moysklad_prices")
    if not stream_enabled(config, "prices"):
        raise MoySkladError("prices_disabled")
    state, _ = MoySkladSyncState.objects.get_or_create(account_id=config.account_id,
        environment=config.environment, defaults={"namespace": config.namespace})
    state = MoySkladSyncState.objects.select_for_update().get(pk=state.pk)
    existing = MoySkladSyncRun.objects.filter(account_id=config.account_id,
        environment=config.environment, kind="prices", status="running").first()
    if existing:
        return existing
    targets = [str(identifier) for identifier in MoySkladProduct.objects.filter(
        account_id=config.account_id, entity_type="product", product__isnull=False,
        is_deferred=False,
    ).order_by("pk").values_list("external_id", flat=True)]
    run = MoySkladSyncRun.objects.create(account_id=config.account_id,
        environment=config.environment, kind="prices", reason=reason, actor=actor,
        total_count=len(targets), cursor={"targets": targets, "offset": 0, "revisions": {}},
    )
    enqueue_task(kind="price_reconciliation", dedupe_key="prices", account_id=config.account_id,
        environment=config.environment, sync_run=run, object_id=str(run.pk), force_apply=True)
    state.prices_next_at = timezone.now() + timedelta(hours=24)
    state.save(update_fields=("prices_next_at", "updated_at"))
    return run


def process_price_reconciliation(task, *, client=None):
    """Persist child request revisions, then observe their fenced resolution."""
    config = get_config()
    if task.kind != "price_reconciliation":
        raise MoySkladError("task_kind_mismatch")
    _check_task(task, config)
    if not stream_enabled(config, "prices"):
        retry_task(task, "prices_disabled")
        return "pending"
    with transaction.atomic():
        run = MoySkladSyncRun.objects.select_for_update().get(pk=task.sync_run_id)
        _check_task(task, config)
        cursor = dict(run.cursor)
        revisions = dict(cursor.get("revisions", {}))
        targets = cursor.get("targets", [])
        offset = cursor.get("offset", 0)
        # Lock products in ID order before any external records.
        batch = targets[offset:offset + 50]
        list(Product.objects.select_for_update().filter(
            moysklad_product__account_id=config.account_id,
            moysklad_product__external_id__in=batch).order_by("pk"))
        for identifier in batch:
            source = MoySkladProduct.objects.filter(account_id=config.account_id,
                entity_type="product", external_id=identifier, product__isnull=False).first()
            if source is None:
                revisions[identifier] = None
                continue
            request_price(external_product_id=identifier, force_apply=True, config=config)
            source.refresh_from_db()
            revisions[identifier] = source.price_requested_revision
        cursor.update(offset=offset + len(batch), revisions=revisions)
        resolved = errors = 0
        for identifier, revision in revisions.items():
            source = MoySkladProduct.objects.filter(account_id=config.account_id,
                entity_type="product", external_id=identifier, product__isnull=False).first()
            if revision is None or source is None:
                errors += 1
            elif source.price_processed_revision >= revision and not source.last_error_code:
                resolved += 1
            elif source.last_error_code and not source.tasks.filter(kind="price", status__in=("pending", "processing")).exists():
                errors += 1
        run.cursor = cursor
        run.processed_count = resolved + errors
        run.updated_count = resolved
        run.error_count = errors
        finished = cursor["offset"] == len(targets) and run.processed_count == len(targets)
        if finished:
            run.status = "partial" if errors else "success"
            run.completed_at = timezone.now()
            complete_task(task)
            if not errors:
                MoySkladSyncState.objects.filter(account_id=config.account_id,
                    environment=config.environment).update(prices_reconciled_at=run.completed_at)
        else:
            _yield_task(task)
            # Give queued short price work priority over polling the same run.
            MoySkladTask.objects.filter(pk=task.pk, status="pending").update(
                next_attempt_at=timezone.now() + timedelta(seconds=1))
        run.save()
        return run.status if finished else "pending"
