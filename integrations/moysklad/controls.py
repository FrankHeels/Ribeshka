"""Authorized, local-only technical controls for the exchange streams."""

from dataclasses import replace

from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from content.consent_revisions import get_current_consent_revision
from integrations.models import MoySkladSyncState
from .catalog_sync import require_owner
from .config import get_config
from .contracts import MoySkladError
from .queue import enqueue_task


STREAMS = ("catalog", "prices", "export", "statuses")


def require_technical_admin(actor):
    require_owner(actor, "integrations.manage_moysklad")
    if not actor.is_superuser:
        raise PermissionDenied("Управлять подключением может только технический администратор.")


def locked_state(config):
    """The caller holds an atomic block; never retarget an existing namespace."""
    if not config.account_id or not config.namespace:
        raise MoySkladError("invalid_configuration")
    state, _ = MoySkladSyncState.objects.get_or_create(
        account_id=config.account_id, environment=config.environment,
        defaults={"namespace": config.namespace},
    )
    state = MoySkladSyncState.objects.select_for_update().get(pk=state.pk)
    if state.namespace != config.namespace:
        raise MoySkladError("target_changed")
    return state


def effective_config(config, state, **overrides):
    flags = {
        f"{stream}_enabled": getattr(state, f"{stream}_enabled")
        for stream in STREAMS if getattr(state, f"{stream}_enabled") is not None
    }
    return get_config(replace(config, **(flags | overrides)))


def _audit(actor, state, action):
    LogEntry.objects.create(
        user_id=actor.pk, content_type=ContentType.objects.get_for_model(state),
        object_id=str(state.pk), object_repr=f"МойСклад {state.account_id} {state.environment}",
        action_flag=CHANGE, change_message=action,
    )


def command_actor(username, *, required=False):
    """Resolve an explicit operator; only enqueue-only CLI commands allow omission."""
    if not username:
        if required:
            raise MoySkladError("owner_required")
        return None
    model = get_user_model()
    actor = model.objects.filter(**{model.USERNAME_FIELD: username}).first()
    if actor is None:
        raise MoySkladError("owner_not_found")
    return actor


@transaction.atomic
def set_stream_enabled(stream, enabled, *, actor):
    require_technical_admin(actor)
    if stream not in STREAMS or not isinstance(enabled, bool):
        raise MoySkladError("invalid_stream")
    config = get_config()
    state = locked_state(config)
    field = f"{stream}_enabled"
    was_enabled = getattr(state, field)
    if was_enabled is None:
        was_enabled = getattr(config, field)
    enable_prices = stream == "prices" and enabled and (not was_enabled or state.prices_enabled_at is None)
    if enabled:
        effective_config(config, state, **{field: True})
    setattr(state, field, enabled)
    if enable_prices:
        state.prices_enabled_at = timezone.now()
    state.save(update_fields=(field, "prices_enabled_at", "updated_at"))
    if enable_prices:
        from .price_sync import start_price_reconciliation

        run = start_price_reconciliation(reason="recovery")
        if run.actor_id is None and run.reason == "recovery":
            run.actor = actor
            run.save(update_fields=("actor",))
    _audit(actor, state, f"{stream}_enabled={str(enabled).lower()}")
    state.refresh_from_db()
    return state


@transaction.atomic
def start_export(*, actor):
    """Only an explicit action starts registration; historical orders stay local."""
    require_technical_admin(actor)
    config = get_config()
    state = locked_state(config)
    effective_config(config, state, export_enabled=True)
    if state.export_started_at is None:
        state.export_started_at = timezone.now()
    state.export_consent_revision = get_current_consent_revision()
    state.export_enabled = True
    state.save(update_fields=("export_started_at", "export_consent_revision", "export_enabled", "updated_at"))
    _audit(actor, state, "export_started")
    return state


@transaction.atomic
def request_access_check(*, actor):
    """The technical administrator only queues a read-only probe for the worker."""
    require_technical_admin(actor)
    config = get_config()
    state = locked_state(config)
    if not config.api_token:
        raise MoySkladError("missing_api_token")
    task = enqueue_task(
        kind="check_access", dedupe_key="access", account_id=config.account_id,
        environment=config.environment,
    )
    _audit(actor, state, f"access_check:{task.pk}")
    return task
