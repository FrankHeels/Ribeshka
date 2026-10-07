"""HTML responses and form-error presentation for wholesale inquiries."""

from django.shortcuts import render

from ..services import wholesale_submission_fields


def private_no_store(response):
    response["Cache-Control"] = "private, no-store, max-age=0"
    return response


def field_errors(form):
    hidden = {"submission_key", "submission_token", "website"}
    return {
        name: list(errors)
        for name, errors in form.errors.items()
        if name not in hidden
    }


def render_wholesale(
    request,
    *,
    status=200,
    values=None,
    field_errors=None,
    form_errors=None,
    page_error=None,
    submission=None,
):
    context = {
        "page_title": "Оптовый запрос",
        "meta_description": "Отдельная форма для запроса условий оптовой покупки.",
        "meta_robots": "noindex, nofollow" if status >= 400 else "index, follow",
        "forms_enabled": True,
        "values": values or {},
        "field_errors": field_errors or {},
        "form_errors": form_errors or [],
        "page_error": page_error,
        "submission": submission or wholesale_submission_fields(request),
        "breadcrumbs": [{"label": "Оптовый запрос"}],
    }
    return private_no_store(render(request, "inquiries/wholesale.html", context, status=status))
