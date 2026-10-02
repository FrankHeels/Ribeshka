from .models import Category


def navigation_categories(request):
    """Expose only categories that contain at least one published product."""
    categories = getattr(request, "navigation_categories", None)
    if categories is None:
        if request.path_info.startswith("/admin/") or request.path_info == "/sitemap.xml":
            return {}
        categories = (
            Category.objects.filter(products__is_published=True)
            .order_by("display_order", "name", "pk")
            .distinct()
        )
    return {"navigation_categories": categories}
