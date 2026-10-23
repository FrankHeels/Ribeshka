"""Admin confirmation shared by immutable retail and wholesale requests."""

from django.contrib import messages
from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.http import HttpResponseNotAllowed
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect

from integrations.moysklad.contracts import MoySkladError
from integrations.request_deletion import require_request_deletion


class RequestDeletionAdminMixin:
    actions = None

    def render_change_form(self, request, context, add=False, change=False, form_url="", obj=None):
        context["show_delete"] = False
        return super().render_change_form(request, context, add=add, change=change, form_url=form_url, obj=obj)

    def has_delete_permission(self, request, obj=None):
        try:
            require_request_deletion(request.user, self.opts.app_label, self.opts.model_name)
        except PermissionDenied:
            return False
        return True

    def delete_model(self, request, obj):
        # Only the explicit service can safely coordinate protected work.
        raise PermissionDenied

    def delete_queryset(self, request, queryset):
        raise PermissionDenied

    @method_decorator(csrf_protect)
    def delete_view(self, request, object_id, extra_context=None):
        require_request_deletion(request.user, self.opts.app_label, self.opts.model_name)
        if not self.admin_site.has_permission(request):
            raise PermissionDenied
        if request.method not in ("GET", "POST"):
            return HttpResponseNotAllowed(("GET", "POST"))
        obj = self.get_object(request, object_id)
        if obj is not None and (not self.has_view_permission(request, obj) or not self.has_delete_permission(request, obj)):
            raise PermissionDenied
        list_url = reverse(f"{self.admin_site.name}:{self.opts.app_label}_{self.opts.model_name}_changelist")
        if obj is None and request.method == "GET":
            return redirect(list_url)
        error = ""
        if request.method == "POST":
            if request.POST.get("confirm") != "yes":
                error = "Подтвердите удаление заявки."
            else:
                try:
                    result = self.request_deletion_service(object_id, actor=request.user)
                except ValidationError as exc:
                    error = " ".join(exc.messages)
                except (ImproperlyConfigured, MoySkladError):
                    error = "Удаление не выполнено. Проверьте конфигурацию обмена и повторите действие."
                else:
                    messages.success(request, "Заявка удалена." if result == "deleted" else
                                     "Удаление поставлено в очередь. Заявка исчезнет после подтверждения МойСклад.")
                    if result == "queued":
                        return redirect(f"{self.admin_site.name}:{self.opts.app_label}_{self.opts.model_name}_change", object_id)
                    return redirect(list_url)
        link = getattr(obj, "moysklad_link", None) if obj else None
        context = {
            **self.admin_site.each_context(request), **(extra_context or {}),
            "title": "Удалить заявку", "opts": self.opts, "original": obj,
            "object": obj, "error": error, "list_url": list_url,
            "change_url": reverse(f"{self.admin_site.name}:{self.opts.app_label}_{self.opts.model_name}_change", args=(object_id,)) if obj else list_url,
            "remote_deletion": bool(link and (link.attempted or link.external_id)),
            "deletion_requested": bool(link and link.deletion_requested_at),
        }
        return TemplateResponse(request, "admin/requests/delete_confirmation.html", context,
                                status=400 if error else 200)
