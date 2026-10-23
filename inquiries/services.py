"""Server-side wholesale inquiry creation."""

import uuid
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connections, transaction
from django.utils import timezone

from content.consent_revisions import get_consent_revision, get_current_consent_revision
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


def delete_inquiry(inquiry_id, *, actor):
    """Delete one confirmed wholesale request and its inactive notification."""
    from integrations.request_deletion import locked_request_outbox, require_request_deletion

    require_request_deletion(actor, "inquiries", "inquiry")
    with transaction.atomic():
        inquiry = Inquiry.objects.select_for_update().filter(pk=inquiry_id).first()
        if inquiry is None:
            return "deleted"
        outbox = locked_request_outbox(inquiry=inquiry)
        outbox.delete()
        inquiry.delete()
    return "deleted"


def wholesale_submission_fields(request, submission_key=None, *, consent_revision=None, consent_revision_id=None):
    if consent_revision is None:
        consent_revision = (
            get_consent_revision(consent_revision_id)
            if consent_revision_id
            else get_current_consent_revision()
        )
    if consent_revision is None:
        raise ValueError("Архивная редакция согласия не найдена.")
    return make_submission_fields(
        request,
        "wholesale",
        submission_key or uuid.uuid4(),
        {"form": "wholesale"},
        consent_revision=consent_revision,
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

        revision = get_consent_revision(claim.get("consent_revision_id"), using=using)
        if revision is None:
            raise ValidationError({"privacy_consent_revision": "Редакция согласия больше недоступна."})

        consume_public_form_rate_limit(request, "wholesale", using=using)

        inquiry = Inquiry(
            contact_name=cleaned_data["contact_name"],
            company=cleaned_data["company"],
            phone=cleaned_data["phone"],
            email=cleaned_data["email"],
            need="",
            privacy_consent=True,
            privacy_consent_at=timezone.now(),
            privacy_consent_revision=revision,
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
