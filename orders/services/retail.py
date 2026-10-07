"""Server-authoritative retail request creation."""

import uuid

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connections, transaction
from django.utils import timezone

from integrations.public_forms import (
    consume_public_form_rate_limit,
    make_submission_fields,
)

from .integration_handlers import (
    clear_retail_cart,
    find_existing_retail_order as find_existing_retail_order_in_db,
    get_retail_order_by_key,
    load_locked_cart_snapshot,
    lock_retail_submission,
    persist_retail_order,
)
from .logic import (
    RetailSubmissionResult,
    build_retail_order_data,
    retail_snapshot_for_state,
    retail_submission_rejection,
)


def retail_submission_fields(request, state, *, requires_confirmation=False, submission_key=None):
    snapshot = retail_snapshot_for_state(state)
    return make_submission_fields(
        request,
        "retail",
        submission_key or uuid.uuid4(),
        snapshot,
        requires_confirmation=requires_confirmation,
    )


def find_existing_retail_order(submission_key, *, using="default"):
    return find_existing_retail_order_in_db(submission_key, using=using)


def submit_retail_order(request, claim, cleaned_data, *, using="default"):
    """Compare the signed display snapshot and atomically save order + outbox."""
    key = claim["submission_key"]
    connection = connections[using]
    if connection.vendor != "postgresql":
        raise RuntimeError("Публичные заявки поддерживают только PostgreSQL.")

    with transaction.atomic(using=using):
        lock_retail_submission(key, using=using)
        existing = get_retail_order_by_key(key, using=using)
        if existing is not None:
            return RetailSubmissionResult("existing", order=existing)

        consume_public_form_rate_limit(request, "retail", using=using)

        current_snapshot, current_items, unavailable, invalid_weight = load_locked_cart_snapshot(
            request,
            using=using,
        )
        rejection_status = retail_submission_rejection(
            claim=claim,
            current_snapshot=current_snapshot,
            current_items=current_items,
            unavailable=unavailable,
            invalid_weight=invalid_weight,
            cleaned_data=cleaned_data,
        )
        if rejection_status:
            return RetailSubmissionResult(rejection_status, snapshot=current_snapshot)

        order_data, item_data = build_retail_order_data(
            cleaned_data,
            key,
            current_items,
            timezone.now(),
        )
        try:
            # Keep model uniqueness validation and all database constraints inside
            # a savepoint so an integrity race does not poison this transaction.
            with transaction.atomic(using=using):
                # The handler saves the parent before building related rows.
                # OrderItem.bulk_create() validates each instance before Django's
                # related-field preparation, so an item built against an unsaved
                # Order would fail full_clean() with a missing required FK.
                order = persist_retail_order(order_data, item_data, using=using)
        except IntegrityError:
            existing = get_retail_order_by_key(key, using=using)
            if existing is not None:
                return RetailSubmissionResult("existing", order=existing)
            raise
        except ValidationError:
            # Order.save() runs full_clean(), including a pre-insert unique check.
            # Under the advisory lock, a key race is resolved before this point.
            existing = get_retail_order_by_key(key, using=using)
            if existing is not None:
                return RetailSubmissionResult("existing", order=existing)
            raise

    return RetailSubmissionResult("created", order=order)


def clear_submitted_cart(request):
    """Clear only after the request transaction committed successfully."""
    clear_retail_cart(request)
