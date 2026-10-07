"""Django URL entry points for wholesale inquiries."""

from django.http import Http404
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from integrations.public_forms import public_form_database_fallback

from .models import Inquiry
from .request_flow.handlers import submit_wholesale_request
from .request_flow.presentation import private_no_store, render_wholesale


@public_form_database_fallback("inquiries:wholesale", "Вернуться к форме")
@require_http_methods(["GET", "HEAD", "POST"])
def wholesale(request):
    if request.method != "POST":
        return render_wholesale(request)
    return submit_wholesale_request(request)


@public_form_database_fallback("inquiries:wholesale", "Вернуться к форме")
@require_http_methods(["GET", "HEAD"])
def success(request, token):
    inquiry = Inquiry.objects.filter(success_token=token).only("inquiry_number").first()
    if inquiry is None:
        raise Http404("Запрос не найден")
    response = render(
        request,
        "inquiries/success.html",
        {
            "page_title": "Запрос принят",
            "meta_robots": "noindex, nofollow",
            "inquiry_number": inquiry.inquiry_number,
        },
    )
    response["X-Robots-Tag"] = "noindex, nofollow"
    return private_no_store(response)
