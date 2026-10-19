from django.conf import settings
from django.db.models import Prefetch
from django.shortcuts import get_object_or_404, render
from django.urls import reverse

from catalog.models import Category, Product
from .legal_documents import LEGAL_DOCUMENTS, render_legal_document
from .contact_location import CONTACT_LOCATION
from .models import ConsentRevision, ContentPage, HomepageRecommendation


PAGE_DEFAULTS = {
    ContentPage.Key.ABOUT: (
        "О компании",
        "Сведения о компании будут добавлены после получения подтверждённых материалов.",
        "Информация о компании ещё не предоставлена заказчиком.",
    ),
    ContentPage.Key.DELIVERY: (
        "Доставка и самовывоз",
        "Способ получения и согласование условий доставки.",
        "Доступны доставка и самовывоз. Стоимость и срок доставки продавец согласует после заявки. Подробные условия ещё не предоставлены заказчиком.",
    ),
    ContentPage.Key.CONTACTS: (
        "Контакты",
        "Контактные сведения продавца будут добавлены после их подтверждения.",
        "Контактные сведения ещё не предоставлены заказчиком.",
    ),
    ContentPage.Key.PRIVACY: (
        "Политика обработки персональных данных",
        "Правила обработки персональных данных на сайте «Калининградская рыбёшка».",
        "Проект документа: перед применением заполните отмеченные поля и проверьте описанные условия обработки.",
    ),
}


def home(request):
    page = ContentPage.objects.filter(key=ContentPage.Key.HOME).first()
    published_products = Product.objects.published().select_related("category").prefetch_related("images")
    categories = (
        Category.objects.filter(products__is_published=True, products__integration_block_reason="")
        .order_by("display_order", "name", "pk")
        .distinct()
        .prefetch_related(
            Prefetch(
                "products",
                queryset=published_products.order_by("display_order", "name", "pk"),
                to_attr="published_products",
            )
        )
    )
    request.navigation_categories = categories
    recommendations = HomepageRecommendation.objects.select_related("product").prefetch_related("product__images").order_by("slot")
    featured_products = [
        recommendation.product
        for recommendation in recommendations
        if recommendation.product_id and recommendation.product.is_published
        and not recommendation.product.integration_block_reason
    ]
    catalog_ready = Product.objects.published().exists()
    hero_values = [
        {"icon": icon, "text": text}
        for icon, text in (
            ("img/quality-raw-material.svg", "Сырье высочайшего качества"),
            ("img/own-production.svg", "Собственное производство"),
            ("img/modern-standards.svg", "Новейшие стандарты"),
        )
    ]

    return render(
        request,
        "home.html",
        {
            "page_title": page.title if page and page.title else "Рыбная продукция",
            "seo_title": page.seo_title if page else "",
            "meta_description": (
                page.seo_description
                if page and page.seo_description
                else "Ассортимент рыбной продукции, розничные заявки и отдельный запрос для оптовых покупателей."
            ),
            "home_title": page.title if page and page.title else "",
            "categories": categories,
            "featured_products": featured_products,
            "catalog_ready": catalog_ready,
            "hero_values": hero_values,
            "about_summary": page.body if page else "",
        },
    )


def _page(request, *, key, title, description, notice, template="content/page.html", extra_context=None):
    page = ContentPage.objects.filter(key=key).first()
    page_title = page.title if page and page.title else title
    return render(
        request,
        template,
        {
            "page_title": page_title,
            "seo_title": page.seo_title if page else "",
            "meta_description": page.seo_description if page and page.seo_description else description,
            "content_notice": notice,
            "page_body": page.body if page else "",
            "breadcrumbs": [{"label": page_title}],
            **(extra_context or {}),
        },
    )


def about(request):
    title, description, notice = PAGE_DEFAULTS[ContentPage.Key.ABOUT]
    return _page(request, key=ContentPage.Key.ABOUT, title=title, description=description, notice=notice)


