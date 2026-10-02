from django.shortcuts import render


def wholesale(request):
    if request.method == "POST":
        return render(
            request,
            "errors/form-unavailable.html",
            {
                "page_title": "Заявки пока не принимаются",
                "meta_robots": "noindex, nofollow",
            },
            status=503,
        )
    return render(
        request,
        "inquiries/wholesale.html",
        {
            "page_title": "Оптовый запрос",
            "meta_description": "Отдельная форма для запроса условий оптовой покупки.",
            "forms_enabled": False,
            "breadcrumbs": [{"label": "Оптовый запрос"}],
        },
    )

