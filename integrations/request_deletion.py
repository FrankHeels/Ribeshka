"""Shared permission and notification guards for confirmed request deletion."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from integrations.models import NotificationOutbox


def require_request_deletion(actor, app_label, model_name):
    if (not actor or not actor.is_active or not actor.is_staff or not actor.has_perms((
        f"{app_label}.view_{model_name}", f"{app_label}.delete_{model_name}",
    ))):
        raise PermissionDenied


def locked_request_outbox(**request_identity):
    """The request row must already be locked before acquiring its outbox."""
    rows = NotificationOutbox.objects.select_for_update().filter(**request_identity)
    for row in rows:
        if row.status == "processing" and row.lease_expires_at > timezone.now():
            raise ValidationError("Уведомление ещё отправляется. Дождитесь завершения и повторите удаление.")
    return rows
