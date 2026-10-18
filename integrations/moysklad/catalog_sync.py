"""Owner-controlled catalog mapping and resumable manual catalog passes."""

from urllib.parse import parse_qs, urlsplit
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from django.utils.text import slugify

from catalog.models import Category, Product
from integrations.models import MoySkladProduct, MoySkladSyncRun, MoySkladSyncState, MoySkladTask
from .config import get_config
from .contracts import LeaseLost, MoySkladError
from .mapping import API_BASE, parse_product, reference_id, source_timestamp, uuid_value
from .queue import complete_task, enqueue_task, ensure_lease, retry_task
from .throttling import ApiThrottle


BLOCKING_ERRORS = frozenset(("invalid_unit", "unsupported_type", "archived", "deleted"))


def require_owner(actor, permission):
    if not actor or not actor.is_active or not actor.is_staff or not actor.has_perm(permission):
        raise PermissionDenied("Недостаточно прав для действия МойСклад.")


def stream_enabled(config, stream):
    override = MoySkladSyncState.objects.filter(
        account_id=config.account_id, environment=config.environment,
    ).values_list(f"{stream}_enabled", flat=True).first()
    return getattr(config, f"{stream}_enabled") if override is None else override


def report_source_error(config, code, external_id):
    """The incident service owns notification routing and coalescing."""
    if code == "deferred_product":
        return
    try:
        from .incidents import open_incident
    except ImportError:
        return
    open_incident(scope="catalog", code=code, object_ids=[str(external_id)],
                  account_id=config.account_id, environment=config.environment)


def _external_query(config):
    return MoySkladProduct.objects.filter(account_id=config.account_id, entity_type="product")


def snapshot_product_mapping(external_id, config):
    """Capture the current registry association before an export's HTTP read."""
    return _external_query(config).filter(external_id=external_id, product__isnull=False).values(
        "pk", "external_id", "product_id", "product__mapping_revision",
        "source_updated_at", "metadata_updated_at",
    ).first()


def record_preflight_product_error(task, snapshot, error, config, *, data=None):
    """Block only the unchanged current mapping; never touch a checked price."""
    if snapshot is None:
        return
    stamp = source_timestamp(data.get("updated")) if data is not None else None
    with transaction.atomic():
        product = Product.objects.select_for_update().order_by("pk").get(pk=snapshot["product_id"])
        source = _external_query(config).select_for_update().get(pk=snapshot["pk"])
        MoySkladTask.objects.select_for_update().get(pk=task.pk)
        if not ensure_lease(task):
            raise LeaseLost()
        if (task.account_id != config.account_id or task.environment != config.environment or
                source.external_id != snapshot["external_id"] or source.product_id != product.pk or
                product.mapping_revision != snapshot["product__mapping_revision"] or
                source.source_updated_at != snapshot["source_updated_at"] or
                source.metadata_updated_at != snapshot["metadata_updated_at"] or source.is_deferred):
            return
        if stamp and any(version and stamp < version for version in (
            source.source_updated_at, source.metadata_updated_at,
        )):
            return
        fields = ["last_error_code", "updated_at"]
        source.last_error_code = error
        if data is not None:
            source.metadata_updated_at = stamp
            source.archived = data.get("archived") is True
            try:
                source.unit_id = reference_id(data.get("uom"), "uom")
            except MoySkladError:
                source.unit_id = None
            fields.extend(("metadata_updated_at", "archived", "unit_id"))
        if error == "deleted":
            source.deleted_at = timezone.now()
            fields.append("deleted_at")
        source.save(update_fields=fields)
        Product.objects.filter(pk=product.pk).update(integration_block_reason=error)
        report_source_error(config, error, source.external_id)


def _check_candidate(source, config):
    if source.account_id != config.account_id or source.entity_type != "product":
        raise ValidationError("Товар принадлежит другому аккаунту или имеет неподходящий тип.")
    if source.is_deferred or source.archived or source.deleted_at or source.unit_id != config.unit_id:
        raise ValidationError("Товар нельзя сопоставить: проверьте тип, единицу и архивность.")
    if source.last_error_code or source.last_checked_price is None or source.last_checked_price <= 0:
        raise ValidationError("У товара нет проверенной розничной цены.")


