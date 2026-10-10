"""Shared primitives for public retail and wholesale request forms."""

import hashlib
import hmac
import re
import uuid
from functools import wraps

from django.conf import settings
from django.core import signing
from django.core.exceptions import ImproperlyConfigured
from django.db import DatabaseError, connections, transaction
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone


SESSION_NONCE_KEY = "public_form_session_nonce"
PHONE_CHARS = re.compile(r"\+?[0-9() .-]+\Z")


def get_session_nonce(request):
    """Return a stable, unguessable nonce scoped to this browser session."""
    value = request.session.get(SESSION_NONCE_KEY)
    try:
        nonce = uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        nonce = uuid.uuid4()
        request.session[SESSION_NONCE_KEY] = str(nonce)
    return str(nonce)


def make_submission_fields(
    request,
    form_kind,
    submission_key,
    snapshot,
    *,
    consent_revision,
    requires_confirmation=False,
):
    key = uuid.UUID(str(submission_key))
    revision_id = consent_revision.revision_id
    payload = {
        "kind": form_kind,
        "key": str(key),
        "nonce": get_session_nonce(request),
        "snapshot": snapshot,
        "consent_revision_id": revision_id,
        "requires_confirmation": bool(requires_confirmation),
    }
    return {
        "submission_key": str(key),
        "submission_token": signing.dumps(payload, salt=f"fishsite.public-form.{form_kind}", compress=True),
        "consent_url": reverse(
            "content:consent_revision",
            kwargs={"revision_id": revision_id},
        ),
    }


def read_submission_claim(request, form_kind, submission_key, submission_token):
    """Verify signature, key and session binding before any idempotency lookup."""
    try:
        key = uuid.UUID(str(submission_key))
        payload = signing.loads(
            str(submission_token),
            salt=f"fishsite.public-form.{form_kind}",
        )
    except (signing.BadSignature, TypeError, ValueError, AttributeError):
        return None

    if not isinstance(payload, dict):
        return None
    if payload.get("kind") != form_kind or payload.get("key") != str(key):
        return None
    if payload.get("nonce") != get_session_nonce(request):
        return None
    if not isinstance(payload.get("snapshot"), dict):
        return None
    revision_id = payload.get("consent_revision_id")
    if not isinstance(revision_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", revision_id):
        return None
    if not isinstance(payload.get("requires_confirmation"), bool):
        return None
    return {
        "submission_key": key,
        "snapshot": payload["snapshot"],
        "consent_revision_id": revision_id,
        "requires_confirmation": payload["requires_confirmation"],
    }


def consent_revision_display_fields(consent_revision):
    """Build the public link for a revision already resolved from a signed token."""
    return {
        "consent_url": reverse(
            "content:consent_revision",
            kwargs={"revision_id": consent_revision.revision_id},
        ),
    }


def _advisory_lock_parts(namespace, value):
    digest = hashlib.sha256(f"{namespace}:{value}".encode("utf-8")).digest()[:8]
    return (
        int.from_bytes(digest[:4], byteorder="big", signed=True),
        int.from_bytes(digest[4:], byteorder="big", signed=True),
    )


def acquire_advisory_transaction_lock(namespace, value, *, using="default"):
    connection = connections[using]
    if connection.vendor != "postgresql" or not connection.in_atomic_block:
        raise ImproperlyConfigured("Публичные заявки требуют транзакцию PostgreSQL.")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            _advisory_lock_parts(namespace, value),
        )


class PublicFormRateLimited(Exception):
    pass


def public_form_database_fallback(retry_url_name, retry_label):
    """Return a DB-independent 503 if a public-form view hits database failure.

    The fallback deliberately renders without a RequestContext: base-template
    context processors query the catalog and cart, and session-bound form token
    generation may also need the database-backed session store.
    """

    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            try:
                return view(request, *args, **kwargs)
            except DatabaseError:
                return render_public_form_unavailable(retry_url_name, retry_label)

        return wrapped

    return decorate


def render_public_form_unavailable(retry_url_name, retry_label):
    """Render a safe 503 without session access or context processors."""
    content = render_to_string(
        "errors/form-unavailable.html",
        {
            "page_title": "Не удалось подтвердить результат",
            "meta_description": "",
            "meta_robots": "noindex, nofollow",
            "retry_url": reverse(retry_url_name),
            "retry_label": retry_label,
            "breadcrumbs": [],
            "navigation_categories": [],
            "cart_count": 0,
            "messages": [],
        },
    )
    response = HttpResponse(content, status=503)
    response["Cache-Control"] = "private, no-store, max-age=0"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


def consume_public_form_rate_limit(request, form_kind, *, using="default"):
    """Increment a shared PostgreSQL time-window bucket for this client."""
    limit = int(getattr(settings, "PUBLIC_FORM_RATE_LIMIT", 20))
    if limit <= 0:
        return
    window_seconds = max(1, int(getattr(settings, "PUBLIC_FORM_RATE_WINDOW_SECONDS", 3600)))
    remote_addr = request.META.get("REMOTE_ADDR", "unknown")
    if getattr(settings, "TRUST_X_REAL_IP", False):
        # Only enable behind a trusted proxy that overwrites this header.
        remote_addr = request.META.get("HTTP_X_REAL_IP") or remote_addr
    material = f"{form_kind}:{remote_addr}".encode("utf-8", errors="replace")
    bucket_key = hmac.new(
        str(settings.SECRET_KEY).encode("utf-8"),
        material,
        hashlib.sha256,
    ).hexdigest()
    now = timezone.now()

    from .models import PublicFormRateLimit

    with transaction.atomic(using=using):
        acquire_advisory_transaction_lock("public-form-rate", bucket_key, using=using)
        bucket = (
            PublicFormRateLimit.objects.using(using)
            .select_for_update()
            .filter(bucket_key=bucket_key)
            .first()
        )
        if bucket is None:
            PublicFormRateLimit.objects.using(using).create(
                bucket_key=bucket_key,
                window_started_at=now,
                request_count=1,
            )
            return

        if (now - bucket.window_started_at).total_seconds() >= window_seconds:
            bucket.window_started_at = now
            bucket.request_count = 1
            bucket.save(using=using, update_fields=("window_started_at", "request_count"))
            return

        if bucket.request_count >= limit:
            raise PublicFormRateLimited
        bucket.request_count += 1
        bucket.save(using=using, update_fields=("request_count",))


def clean_public_phone(value):
    """Accept common international/local formatting without imposing a region."""
    phone = value.strip()
    digits = sum(character in "0123456789" for character in phone)
    if (
        not PHONE_CHARS.fullmatch(phone)
        or phone.count("+") > 1
        or ("+" in phone and not phone.startswith("+"))
        or not 5 <= digits <= 15
    ):
        return None
    return phone


def enqueue_notification(*, order=None, inquiry=None, using="default"):
    """Persist the outbox row and wake listeners in the caller's transaction."""
    connection = connections[using]
    if connection.vendor != "postgresql" or not connection.in_atomic_block:
        raise ImproperlyConfigured("Outbox нужно создавать в транзакции PostgreSQL.")
    if (order is None) == (inquiry is None):
        raise ValueError("Outbox должен ссылаться ровно на одну заявку.")

    from .models import NotificationOutbox

    outbox = NotificationOutbox.objects.using(using).create(order=order, inquiry=inquiry)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_notify(%s, %s)", ("notification_outbox", ""))
    return outbox
