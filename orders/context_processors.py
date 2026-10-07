from django.contrib import messages
from django.urls import reverse

from .cart.state import CART_ISSUES_MESSAGE, cart_count as count_cart_lines


def cart_count(request):
    """Expose the number of distinct published products in the session cart."""
    if request.path_info.startswith("/admin/") or request.path_info == "/sitemap.xml":
        return {}

    count = count_cart_lines(request)
    state = getattr(request, "_retail_cart_state", None)
    cart_paths_with_inline_notice = {reverse("orders:cart"), reverse("orders:checkout")}
    if (
        state is not None
        and state.has_issues
        and request.path_info not in cart_paths_with_inline_notice
        and hasattr(request, "_messages")
        and not getattr(request, "_cart_issues_messaged", False)
    ):
        messages.warning(request, CART_ISSUES_MESSAGE)
        request._cart_issues_messaged = True
    return {"cart_count": count}