@transaction.atomic
def upsert_product(data, config=None, *, task=None):
    """Refresh source metadata without replacing any editorial product fields."""
    config = get_config(config)
    parsed = parse_product(data, config)
    source, created = _external_query(config).get_or_create(
        external_id=parsed["external_id"], defaults={"account_id": config.account_id},
    )
    product_id = source.product_id
    if product_id:
        Product.objects.select_for_update().get(pk=product_id)
    source = MoySkladProduct.objects.select_for_update().get(pk=source.pk)
    if source.product_id != product_id:
        raise MoySkladError("mapping_conflict", retryable=True)
    if task is not None:
        MoySkladTask.objects.select_for_update().get(pk=task.pk)
        if not ensure_lease(task):
            raise LeaseLost()
    # A delayed catalog response cannot roll source metadata backwards.
    if any(version and parsed["source_updated_at"] < version for version in (
        source.metadata_updated_at, source.source_updated_at,
    )):
        return source
    fields = ("name", "article", "code", "unit_id", "unit_name", "archived", "last_error_code")
    for field in fields:
        setattr(source, field, parsed[field])
    source.is_deferred = parsed["is_deferred"]
    source.deleted_at = None
    source.metadata_updated_at = parsed["source_updated_at"]
    update_fields = [*fields, "is_deferred", "deleted_at", "metadata_updated_at", "updated_at"]
    # Candidate prices are needed to create real cards. A linked record's
    # checked baseline belongs exclusively to successful price processing.
    if not source.product_id and not parsed["last_error_code"]:
        source.source_updated_at = parsed["source_updated_at"]
        source.last_checked_price = parsed["price"]
        source.price_checked_at = timezone.now()
        update_fields.extend(("source_updated_at", "last_checked_price", "price_checked_at"))
    source.save(update_fields=update_fields)
    if product_id and parsed["last_error_code"] in BLOCKING_ERRORS:
        Product.objects.filter(pk=product_id).update(integration_block_reason=parsed["last_error_code"])
        report_source_error(config, parsed["last_error_code"], source.external_id)
    elif not source.last_error_code:
        from .incidents import close_recovered_catalog_incidents

        close_recovered_catalog_incidents(config)
    return source


@transaction.atomic
def link_product(*, product_id, external_product_id, actor):
    config = get_config()
    require_owner(actor, "integrations.link_moysklad_product")
    snapshot = _external_query(config).get(external_id=uuid_value(external_product_id))
    products = {item.pk: item for item in Product.objects.select_for_update().filter(
        pk__in={product_id, snapshot.product_id} - {None}).order_by("pk")}
    product = products[product_id]
    source = _external_query(config).select_for_update().get(pk=snapshot.pk)
    if source.product_id != snapshot.product_id:
        raise MoySkladError("mapping_conflict", retryable=True)
    _check_candidate(source, config)
    if source.product_id is not None or MoySkladProduct.objects.filter(product=product).exists():
        raise ValidationError("Товар уже сопоставлен. Используйте отдельное действие перепривязки.")
    source.product = product
    source.save(update_fields=("product", "updated_at"))
    Product.objects.filter(pk=product.pk).update(mapping_revision=F("mapping_revision") + 1)
    from .price_sync import request_price
    request_price(external_product_id=source.external_id, force_apply=True, config=config)
    return source


