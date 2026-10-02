from django.db.models import Prefetch
from django.shortcuts import render
from django.urls import reverse

from catalog.models import Category, Product
from .models import ContentPage


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
        "Текст политики обработки персональных данных будет опубликован после согласования.",
        "Текст политики ещё не предоставлен заказчиком. Формы пока отключены и не сохраняют данные.",
    ),
}


def home(request):
    page = ContentPage.objects.filter(key=ContentPage.Key.HOME).prefetch_related("sections").first()
    sections = list(page.sections.all()) if page else []
    section_map = {section.key: section for section in sections}
    advantages = [
        {"title": section.title, "description": section.body}
        for section in sections
        if section.key.startswith("advantage_") and (section.title or section.body.strip())
    ]
    hero = section_map.get("hero")
    about = section_map.get("about")
    contacts = section_map.get("contacts")
    published_products = Product.objects.published().select_related("category").prefetch_related("images")
    categories = (
        Category.objects.filter(products__is_published=True)
        .order_by("display_order", "name", "pk")
        .distinct()
        .prefetch_related(
            Prefetch(
                "products",
                queryset=published_products.order_by("name", "pk"),
                to_attr="published_products",
            )
        )
    )
    request.navigation_categories = categories
    featured_products = list(published_products.order_by("name", "pk")[:2])
    catalog_ready = Product.objects.published().exists()

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
            "home_sections": section_map,
            "home_title": page.title if page and page.title else "",
            "hero_section": hero,
            "categories": categories,
            "featured_products": featured_products,
            "catalog_ready": catalog_ready,
            "hero_photo": None,
            "advantages": advantages,
            "about_summary": about.body if about else "",
            "contact_summary": contacts.body if contacts else "",
            "delivery_section": section_map.get("delivery"),
            "home_steps": [
                section for section in sections
                if section.key in {"step_1", "step_2", "step_3"}
                and (section.title or section.body.strip())
            ],
        },
    )


def _page(request, *, key, title, description, notice):
    page = ContentPage.objects.filter(key=key).prefetch_related("sections").first()
    sections = list(page.sections.all()) if page else []
    lead = next((section for section in sections if section.key == "lead"), None)
    substantive_sections = [
        section for section in sections
        if section.key != "lead" and (section.title or section.body.strip())
    ]
    page_title = page.title if page and page.title else title
    return render(
        request,
        "content/page.html",
        {
            "page_title": page_title,
            "seo_title": page.seo_title if page else "",
            "meta_description": page.seo_description if page and page.seo_description else description,
            "content_notice": notice,
            "page_lead": lead,
            "content_sections": substantive_sections,
            "breadcrumbs": [{"label": page_title}],
        },
    )


def about(request):
    title, description, notice = PAGE_DEFAULTS[ContentPage.Key.ABOUT]
    return _page(request, key=ContentPage.Key.ABOUT, title=title, description=description, notice=notice)


def delivery(request):
    title, description, notice = PAGE_DEFAULTS[ContentPage.Key.DELIVERY]
    return _page(request, key=ContentPage.Key.DELIVERY, title=title, description=description, notice=notice)


def contacts(request):
    title, description, notice = PAGE_DEFAULTS[ContentPage.Key.CONTACTS]
    return _page(request, key=ContentPage.Key.CONTACTS, title=title, description=description, notice=notice)


def privacy(request):
    title, description, notice = PAGE_DEFAULTS[ContentPage.Key.PRIVACY]
    return _page(request, key=ContentPage.Key.PRIVACY, title=title, description=description, notice=notice)


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
            "content:privacy",
        )
    ]
    locations.extend(
        request.build_absolute_uri(reverse("catalog:category", args=(slug,)))
        for slug in Category.objects.filter(products__is_published=True)
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
