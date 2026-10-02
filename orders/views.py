from django.contrib import messages
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from catalog.models import Product

from .cart import (
    CART_ISSUES_MESSAGE,
    get_cart_state,
    get_stored_lines,
    parse_product_id,
    set_stored_lines,
)
from .forms import CartWeightForm


def _not_accepting(request):
    return render(
        request,
        "errors/form-unavailable.html",
        {
            "page_title": "Заявки пока не принимаются",
            "meta_robots": "noindex, nofollow",
        },
        status=503,
    )


def _queue_cart_issue(request, state):
    if state.has_issues and hasattr(request, "_messages"):
        messages.warning(request, CART_ISSUES_MESSAGE)


def _render_cart(request, *, status=200, cart_error=None, submitted_line=None):
    state = get_cart_state(request)
    items = [item.copy() for item in state.items]
    if submitted_line:
        line_id, raw_weight, errors = submitted_line
        for item in items:
            if item["key"] == line_id:
                item["weight_input_value"] = raw_weight
                item["weight_is_bound"] = True
                item["weight_errors"] = errors
                break

    return render(
        request,
        "orders/cart.html",
        {
            "page_title": "Корзина",
            "meta_description": "Состав заявки на рыбную продукцию.",
            "cart_items": items,
            "cart_total": state.total,
            "cart_total_is_valid": state.total_is_valid,
            "cart_issues": state.has_issues,
            "cart_issues_message": CART_ISSUES_MESSAGE,
            "cart_error": cart_error,
            "forms_enabled": True,
            "checkout_enabled": False,
            "breadcrumbs": [{"label": "Корзина"}],
        },
        status=status,
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
    existing_line = next(
        (item for item in state.items if item["key"] == str(product.pk)),
        None,
    )
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
            "is_in_cart": existing_line is not None,
            "cart_issues": state.has_issues,
            "cart_issues_message": CART_ISSUES_MESSAGE,
            "breadcrumbs": _product_breadcrumbs(product),
        },
        status=400,
    )


def _load_public_product(product_id):
    return get_object_or_404(
        Product.objects.published().select_related("category").prefetch_related("images"),
        pk=product_id,
    )


@require_http_methods(["GET", "HEAD", "POST"])
def cart(request):
    if request.method in {"GET", "HEAD"}:
        return _render_cart(request)

    action = request.POST.get("action")
    if action == "remove":
        lines, _ = get_stored_lines(request)
        line_id = request.POST.get("cart_line_id")
        if parse_product_id(line_id) is not None and line_id in lines:
            del lines[line_id]
            set_stored_lines(request, lines)
        return redirect("orders:cart")

    if action == "add":
        product_id = parse_product_id(request.POST.get("product_id"))
        if product_id is None:
            raise Http404("Товар не найден")
        product = _load_public_product(product_id)
        state = get_cart_state(request)
        form = CartWeightForm(request.POST, product=product)
        if not form.is_valid():
            return _render_product_weight_error(request, product, form, state)

        lines, _ = get_stored_lines(request)
        lines[str(product.pk)] = format(form.cleaned_data["weight"], "f")
        set_stored_lines(request, lines)
        _queue_cart_issue(request, state)
        return redirect("orders:cart")

    if action == "update":
        state = get_cart_state(request)
        lines, _ = get_stored_lines(request)
        line_id = request.POST.get("cart_line_id")
        product_id = parse_product_id(line_id)
        if product_id is None or line_id not in lines:
            raise Http404("Позиция не найдена")
        product = next(
            (item["product"] for item in state.items if item["key"] == line_id),
            None,
        )
        if product is None:
            raise Http404("Товар не найден")

        form = CartWeightForm(request.POST, product=product)
        if not form.is_valid():
            return _render_cart(
                request,
                status=400,
                cart_error="Вес не сохранён. Исправьте значение.",
                submitted_line=(
                    line_id,
                    request.POST.get("weight", ""),
                    list(form.errors.get("weight", [])),
                ),
            )

        lines[line_id] = format(form.cleaned_data["weight"], "f")
        set_stored_lines(request, lines)
        _queue_cart_issue(request, state)
        return redirect("orders:cart")

    return HttpResponseBadRequest("Неизвестное действие корзины.")


@require_http_methods(["GET", "HEAD", "POST"])
def checkout(request):
    if request.method == "POST":
        return _not_accepting(request)

    state = get_cart_state(request)
    return render(
        request,
        "orders/checkout.html",
        {
            "page_title": "Оформление заявки",
            "meta_description": "Контакты и способ получения заявки на рыбную продукцию.",
            "cart_items": state.items,
            "cart_total": state.total,
            "cart_total_is_valid": state.total_is_valid,
            "cart_issues": state.has_issues,
            "cart_issues_message": CART_ISSUES_MESSAGE,
            "forms_enabled": False,
            "checkout_enabled": False,
            "breadcrumbs": [
                {"label": "Корзина", "url": "/cart/"},
                {"label": "Оформление заявки"},
            ],
        },
    )


def success(request, token):
    # A success page is available only after the backend resolves a real,
    # unguessable order token. Unknown tokens reveal no order information.
    raise Http404("Заявка не найдена")