@transaction.atomic
def remap_product(*, product_id, external_product_id, actor):
    """Explicitly replace identity; immutable accepted order snapshots are untouched."""
    config = get_config()
    require_owner(actor, "integrations.link_moysklad_product")
    snapshot = _external_query(config).get(external_id=uuid_value(external_product_id))
    products = {item.pk: item for item in Product.objects.select_for_update().filter(
        pk__in={product_id, snapshot.product_id} - {None}).order_by("pk")}
    product = products[product_id]
    records = list(MoySkladProduct.objects.select_for_update().filter(
        Q(product_id=product.pk) | Q(pk=snapshot.pk),
    ).order_by("pk"))
    source = next(item for item in records if item.pk == snapshot.pk)
    if source.product_id != snapshot.product_id:
        raise MoySkladError("mapping_conflict", retryable=True)
    _check_candidate(source, config)
    if source.product_id:
        raise ValidationError("Выбранный внешний товар уже связан с карточкой.")
    for previous in records:
        if previous.pk == source.pk:
            continue
        previous.product = None
        previous.save(update_fields=("product", "updated_at"))
    source.product = product
    source.save(update_fields=("product", "updated_at"))
    Product.objects.filter(pk=product.pk).update(mapping_revision=F("mapping_revision") + 1)
    from .price_sync import request_price
    request_price(external_product_id=source.external_id, force_apply=True, config=config)
    return source


@transaction.atomic
def create_product_from_candidate(*, external_product_id, category_id, actor):
    config = get_config()
    require_owner(actor, "integrations.create_moysklad_candidate")
    if not actor.has_perm("catalog.add_product"):
        raise PermissionDenied
    source = _external_query(config).select_for_update().get(external_id=uuid_value(external_product_id))
    _check_candidate(source, config)
    if source.product_id:
        raise ValidationError("Карточка для товара уже создана.")
    category = Category.objects.get(pk=category_id)
    # UUID suffix also avoids concurrent creation races between equal names.
    base = slugify(source.name)[:150] or "product"
    slug = f"{base}-{source.external_id.hex}"
    suffix = 1
    while Product.objects.filter(slug=slug).exists():
        slug = f"{base}-{source.external_id.hex}-{suffix}"
        suffix += 1
    product = Product.objects.create(
        name=source.name[:180], slug=slug, category=category,
        price_per_kg=source.last_checked_price, price_source=Product.PriceSource.MOYSKLAD,
        mapping_revision=1, is_published=False,
    )
    source.product = product
    source.save(update_fields=("product", "updated_at"))
    from .price_sync import request_price
    request_price(external_product_id=source.external_id, force_apply=True, config=config)
    return product


@transaction.atomic
def unblock_product(*, product_id, actor):
    config = get_config()
    require_owner(actor, "integrations.link_moysklad_product")
    if not actor.has_perm("catalog.change_product"):
        raise PermissionDenied
    product = Product.objects.select_for_update().get(pk=product_id)
    source = _external_query(config).select_for_update().get(product=product)
    _check_candidate(source, config)
    Product.objects.filter(pk=product.pk).update(integration_block_reason="")
    product.integration_block_reason = ""
    return product


@transaction.atomic
def start_catalog_sync(*, actor=None):
    """Queue a manual pass; actor=None is reserved for the trusted CLI entry."""
    config = get_config()
    if actor is not None:
        require_owner(actor, "integrations.sync_moysklad_catalog")
    if not stream_enabled(config, "catalog"):
        raise MoySkladError("catalog_disabled")
    state, _ = MoySkladSyncState.objects.get_or_create(
        account_id=config.account_id, environment=config.environment, defaults={"namespace": config.namespace},
    )
    MoySkladSyncState.objects.select_for_update().get(pk=state.pk)
    existing = MoySkladSyncRun.objects.filter(
        account_id=config.account_id, environment=config.environment, kind="catalog", status="running",
    ).first()
    if existing:
        return existing
    run = MoySkladSyncRun.objects.create(
        account_id=config.account_id, environment=config.environment, kind="catalog", actor=actor,
        cursor={"offset": 0, "seen": [], "phase": "catalog"},
    )
    enqueue_task(kind="catalog_sync", dedupe_key="catalog", account_id=config.account_id,
                 environment=config.environment, sync_run=run, object_id=str(run.pk), force_apply=True)
    return run


def _yield_task(task):
    if not ensure_lease(task):
        raise LeaseLost()
    changed = MoySkladTask.objects.filter(pk=task.pk, lease_token=task.lease_token,
        status="processing", lease_expires_at__gt=timezone.now()).update(
        status="pending", lease_token=None, lease_expires_at=None, next_attempt_at=timezone.now(),
    )
    if changed != 1:
        raise LeaseLost()


