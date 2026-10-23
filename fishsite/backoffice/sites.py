from django.urls import path
from django.utils.translation import gettext_lazy as _
from two_factor.admin import AdminSiteOTPRequiredMixin
from unfold.sites import UnfoldAdminSite


class FishSiteAdminSite(AdminSiteOTPRequiredMixin, UnfoldAdminSite):
    site_header = _("FishSite — управление сайтом")
    site_title = _("FishSite")
    index_title = _("Обзор сайта")
    index_template = "admin/index.html"

    def get_urls(self):
        from .views import preview_product
        from .moysklad_views import (
            moysklad_action, moysklad_candidate_action, moysklad_candidates,
            moysklad_order_action, moysklad_order_export, moysklad_status, moysklad_technical, moysklad_unblock,
        )

        return [
            path("moysklad/", self.admin_view(moysklad_status), name="moysklad_status"),
            path("moysklad/technical/", self.admin_view(moysklad_technical), name="moysklad_technical"),
            path("moysklad/action/<str:action>/", self.admin_view(moysklad_action), name="moysklad_action"),
            path("moysklad/candidates/", self.admin_view(moysklad_candidates), name="moysklad_candidates"),
            path("moysklad/candidates/<uuid:external_id>/<str:operation>/", self.admin_view(moysklad_candidate_action), name="moysklad_candidate_action"),
            path("moysklad/products/<int:product_id>/unblock/", self.admin_view(moysklad_unblock), name="moysklad_unblock"),
            path("moysklad/orders/<int:order_id>/send/", self.admin_view(moysklad_order_export), name="moysklad_order_export"),
            path("moysklad/orders/<int:link_id>/<str:operation>/", self.admin_view(moysklad_order_action), name="moysklad_order_action"),
            path("catalog/product/<int:product_id>/preview/", self.admin_view(preview_product), name="product_preview"),
        ] + super().get_urls()
