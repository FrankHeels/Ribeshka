"""Owner-confirmed deletion, retaining uncertain external orders until resolved."""

from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import transaction
from django.utils import timezone

from integrations.models import MoySkladOrderLink, MoySkladTask
from integrations.moysklad.config import get_config
from integrations.moysklad.queue import enqueue_task
from integrations.request_deletion import locked_request_outbox, require_request_deletion

from ..models import Order


def validate_deletion_target(link):
    """A frozen identity can only be deleted using the same configured target."""
    try:
        config = get_config()
    except ImproperlyConfigured:
        raise ValidationError("Проверьте серверную конфигурацию МойСклад перед удалением.") from None
    if (not config.account_id or not config.namespace
            or link.account_id != config.account_id or link.environment != config.environment
            or link.config_snapshot.get("account_id") != str(link.account_id)
            or link.config_snapshot.get("environment") != link.environment
            or link.config_snapshot.get("namespace") != str(config.namespace)):
        raise ValidationError("Заявка относится к другой конфигурации обмена. Нужна проверка администратора.")
    return config


def locked_order_work(link):
    tasks = MoySkladTask.objects.select_for_update().filter(order_link=link).order_by("pk")
    for task in tasks:
        if task.kind != "delete_order" and task.status == "processing" and task.lease_expires_at > timezone.now():
            raise ValidationError("Обмен ещё выполняется. Дождитесь завершения и повторите удаление.")
    return tasks


def purge_order(order, link, *, tasks, outbox):
    """Call only while holding Order, link, task and outbox locks in that order."""
    outbox.delete()
    if link is not None:
        from integrations.moysklad.incidents import retire_deleted_order_incidents

        retire_deleted_order_incidents(account_id=link.account_id, environment=link.environment,
                                       order_number=order.order_number)
        tasks.delete()
        link.delete()
    order.delete()


def request_order_deletion(order_id, *, actor):
    require_request_deletion(actor, "orders", "order")
    with transaction.atomic():
        order = Order.objects.select_for_update().filter(pk=order_id).first()
        if order is None:
            return "deleted"
        link = MoySkladOrderLink.objects.select_for_update().filter(order=order).first()
        tasks = locked_order_work(link) if link else MoySkladTask.objects.none()
        outbox = locked_request_outbox(order=order)
        if link is None or (not link.attempted and not link.external_id):
            purge_order(order, link, tasks=tasks, outbox=outbox)
            return "deleted"

        validate_deletion_target(link)
        now = timezone.now()
        first_request = link.deletion_requested_at is None
        if first_request:
            link.deletion_requested_at, link.deletion_requested_by = now, actor
            link.audit_log = [*link.audit_log, {
                "action": "request_deletion", "actor": actor.pk, "at": now.isoformat(),
            }]
            link.last_error_code = ""
            link.save(update_fields=("deletion_requested_at", "deletion_requested_by", "audit_log", "last_error_code", "updated_at"))
        # A pending email and stale exchange tasks must never run after this
        # decision. An active SMTP/API lease was rejected before any mutation.
        outbox.delete()
        tasks.exclude(kind="delete_order").delete()
        task = enqueue_task(
            kind="delete_order", dedupe_key=f"delete:{order.pk}",
            account_id=link.account_id, environment=link.environment,
            order_link=link, object_id=str(order.pk),
        )
        if task.status in ("failed", "manual_review"):
            task.status, task.lease_token, task.lease_expires_at = "pending", None, None
            task.next_attempt_at, task.completed_at = now, None
            task.last_error_code, task.first_error_at = "", None
            task.save(update_fields=("status", "lease_token", "lease_expires_at", "next_attempt_at", "completed_at", "last_error_code", "first_error_at", "updated_at"))
            link.last_error_code = ""
            link.audit_log = [*link.audit_log, {"action": "retry_deletion", "actor": actor.pk, "at": now.isoformat()}]
            link.save(update_fields=("last_error_code", "audit_log", "updated_at"))
        return "queued"