def delivery(request):
    title, description, notice = PAGE_DEFAULTS[ContentPage.Key.DELIVERY]
    return _page(
        request,
        key=ContentPage.Key.DELIVERY,
        title=title,
        description=description,
        notice=notice,
        template="content/delivery.html",
        extra_context={"yandex_maps_api_key": settings.YANDEX_MAPS_API_KEY},
    )


def contacts(request):
    title, description, notice = PAGE_DEFAULTS[ContentPage.Key.CONTACTS]
    return _page(
        request,
        key=ContentPage.Key.CONTACTS,
        title=title,
        description=description,
        notice=notice,
        template="content/contacts.html",
        extra_context={"contact_location": CONTACT_LOCATION, "yandex_maps_api_key": settings.YANDEX_MAPS_API_KEY},
    )


def privacy(request):
    title, description, notice = PAGE_DEFAULTS[ContentPage.Key.PRIVACY]
    document = render_legal_document("policy")
    return _page(
        request,
        key=ContentPage.Key.PRIVACY,
        title=title,
        description=description,
        notice=notice,
        template="content/legal.html",
        extra_context={
            "legal_html": document["html"],
            "legal_draft_notice": document["draft_notice"],
            "legal_current_key": "policy",
            "legal_navigation": _legal_navigation("policy"),
            "meta_robots": "noindex, nofollow",
        },
    )


def _legal_navigation(current_key):
    return [
        {
            "key": key,
            "title": document["title"],
            "url": reverse(document["route"]),
            "current": key == current_key,
        }
        for key, document in LEGAL_DOCUMENTS.items()
    ]


def _standalone_legal_page(request, key):
    document = render_legal_document(key)
    return render(
        request,
        "content/legal.html",
        {
            "page_title": document["title"],
            "meta_description": document["description"],
            "meta_robots": "noindex, nofollow",
            "breadcrumbs": [{"label": document["title"]}],
            "legal_html": document["html"],
            "legal_draft_notice": document["draft_notice"],
            "legal_current_key": key,
            "legal_navigation": _legal_navigation(key),
        },
    )


def personal_data_consent(request):
    return _standalone_legal_page(request, "consent")


def consent_revision(request, revision_id):
    revision = get_object_or_404(ConsentRevision, revision_id=revision_id)
    document = render_legal_document("consent", source=revision.text)
    return render(
        request,
        "content/legal.html",
        {
            "page_title": document["title"],
            "meta_description": document["description"],
            "meta_robots": "noindex, nofollow",
            "breadcrumbs": [{"label": document["title"]}],
            "legal_html": document["html"],
            "legal_draft_notice": document["draft_notice"],
            "legal_current_key": "consent",
            "legal_navigation": _legal_navigation("consent"),
            "legal_revision": revision,
        },
    )


def yandex_metrica_consent(request):
    return _standalone_legal_page(request, "metrica")


def not_found(request, exception=None):
    return render(
        request,
        "errors/404.html",
        {"page_title": "Страница не найдена", "meta_robots": "noindex, nofollow"},
        status=404,
    )


def sitemap(request):
    locations = [
        request.build_absolute_uri(reverse(name))
        for name in (
            "home",
            "inquiries:wholesale",
            "content:about",
            "content:delivery",
            "content:contacts",
        )
    ]
    locations.extend(
        request.build_absolute_uri(reverse("catalog:category", args=(slug,)))
        for slug in Category.objects.filter(products__is_published=True, products__integration_block_reason="")
        .order_by()
        .values_list("slug", flat=True)
        .distinct()
    )
    locations.extend(
        request.build_absolute_uri(reverse("catalog:product", args=(slug,)))
        for slug in Product.objects.published().values_list("slug", flat=True)
    )
    return render(
        request,
        "sitemap.xml",
        {"locations": locations},
        content_type="application/xml; charset=utf-8",
    )
