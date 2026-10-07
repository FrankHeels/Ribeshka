"""Server-side wholesale inquiry creation."""

import uuid
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connections, transaction
from django.utils import timezone

from integrations.public_forms import (
    acquire_advisory_transaction_lock,
    consume_public_form_rate_limit,
    enqueue_notification,
    make_submission_fields,
)

from .models import Inquiry


@dataclass(frozen=True)
class InquirySubmissionResult:
    status: str
    inquiry: Inquiry


def wholesale_submission_fields(request, submission_key=None):
    return make_submission_fields(
        request,
        "wholesale",
        submission_key or uuid.uuid4(),
        {"form": "wholesale"},
    )


def find_existing_inquiry(submission_key, *, using="default"):
    with transaction.atomic(using=using):
        acquire_advisory_transaction_lock("wholesale-submission", submission_key, using=using)
        return Inquiry.objects.using(using).filter(submission_key=submission_key).first()


def submit_wholesale_inquiry(request, cleaned_data, claim, *, using="default"):
    connection = connections[using]
    if connection.vendor != "postgresql":
        raise RuntimeError("Публичные заявки поддерживают только PostgreSQL.")

    key = claim["submission_key"]
    with transaction.atomic(using=using):
        acquire_advisory_transaction_lock("wholesale-submission", key, using=using)
        existing = Inquiry.objects.using(using).filter(submission_key=key).first()
        if existing is not None:
            return InquirySubmissionResult("existing", existing)

        consume_public_form_rate_limit(request, "wholesale", using=using)

        inquiry = Inquiry(
            contact_name=cleaned_data["contact_name"],
            company=cleaned_data["company"],
            phone=cleaned_data["phone"],
            email=cleaned_data["email"],
            need="",
            privacy_consent=True,
            privacy_consent_at=timezone.now(),
            submission_key=key,
        )
        try:
            with transaction.atomic(using=using):
                inquiry.save(using=using, force_insert=True)
                enqueue_notification(inquiry=inquiry, using=using)
        except IntegrityError:
            existing = Inquiry.objects.using(using).filter(submission_key=key).first()
            if existing is not None:
                return InquirySubmissionResult("existing", existing)
            raise
        except ValidationError:
            existing = Inquiry.objects.using(using).filter(submission_key=key).first()
            if existing is not None:
                return InquirySubmissionResult("existing", existing)
            raise

    return InquirySubmissionResult("created", inquiry)
