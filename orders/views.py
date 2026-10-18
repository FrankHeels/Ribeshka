"""Django URL entry points for the retail cart and request result."""

from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from integrations.public_forms import public_form_database_fallback

from .cart.actions import handle_cart_action
from .models import Order
from .cart.presentation import _private_no_store, render_cart
from .request_flow.handlers import submit_retail_request


@public_form_database_fallback("orders:cart", "Вернуться к корзине")
@require_http_methods(["GET", "HEAD", "POST"])
def cart(request):
    if request.method in {"GET", "HEAD"}:
        return render_cart(request)
    if request.POST.get("action") == "submit":
        return submit_retail_request(request)
    return handle_cart_action(request)


@public_form_database_fallback("orders:cart", "Вернуться к корзине")
@require_http_methods(["GET", "HEAD", "POST"])
def checkout(request):
    if request.method == "POST":
        return submit_retail_request(request)
    return redirect(f"{reverse('orders:cart')}#request")


@public_form_database_fallback("orders:cart", "Вернуться к корзине")
@require_http_methods(["GET", "HEAD"])
def success(request, token):
    if not Order.objects.filter(success_token=token).exists():
        raise Http404("Заявка не найдена")
    response = render(
        request,
        "orders/success.html",
        {
            "page_title": "Заявка получена",
            "meta_robots": "noindex, nofollow",
        },
    )
    response["X-Robots-Tag"] = "noindex, nofollow"
    return _private_no_store(response)