def validate_page(response, *, resource, offset, maximum=100):
    """Validate pagination metadata without following any returned URL."""
    if not isinstance(response, dict) or not isinstance(response.get("rows"), list):
        raise MoySkladError("incomplete_catalog")
    rows, meta = response["rows"], response.get("meta")
    if not isinstance(meta, dict):
        raise MoySkladError("incomplete_catalog")
    size, limit = meta.get("size"), meta.get("limit")
    if (isinstance(size, bool) or not isinstance(size, int) or size < 0 or
            meta.get("offset") != offset or isinstance(limit, bool) or not isinstance(limit, int) or
            not 0 < limit <= maximum or len(rows) > limit or offset + len(rows) > size or
            (not rows and offset < size)):
        raise MoySkladError("incomplete_catalog")
    href = meta.get("nextHref")
    more = offset + len(rows) < size
    if more and not href:
        raise MoySkladError("incomplete_catalog")
    if href is not None:
        if not isinstance(href, str):
            raise MoySkladError("invalid_pagination")
        parsed, expected = urlsplit(href), urlsplit(f"{API_BASE}{resource}")
        query = parse_qs(parsed.query, keep_blank_values=True)
        if (parsed.scheme != expected.scheme or parsed.netloc != expected.netloc or
                parsed.path != expected.path or parsed.fragment or
                not set(query).issubset({"offset", "limit", "filter"}) or
                query.get("offset") != [str(offset + len(rows))] or
                query.get("limit") != [str(limit)] or not more):
            raise MoySkladError("invalid_pagination")
    return rows, size


def _fail_catalog_task(task, run, error, config):
    """Persist a terminal manual run and its immediate alert atomically."""
    from .incidents import open_incident

    if not ensure_lease(task):
        raise LeaseLost()
    if error.status_code in (401, 403):
        ApiThrottle(config).pause_access()
    elif error.status_code == 429:
        ApiThrottle(config).defer(error.retry_after if error.retry_after is not None else 1.0)
    with transaction.atomic():
        MoySkladTask.objects.select_for_update().get(pk=task.pk)
        if not ensure_lease(task):
            raise LeaseLost()
        MoySkladSyncRun.objects.filter(pk=run.pk).update(
            total_count=run.total_count, processed_count=run.processed_count, updated_count=run.updated_count,
            skipped_count=run.skipped_count, error_count=run.error_count,
        )
        retry_task(task, error.code, retry_after=error.retry_after)
        access = error.status_code in (401, 403)
        open_incident(scope="access" if access else "catalog",
                      code="access_denied" if access else error.code,
                      object_ids=() if access else [str(run.pk)],
                      account_id=config.account_id, environment=config.environment)
    return "failed"


