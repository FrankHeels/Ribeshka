"""HTTP orchestration for wholesale inquiry submission."""

from django.core.exceptions import ValidationError
from django.db import DatabaseError
from django.shortcuts import redirect

from content.consent_revisions import get_consent_revision
from integrations.public_forms import (
    PublicFormRateLimited,
    consume_public_form_rate_limit,
    consent_revision_display_fields,
    read_submission_claim,
    render_public_form_unavailable,
)

from ..forms import WholesaleRequestForm
from ..services import find_existing_inquiry, submit_wholesale_inquiry, wholesale_submission_fields
from .presentation import field_errors, private_no_store, render_wholesale


def _values(post):
    return {
        "contact_name": post.get("contact_name", ""),
        "company": post.get("company", ""),
        "phone": post.get("phone", ""),
        "email": post.get("email", ""),
        "consent": post.get("consent") == "yes",
    }


def _service_unavailable():
    return render_public_form_unavailable("inquiries:wholesale", "Вернуться к форме")


def _rate_limit_rejection(request, values, submission):
    try:
        consume_public_form_rate_limit(request, "wholesale")
    except PublicFormRateLimited:
        return render_wholesale(
            request,
            status=429,
            values=values,
            form_errors=["Слишком много запросов. Повторите попытку позже."],
            submission=submission,
        )
    except DatabaseError:
        return _service_unavailable()
    return None


def submit_wholesale_request(request):
    form = WholesaleRequestForm(request.POST)
    values = _values(request.POST)
    claim = read_submission_claim(
        request,
        "wholesale",
        request.POST.get("submission_key"),
        request.POST.get("submission_token"),
    )

    if claim is not None:
        try:
            existing = find_existing_inquiry(claim["submission_key"])
        except DatabaseError:
            return _service_unavailable()
        if existing is not None:
            response = redirect("inquiries:success", token=existing.success_token)
            return private_no_store(response)

    consent_revision = (
        get_consent_revision(claim["consent_revision_id"])
        if claim is not None
        else None
    )
    if consent_revision is None:
        claim = None
        values["consent"] = False

    if claim is not None:
        submission = {
            "submission_key": request.POST.get("submission_key", ""),
            "submission_token": request.POST.get("submission_token", ""),
            **consent_revision_display_fields(consent_revision),
        }
    else:
        submission = wholesale_submission_fields(request)

    if claim is None:
        state_limited = _rate_limit_rejection(request, values, submission)
        if state_limited is not None:
            return private_no_store(state_limited)
        return render_wholesale(
            request,
            status=400,
            values=values,
            form_errors=["Форма устарела. Обновите страницу и повторите отправку."],
            submission=submission,
        )
    if not form.is_valid():
        limited = _rate_limit_rejection(request, values, submission)
        if limited is not None:
            return private_no_store(limited)
        return render_wholesale(
            request,
            status=400,
            values=values,
            field_errors=field_errors(form),
            submission=submission,
        )
    if form.cleaned_data["website"]:
        limited = _rate_limit_rejection(request, values, submission)
        if limited is not None:
            return private_no_store(limited)
        return render_wholesale(
            request,
            status=400,
            values=values,
            form_errors=["Не удалось принять запрос. Проверьте данные и повторите попытку."],
            submission=submission,
        )

    try:
        result = submit_wholesale_inquiry(request, form.cleaned_data, claim)
    except PublicFormRateLimited:
        return render_wholesale(
            request,
            status=429,
            values=values,
            form_errors=["Слишком много запросов. Повторите попытку позже."],
            submission=submission,
        )
    except (DatabaseError, ValidationError):
        return render_wholesale(
            request,
            status=503,
            values=values,
            form_errors=["Не удалось подтвердить результат. Повторите отправку исходной формы позже."],
            submission=submission,
        )

    response = redirect("inquiries:success", token=result.inquiry.success_token)
    return private_no_store(response)
