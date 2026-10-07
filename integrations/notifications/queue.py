"""Atomic outbox claims and compare-and-set delivery state transitions."""

from dataclasses import dataclass
from datetime import timedelta
import logging
import uuid
import time

from django.conf import settings
from django.db import transaction
from django.db.models import Case, DateTimeField, F, Min, Q, When
from django.utils import timezone

from integrations.models import NotificationOutbox
from .events import log_notification_event


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class NotificationClaim:
    outbox_id: int
    lease_token: uuid.UUID
    attempt_count: int


class NotificationQueue:
    def __init__(
        self,
        *,
        using="default",
        max_attempts=None,
        retry_base_seconds=None,
        retry_max_seconds=None,
        lease_seconds=None,
        now=timezone.now,
    ):
        self.using = using
        self.max_attempts = int(max_attempts if max_attempts is not None else settings.NOTIFICATION_MAX_ATTEMPTS)
        self.retry_base_seconds = int(
            retry_base_seconds if retry_base_seconds is not None else settings.NOTIFICATION_RETRY_BASE_SECONDS
        )
        self.retry_max_seconds = int(
            retry_max_seconds if retry_max_seconds is not None else settings.NOTIFICATION_RETRY_MAX_SECONDS
        )
        self.lease_seconds = int(lease_seconds if lease_seconds is not None else settings.NOTIFICATION_LEASE_SECONDS)
        self.now = now
        if (
            self.max_attempts <= 0
            or self.retry_base_seconds <= 0
            or self.retry_max_seconds <= 0
            or self.lease_seconds <= 0
        ):
            raise ValueError("Notification queue settings must be positive")

    def claim_next(self):
        while True:
            started = time.monotonic()
            now = self.now()
            ready = Q(status=NotificationOutbox.Status.PENDING, next_attempt_at__lte=now) | Q(
                status=NotificationOutbox.Status.PROCESSING,
                lease_expires_at__lte=now,
            )
            exhausted_id = None
            with transaction.atomic(using=self.using):
                row = (
                    NotificationOutbox.objects.using(self.using)
                    .filter(ready)
                    .order_by("created_at", "pk")
                    .select_for_update(skip_locked=True)
                    .first()
                )
                if row is None:
                    return None

                if row.attempt_count >= self.max_attempts:
                    row.status = NotificationOutbox.Status.MANUAL_REVIEW
                    row.last_error = "attempt_limit_reached"
                    row.next_attempt_at = now
                    row.lease_expires_at = None
                    row.lease_token = None
                    row.sent_at = None
                    row.save(
                        using=self.using,
                        update_fields=(
                            "status", "last_error", "next_attempt_at", "lease_expires_at", "lease_token", "sent_at"
                        ),
                    )
                    exhausted_id = row.pk
                else:
                    token = uuid.uuid4()
                    row.status = NotificationOutbox.Status.PROCESSING
                    row.attempt_count += 1
                    row.lease_token = token
                    row.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
                    row.sent_at = None
                    row.save(
                        using=self.using,
                        update_fields=("status", "attempt_count", "lease_token", "lease_expires_at", "sent_at"),
                    )
                    return NotificationClaim(row.pk, token, row.attempt_count)

            log_notification_event(
                logger,
                logging.WARNING,
                event="attempt_limit_reached",
                outbox_id=exhausted_id,
                backend=settings.NOTIFICATION_BACKEND,
                result="manual_review",
                duration_seconds=round(max(0, time.monotonic() - started), 3),
                error_code="attempt_limit_reached",
            )

    def mark_sent(self, claim):
        now = self.now()
        return (
            NotificationOutbox.objects.using(self.using)
            .filter(
                pk=claim.outbox_id,
                status=NotificationOutbox.Status.PROCESSING,
                lease_token=claim.lease_token,
                lease_expires_at__gt=now,
            )
            .update(
                status=NotificationOutbox.Status.SENT,
                sent_at=now,
                last_error="",
                lease_token=None,
                lease_expires_at=None,
            )
            == 1
        )

    def mark_temporary_failure(self, claim, error_code):
        now = self.now()
        query = NotificationOutbox.objects.using(self.using).filter(
            pk=claim.outbox_id,
            status=NotificationOutbox.Status.PROCESSING,
            lease_token=claim.lease_token,
            lease_expires_at__gt=now,
        )
        if claim.attempt_count >= self.max_attempts:
            values = {
                "status": NotificationOutbox.Status.MANUAL_REVIEW,
                "last_error": error_code,
                "next_attempt_at": now,
                "lease_token": None,
                "lease_expires_at": None,
            }
        else:
            delay = min(
                self.retry_max_seconds,
                self.retry_base_seconds * (2 ** min(claim.attempt_count - 1, 30)),
            )
            values = {
                "status": NotificationOutbox.Status.PENDING,
                "last_error": error_code,
                "next_attempt_at": now + timedelta(seconds=delay),
                "lease_token": None,
                "lease_expires_at": None,
            }
        return query.update(**values) == 1

    def mark_permanent_failure(self, claim, error_code):
        now = self.now()
        return (
            NotificationOutbox.objects.using(self.using)
            .filter(
                pk=claim.outbox_id,
                status=NotificationOutbox.Status.PROCESSING,
                lease_token=claim.lease_token,
                lease_expires_at__gt=now,
            )
            .update(
                status=NotificationOutbox.Status.MANUAL_REVIEW,
                last_error=error_code,
                next_attempt_at=now,
                lease_token=None,
                lease_expires_at=None,
            )
            == 1
        )

    def next_due_at(self):
        return (
            NotificationOutbox.objects.using(self.using)
            .filter(Q(status=NotificationOutbox.Status.PENDING) | Q(status=NotificationOutbox.Status.PROCESSING))
            .aggregate(
                due_at=Min(
                    Case(
                        When(status=NotificationOutbox.Status.PENDING, then=F("next_attempt_at")),
                        When(status=NotificationOutbox.Status.PROCESSING, then=F("lease_expires_at")),
                        output_field=DateTimeField(),
                    )
                )
            )["due_at"]
        )
