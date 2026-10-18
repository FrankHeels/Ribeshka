"""Database-backed request slots shared by workers for one API account."""

from datetime import timedelta
import math
import time
import uuid

from django.db import transaction
from django.utils import timezone

from .config import get_config
from .contracts import LeaseLost, MoySkladError


API_INTERVAL_SECONDS = 0.5
# The lease includes the complete 60-second request deadline and cleanup margin.
API_LEASE_SECONDS = 70


class ApiThrottle:
    def __init__(self, config=None, *, now=timezone.now, sleep=time.sleep):
        self.config = get_config(config)
        self.now = now
        self.sleep = sleep

    def _states(self):
        from .models import MoySkladApiState

        if not self.config.account_id or not self.config.namespace:
            raise MoySkladError("invalid_configuration")
        return MoySkladApiState.objects.filter(account_id=self.config.account_id)

    def _workflow_states(self):
        from .models import MoySkladSyncState

        return MoySkladSyncState.objects.filter(account_id=self.config.account_id)

    def _locked_state(self):
        state, _ = self._states().get_or_create(account_id=self.config.account_id)
        # Lock explicitly after get_or_create, including its unique-conflict
        # recovery path, so first requests in two processes cannot both own it.
        state = self._states().select_for_update().get(pk=state.pk)
        workflow, _ = self._workflow_states().get_or_create(
            account_id=self.config.account_id, environment=self.config.environment,
            defaults={
                "namespace": self.config.namespace,
                "access_error_code": state.access_error_code,
                "export_paused": state.export_paused,
            },
        )
        if workflow.namespace != self.config.namespace:
            raise MoySkladError("target_changed")
        return state

    def _workflow_heartbeat(self, now):
        self._workflow_states().filter(
            environment=self.config.environment, namespace=self.config.namespace,
        ).update(heartbeat_at=now, updated_at=now)

    def acquire(self, *, access_probe=False):
        """Reserve one slot, sleeping at most half a second outside the lock."""
        if not isinstance(access_probe, bool):
            raise MoySkladError("invalid_configuration")
        with transaction.atomic():
            state = self._locked_state()
            now = self.now()
            if state.access_error_code and not access_probe:
                raise MoySkladError("access_denied")
            if state.api_paused_until and state.api_paused_until > now:
                raise MoySkladError(
                    "rate_limited", retryable=True,
                    retry_after=(state.api_paused_until - now).total_seconds(),
                )
            if state.api_lease_token and state.api_lease_expires_at > now:
                raise MoySkladError(
                    "api_busy", retryable=True,
                    retry_after=(state.api_lease_expires_at - now).total_seconds(),
                )
            slot_at = max(now, state.api_next_at or now)
            wait = (slot_at - now).total_seconds()
            if wait > API_INTERVAL_SECONDS:
                raise MoySkladError("rate_limited", retryable=True, retry_after=wait)
            token = uuid.uuid4()
            state.api_lease_token = token
            state.api_lease_expires_at = slot_at + timedelta(seconds=API_LEASE_SECONDS)
            state.api_next_at = slot_at + timedelta(seconds=API_INTERVAL_SECONDS)
            state.save(update_fields=(
                "api_lease_token", "api_lease_expires_at", "api_next_at",
            ))
            self._workflow_heartbeat(now)
        try:
            if wait:
                self.sleep(wait)
        except BaseException:
            self.release(token)
            raise
        return token

    def heartbeat(self, token):
        with transaction.atomic():
            state = self._locked_state()
            now = self.now()
            if token is None or state.api_lease_token != token or state.api_lease_expires_at <= now:
                raise LeaseLost()
            state.api_lease_expires_at = now + timedelta(seconds=API_LEASE_SECONDS)
            # Re-anchor the rate limit to actual sending after a process stall.
            state.api_next_at = max(
                state.api_next_at or now, now + timedelta(seconds=API_INTERVAL_SECONDS),
            )
            state.save(update_fields=("api_lease_expires_at", "api_next_at"))
            self._workflow_heartbeat(now)

    def release(self, token):
        """An expired owner can never release a replacement owner's request."""
        if token is not None:
            self._states().filter(api_lease_token=token).update(
                api_lease_token=None, api_lease_expires_at=None,
            )

    def defer(self, delay_seconds):
        try:
            delay = float(delay_seconds)
            if not math.isfinite(delay) or delay < 0:
                raise ValueError
            until = self.now() + timedelta(seconds=delay)
        except (TypeError, ValueError, OverflowError):
            raise MoySkladError("invalid_retry_after") from None
        with transaction.atomic():
            state = self._locked_state()
            if state.api_paused_until is None or state.api_paused_until < until:
                state.api_paused_until = until
                state.save(update_fields=("api_paused_until",))

    def pause_access(self):
        with transaction.atomic():
            state = self._locked_state()
            state.access_error_code = "access_denied"
            state.access_revision += 1
            state.save(update_fields=("access_error_code", "access_revision"))
            self._workflow_states().update(access_error_code=state.access_error_code)

    def pause_export(self):
        with transaction.atomic():
            state = self._locked_state()
            state.export_paused = True
            state.export_pause_revision += 1
            state.save(update_fields=("export_paused", "export_pause_revision"))
            self._workflow_states().update(export_paused=True)

    def pause_revisions(self):
        """Capture both pause versions, retaining the lock in an outer atomic."""
        with transaction.atomic():
            state = self._locked_state()
            return state.access_revision, state.export_pause_revision

    def clear_access_pause(self, *, expected_revision=None):
        """Clear the account pause after the operator's validated access probe."""
        with transaction.atomic():
            state = self._locked_state()
            if expected_revision is not None and state.access_revision != expected_revision:
                return False
            state.access_error_code = ""
            state.save(update_fields=("access_error_code",))
            self._workflow_states().update(access_error_code="")
            return True

    def clear_export_pause(self, *, expected_revision=None):
        """Clear the account export pause after the operator's validated probe."""
        with transaction.atomic():
            state = self._locked_state()
            if expected_revision is not None and state.export_pause_revision != expected_revision:
                return False
            state.export_paused = False
            state.save(update_fields=("export_paused",))
            self._workflow_states().update(export_paused=False)
            return True
