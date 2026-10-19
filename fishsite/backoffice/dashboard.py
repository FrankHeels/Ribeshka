from django.core.exceptions import ImproperlyConfigured

from catalog.models import Product
from integrations.moysklad.config import get_config
from inquiries.models import Inquiry
from orders.models import Order


def dashboard_callback(request, context):
    user = request.user
    context["has_retail_access"] = user.has_perm("orders.view_order")
    context["has_wholesale_access"] = user.has_perm("inquiries.view_inquiry")
    context["has_catalog_access"] = user.has_perm("catalog.view_product")
    context["has_moysklad_access"] = user.has_perm("integrations.view_moyskladsyncstate")
    if context["has_moysklad_access"]:
        context["moysklad_configured"] = False
        try:
            config = get_config()
        except ImproperlyConfigured:
            context["moysklad_configuration_error"] = True
        else:
            if config.account_id and config.namespace:
                context["moysklad_configured"] = True
    context["has_content_access"] = user.has_perm("content.view_contentpage")
    if context["has_retail_access"]:
        context["retail_count"] = Order.objects.count()
        context["recent_retail_requests"] = list(
            Order.objects.order_by("-created_at", "-pk").values("pk", "order_number", "created_at")[:5]
        )
    if context["has_wholesale_access"]:
        context["wholesale_count"] = Inquiry.objects.count()
        context["recent_wholesale_requests"] = list(
            Inquiry.objects.order_by("-created_at", "-pk").values("pk", "inquiry_number", "created_at")[:5]
        )
    if context["has_catalog_access"]:
        context["published_products_without_photo"] = (
            Product.objects.filter(is_published=True, images__isnull=True).count()
        )
    return context