def process_catalog_task(task, *, client):
    """Process one bounded page; only a complete listing permits deletion probes."""
    from .price_sync import process_catalog_price
    config = get_config()
    if task.kind != "catalog_sync":
        raise MoySkladError("task_kind_mismatch")
    if task.account_id != config.account_id or task.environment != config.environment:
        raise MoySkladError("target_mismatch")
    if not ensure_lease(task):
        raise LeaseLost()
    if not stream_enabled(config, "catalog"):
        retry_task(task, "catalog_disabled")
        return "failed"
    run = MoySkladSyncRun.objects.get(pk=task.sync_run_id)
    cursor = dict(run.cursor)
    try:
        if cursor.get("phase", "catalog") == "catalog":
            offset = cursor.get("offset", 0)
            response = client.get("entity/product", params={"limit": 100, "offset": offset,
                                                           "filter": "archived=true;archived=false"})
            rows, size = validate_page(response, resource="entity/product", offset=offset)
            if "size" in cursor and cursor["size"] != size:
                raise MoySkladError("catalog_changed_during_scan")
            run.total_count = size
            seen = set(cursor.get("seen", []))
            for row in rows:
                if not ensure_lease(task):
                    raise LeaseLost()
                try:
                    identifier = str(uuid_value(row.get("id")))
                    if identifier in seen:
                        raise MoySkladError("duplicate_catalog_item", retryable=True)
                    with transaction.atomic():
                        if not ensure_lease(task):
                            raise LeaseLost()
                        source = upsert_product(row, config=config, task=task)
                    seen.add(identifier)
                    if source.is_deferred:
                        run.skipped_count += 1
                    elif source.product_id:
                        result = process_catalog_price(task, source=source, client=client)
                        if result == "updated":
                            run.updated_count += 1
                        elif result in ("stale", "invalid"):
                            run.error_count += 1
                        else:
                            run.skipped_count += 1
                    else:
                        run.skipped_count += 1
                except LeaseLost:
                    raise
                except MoySkladError as exc:
                    if exc.retryable or exc.status_code in (401, 403, 429):
                        raise
                    run.error_count += 1
                run.processed_count += 1
            cursor.update(offset=offset + len(rows), seen=sorted(seen), size=size)
            run.total_count = size
            if cursor["offset"] == size:
                # A separate UUID read must confirm absence from the full list.
                if len(seen) != size:
                    # Invalid/duplicate identities mean the listing is incomplete.
                    cursor.update(phase="missing", missing=[], missing_cursor=0)
                    run.error_count += 1
                else:
                    cursor.update(phase="missing", missing=list(_external_query(config).exclude(
                        external_id__in=seen).values_list("pk", flat=True)), missing_cursor=0)
                    run.total_count += len(cursor["missing"])
        else:
            missing = cursor.get("missing", [])
            position = cursor.get("missing_cursor", 0)
            for pk in missing[position:position + 20]:
                source = _external_query(config).get(pk=pk)
                try:
                    if source.product_id and not source.is_deferred:
                        result = process_catalog_price(task, source=source, client=client)
                        if result == "updated":
                            run.updated_count += 1
                        elif result in ("stale", "invalid"):
                            run.error_count += 1
                        else:
                            run.skipped_count += 1
                    else:
                        payload = client.get(f"entity/product/{source.external_id}")
                        with transaction.atomic():
                            if not ensure_lease(task):
                                raise LeaseLost()
                            upsert_product(payload, config=config, task=task)
                        run.skipped_count += 1
                except MoySkladError as exc:
                    if exc.status_code != 404:
                        raise
                    with transaction.atomic():
                        if source.product_id:
                            Product.objects.select_for_update().get(pk=source.product_id)
                        locked = MoySkladProduct.objects.select_for_update().get(pk=pk)
                        MoySkladTask.objects.select_for_update().get(pk=task.pk)
                        if not ensure_lease(task):
                            raise LeaseLost()
                        locked.deleted_at = timezone.now()
                        locked.last_error_code = "deleted"
                        locked.save(update_fields=("deleted_at", "last_error_code", "updated_at"))
                        if locked.product_id and not locked.is_deferred:
                            Product.objects.filter(pk=locked.product_id).update(integration_block_reason="deleted")
                            report_source_error(config, "deleted", locked.external_id)
                    run.error_count += 1
                run.processed_count += 1
                cursor["missing_cursor"] = cursor.get("missing_cursor", 0) + 1
        with transaction.atomic():
            if not ensure_lease(task):
                raise LeaseLost()
            run.cursor = cursor
            finished = cursor.get("phase") == "missing" and cursor.get("missing_cursor", 0) == len(cursor.get("missing", []))
            if finished:
                run.status = "partial" if run.error_count else "success"
                run.completed_at = timezone.now()
                complete_task(task)
            else:
                _yield_task(task)
            run.save()
            if finished and run.status == "success":
                from .incidents import close_recovered_catalog_incidents

                close_recovered_catalog_incidents(config, manual_success=True)
        return run.status if finished else "pending"
    except LeaseLost:
        raise
    except MoySkladError as exc:
        return _fail_catalog_task(task, run, exc, config)
    except (TimeoutError, OSError):
        return _fail_catalog_task(task, run, MoySkladError("network_error", retryable=True), config)
