"""Coordinate HTTP validation and responses for retail request submission."""

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

from ..cart.state import get_cart_state
from ..forms import RetailRequestForm
from ..cart.presentation import _private_no_store, render_cart as _render_cart
from ..services import (
    clear_submitted_cart,
    find_existing_retail_order,
    retail_submission_fields,
    submit_retail_order,
)


def _form_field_errors(form):
    hidden = {"submission_key", "submission_token", "website"}
    return {
        name: list(errors)
        for name, errors in form.errors.items()
        if name not in hidden
    }


def _retail_values(post):
    return {
        "name": post.get("name", ""),
        "phone": post.get("phone", ""),
        "email": post.get("email", ""),
        "pickup_method": post.get("pickup_method", ""),
        "address": post.get("address", ""),
        "comment": post.get("comment", ""),
        "consent": post.get("consent") == "yes",
        "confirm_updated_cart": post.get("confirm_updated_cart") == "yes",
    }


def _request_unavailable():
    return render_public_form_unavailable("orders:cart", "Вернуться к корзине")


def _rate_limit_rejection(request, values, submission):
    try:
        consume_public_form_rate_limit(request, "retail")
    except PublicFormRateLimited:
        return _private_no_store(
            _render_cart(
                request,
                status=429,
                values=values,
                form_errors=["Слишком много запросов. Повторите попытку позже."],
                submission=submission,
            )
        )
    except DatabaseError:
        return _request_unavailable()
    return None


def submit_retail_request(request):
    form = RetailRequestForm(request.POST)
    values = _retail_values(request.POST)
    claim = read_submission_claim(
        request,
        "retail",
        request.POST.get("submission_key"),
        request.POST.get("submission_token"),
    )
    # Resolve a previously accepted key before cart state, form validation, or
    # rate limiting so a lost response can safely be retried after cart cleanup.
    if claim is not None:
        try:
            existing = find_existing_retail_order(claim["submission_key"])
        except DatabaseError:
            return _request_unavailable()
        if existing is not None:
            return _private_no_store(
                redirect("orders:success", token=existing.success_token)
            )

    consent_revision = (
        get_consent_revision(claim["consent_revision_id"])
        if claim is not None
        else None
    )
    if consent_revision is None:
        claim = None

    if claim is None:
        state = get_cart_state(request)
        values["consent"] = False
        fresh_fields = retail_submission_fields(request, state)
        limited = _rate_limit_rejection(request, values, fresh_fields)
        if limited is not None:
            return limited
        return _private_no_store(
            _render_cart(
                request,
                status=400,
                values=values,
                form_errors=["Форма устарела. Обновите страницу и повторите отправку."],
                submission=fresh_fields,
            )
        )
    submitted_fields = {
        "submission_key": request.POST.get("submission_key", ""),
        "submission_token": request.POST.get("submission_token", ""),
        **consent_revision_display_fields(consent_revision),
    }
    if not form.is_valid():
        limited = _rate_limit_rejection(request, values, submitted_fields)
        if limited is not None:
            return limited
        field_errors = _form_field_errors(form)
        return _private_no_store(
            _render_cart(
                request,
                status=400,
                values=values,
                field_errors=field_errors,
                form_errors=(
                    []
                    if field_errors
                    else ["Не удалось принять заявку. Проверьте данные и повторите попытку."]
                ),
                submission=submitted_fields,
                price_changed=claim["requires_confirmation"],
                cart_changes=(
                    [{"message": "Проверьте показанный состав и подтвердите его перед отправкой."}]
                    if claim["requires_confirmation"]
                    else []
                ),
            )
        )
    if form.cleaned_data["website"]:
        limited = _rate_limit_rejection(request, values, submitted_fields)
        if limited is not None:
            return limited
        return _private_no_store(
            _render_cart(
                request,
                status=400,
                values=values,
                form_errors=["Не удалось принять заявку. Проверьте данные и повторите попытку."],
                submission=submitted_fields,
            )
        )

    try:
        result = submit_retail_order(request, claim, form.cleaned_data)
    except PublicFormRateLimited:
        return _private_no_store(
            _render_cart(
                request,
                status=429,
                values=values,
                form_errors=["Слишком много запросов. Повторите попытку позже."],
                submission=submitted_fields,
            )
        )
    except (DatabaseError, ValidationError):
        return _private_no_store(
            _render_cart(
                request,
                status=503,
                values=values,
                form_errors=["Не удалось подтвердить результат. Повторите отправку исходной формы позже."],
                submission=submitted_fields,
                price_changed=claim["requires_confirmation"],
                cart_changes=(
                    [{"message": "Проверьте показанный состав и подтвердите его перед отправкой."}]
                    if claim["requires_confirmation"]
                    else []
                ),
            )
        )

    if result.status in {"created", "existing"}:
        if result.status == "created":
            clear_submitted_cart(request)
        return _private_no_store(
            redirect("orders:success", token=result.order.success_token)
        )

    if result.status == "changed":
        state = get_cart_state(request)
        renewed_submission = retail_submission_fields(
            request,
            state,
            requires_confirmation=True,
            consent_revision=consent_revision,
        )
        return _private_no_store(
            _render_cart(
                request,
                status=409,
                values={**values, "confirm_updated_cart": False},
                price_changed=True,
                cart_changes=[
                    {"message": "Цена или состав корзины изменились. Проверьте показанные данные и подтвердите их."}
                ],
                submission=renewed_submission,
            )
        )

    if result.status == "confirmation_required":
        return _private_no_store(
            _render_cart(
                request,
                status=400,
                values=values,
                field_errors={
                    "confirm_updated_cart": ["Подтвердите показанные состав и цену перед отправкой."]
                },
                price_changed=True,
                cart_changes=[{"message": "Подтвердите состав и цену, показанные в корзине."}],
                submission=submitted_fields,
            )
        )

    form_errors = (
        ["Сначала добавьте товары в корзину."]
        if result.status == "empty"
        else ["Исправьте целый вес каждой позиции от 1 до 99 999 кг."]
    )
    return _private_no_store(
        _render_cart(
            request,
            status=400,
            values=values,
            form_errors=form_errors,
            submission=submitted_fields,
        )
    )
