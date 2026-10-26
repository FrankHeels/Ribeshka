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
    public_categories = Category.objects.filter(
        products__is_published=True, products__integration_block_reason="",
    ).distinct()
    category_obj = get_object_or_404(public_categories, slug=slug)
    queryset = _published_products().filter(category=category_obj)
    queryset = queryset.order_by("display_order", "name", "pk")
    page_obj, products = _page_products(request, queryset)
    description = category_obj.description or f"Опубликованные товары категории «{category_obj.name}»."
    canonical_path = reverse("catalog:category", args=(category_obj.slug,))
    if page_obj.number > 1:
        canonical_path += f"?page={page_obj.number}"

    return render(
        request,
        "catalog/category.html",
        {
            "page_title": category_obj.name,
            "seo_title": category_obj.seo_title or f"{category_obj.name} — Калининградская рыбёшка",
            "meta_description": (category_obj.seo_description or description)[:320],
            "canonical_url": request.build_absolute_uri(canonical_path),
            "category": category_obj,
            "products": products,
            "page_obj": page_obj,
            "breadcrumbs": [{"label": category_obj.name}],
        },
    )


def product(request, slug):
    product_obj = get_object_or_404(_published_products(), slug=slug)
    meta_description = product_obj.seo_description or product_obj.short_description or product_obj.description or product_obj.name

    return render(
        request,
        "catalog/product.html",
        {
            "page_title": product_obj.name,
            "seo_title": product_obj.seo_title or f"{product_obj.name} — Калининградская рыбёшка",
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
