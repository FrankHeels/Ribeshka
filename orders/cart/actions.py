"""HTTP action handlers for adding, updating, and removing cart lines."""

from decimal import Decimal

from django.contrib import messages
from django.http import Http404, HttpResponseBadRequest, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse

from catalog.models import Product

from .state import (
    CART_ISSUES_MESSAGE,
    MAX_RETAIL_WEIGHT,
    get_cart_state,
    get_stored_lines,
    is_valid_retail_weight,
    parse_product_id,
    parse_stored_weight,
    set_stored_lines,
)
from ..forms import CartWeightForm
from .presentation import _render_product_weight_error, render_cart


def _queue_cart_issue(request, state):
    if state.has_issues and hasattr(request, "_messages"):
        messages.warning(request, CART_ISSUES_MESSAGE)


def _load_public_product(product_id):
    return get_object_or_404(
        Product.objects.published().select_related("category").prefetch_related("images"),
        pk=product_id,
    )


def _cart_change_redirect(preserve_confirmation):
    url = reverse("orders:cart")
    if preserve_confirmation:
        url += "?confirm=1"
    return HttpResponseRedirect(url)


def handle_cart_action(request):
    """Apply one non-submission POST action to the session cart."""
    action = request.POST.get("action")
    preserve_confirmation = request.POST.get("preserve_confirmation") == "1"

    if action == "remove":
        lines, _ = get_stored_lines(request)
        line_id = request.POST.get("cart_line_id")
        if parse_product_id(line_id) is not None and line_id in lines:
            del lines[line_id]
            set_stored_lines(request, lines)
        return _cart_change_redirect(preserve_confirmation)

    if action == "add":
        product_id = parse_product_id(request.POST.get("product_id"))
        if product_id is None:
            raise Http404("Товар не найден")
        product = _load_public_product(product_id)
        state = get_cart_state(request)
        form = CartWeightForm(request.POST)
        if not form.is_valid():
            return _render_product_weight_error(request, product, form, state)

        requested_weight = form.cleaned_data["weight"]
        lines, _ = get_stored_lines(request)
        existing_weight = parse_stored_weight(lines.get(str(product.pk)))
        if existing_weight is not None and not is_valid_retail_weight(existing_weight):
            form.add_error(
                "weight",
                "В корзине уже сохранён недопустимый вес этого товара. "
                "Исправьте или удалите позицию в корзине перед добавлением.",
            )
            return _render_product_weight_error(request, product, form, state)

        updated_weight = requested_weight + (existing_weight or Decimal("0"))
        if updated_weight > MAX_RETAIL_WEIGHT:
            form.add_error(
                "weight",
                "Общий вес товара не может превышать 99 999 кг. "
                "Укажите меньший добавляемый вес.",
            )
            return _render_product_weight_error(request, product, form, state)

        lines[str(product.pk)] = format(updated_weight.to_integral_value(), "f")
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

        form = CartWeightForm(request.POST)
        if not form.is_valid():
            return render_cart(
                request,
                status=400,
                cart_error="Вес не сохранён. Исправьте значение.",
                preserve_confirmation=preserve_confirmation,
                submitted_line=(
                    line_id,
                    request.POST.get("weight", ""),
                    list(form.errors.get("weight", [])),
                ),
            )

        lines[line_id] = format(form.cleaned_data["weight"], "f")
        set_stored_lines(request, lines)
        _queue_cart_issue(request, state)
        return _cart_change_redirect(preserve_confirmation)

    return HttpResponseBadRequest("Неизвестное действие корзины.")
