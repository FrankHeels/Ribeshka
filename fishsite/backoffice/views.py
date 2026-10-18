from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, render
from django.utils.cache import add_never_cache_headers
from django.views.decorators.http import require_GET

from catalog.models import Product
from orders.cart import MIN_RETAIL_WEIGHT

from .moysklad_views import moysklad_status  # AdminSite compatibility import.


@staff_member_required
@require_GET
def preview_product(request, product_id):
    if not request.user.is_verified() or not request.user.has_perm("catalog.view_product"):
        raise PermissionDenied
    product = get_object_or_404(Product.objects.select_related("category").prefetch_related("images"), pk=product_id)
    response = render(
        request,
        "catalog/product.html",
        {
            "page_title": product.name,
            "seo_title": product.seo_title or product.name,
            "meta_description": (product.seo_description or product.short_description or product.description or product.name)[:320],
            "product": product,
            "forms_enabled": False,
            "preview_mode": True,
            "meta_robots": "noindex, nofollow, noarchive",
            "initial_weight": MIN_RETAIL_WEIGHT,
            "field_errors": {},
            "breadcrumbs": [{"label": product.name}],
        },
    )
    add_never_cache_headers(response)
    response["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response
