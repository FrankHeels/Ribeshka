"""HTML rendering and response presentation for the retail cart."""

from django.db.models import prefetch_related_objects
from django.shortcuts import render
from django.urls import reverse

from .state import CART_ISSUES_MESSAGE, get_cart_state
from ..services import retail_submission_fields


def _private_no_store(response):
    response["Cache-Control"] = "private, no-store, max-age=0"
    return response


def render_cart(
    request,
    *,
    status=200,
    cart_error=None,
    submitted_line=None,
    values=None,
    field_errors=None,
    form_errors=None,
    price_changed=False,
    cart_changes=None,
    submission=None,
    preserve_confirmation=False,
):
    state = get_cart_state(request)
    items = [item.copy() for item in state.items]
    prefetch_related_objects([item["product"] for item in items], "images")
    submission = submission or retail_submission_fields(
        request,
        state,
        requires_confirmation=(
            preserve_confirmation or request.GET.get("confirm") == "1"
        ),
    )
    confirmation_visible = (
        price_changed or preserve_confirmation or request.GET.get("confirm") == "1"
    )
    displayed_changes = cart_changes or []
    if confirmation_visible and not displayed_changes:
        displayed_changes = [
            {"message": "Проверьте показанный состав и подтвердите его перед отправкой."}
        ]
    if submitted_line:
        line_id, raw_weight, errors = submitted_line
        for item in items:
            if item["key"] == line_id:
                item["weight_input_value"] = raw_weight
                item["weight_is_bound"] = True
                item["weight_errors"] = errors
                break

    return _private_no_store(
        render(
            request,
            "orders/cart.html",
            {
                "page_title": "Корзина",
                "meta_description": "Состав заявки на рыбную продукцию.",
                "meta_robots": "noindex, nofollow",
                "cart_items": items,
                "cart_total": state.total,
                "cart_weight": (
                    sum(item["weight"] for item in items)
                    if state.total_is_valid and not any(item.get("weight_errors") for item in items)
                    else None
                ),
                "cart_total_is_valid": state.total_is_valid,
                "cart_issues": state.has_issues,
                "cart_issues_message": CART_ISSUES_MESSAGE,
                "cart_error": cart_error,
                "values": values or {},
                "field_errors": field_errors or {},
                "form_errors": form_errors or [],
                "price_changed": confirmation_visible,
                "cart_changes": displayed_changes,
                "submission": submission,
                "forms_enabled": True,
                "breadcrumbs": [{"label": "Корзина"}],
            },
            status=status,
        )
    )


def _product_breadcrumbs(product):
    return [
        {
            "label": product.category.name,
            "url": reverse("catalog:category", args=(product.category.slug,)),
        },
        {"label": product.name},
    ]


def _render_product_weight_error(request, product, form, state):
    return render(
        request,
        "catalog/product.html",
        {
            "page_title": product.name,
            "seo_title": f"{product.name} — Калининградская рыбёшка",
            "meta_description": (product.short_description or product.description or product.name)[:320],
            "product": product,
            "forms_enabled": True,
            "field_errors": {"weight": form.errors.get("weight", [])},
            "field_ids": {"weight": "product-weight"},
            "initial_weight": form["weight"].value(),
            "cart_issues": state.has_issues,
            "cart_issues_message": CART_ISSUES_MESSAGE,
            "breadcrumbs": _product_breadcrumbs(product),
        },
        status=400,
    )
