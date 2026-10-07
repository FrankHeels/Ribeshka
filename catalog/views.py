from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, render
from django.urls import reverse

from orders.cart import MIN_RETAIL_WEIGHT

from .models import Category, Product


PRODUCTS_PER_PAGE = 12


def _published_products():
    return Product.objects.published().select_related("category").prefetch_related("images")


def _page_products(request, queryset):
    page_obj = Paginator(queryset, PRODUCTS_PER_PAGE).get_page(request.GET.get("page"))
    return page_obj, page_obj.object_list


def category(request, slug):
    public_categories = Category.objects.filter(products__is_published=True).distinct()
    category_obj = get_object_or_404(public_categories, slug=slug)
    queryset = _published_products().filter(category=category_obj).order_by("name", "pk")
    page_obj, products = _page_products(request, queryset)
    description = category_obj.description or f"Опубликованные товары категории «{category_obj.name}»."

    return render(
        request,
        "catalog/category.html",
        {
            "page_title": category_obj.name,
            "seo_title": f"{category_obj.name} — Калининградская рыбёшка",
            "meta_description": description[:320],
            "category": category_obj,
            "products": products,
            "page_obj": page_obj,
            "breadcrumbs": [{"label": category_obj.name}],
        },
    )


def product(request, slug):
    product_obj = get_object_or_404(_published_products(), slug=slug)
    meta_description = product_obj.short_description or product_obj.description or product_obj.name

    return render(
        request,
        "catalog/product.html",
        {
            "page_title": product_obj.name,
            "seo_title": f"{product_obj.name} — Калининградская рыбёшка",
            "meta_description": meta_description[:320],
            "product": product_obj,
            "forms_enabled": True,
            "field_errors": {},
            "initial_weight": MIN_RETAIL_WEIGHT,
            "breadcrumbs": [
                {
                    "label": product_obj.category.name,
                    "url": reverse("catalog:category", args=(product_obj.category.slug,)),
                },
                {"label": product_obj.name},
            ],
        },
    )
