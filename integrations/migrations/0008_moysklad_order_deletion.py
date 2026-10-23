from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def grant_owner_request_deletion(apps, schema_editor):
    """Extend an existing Owner group without replacing unrelated grants."""
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    using = schema_editor.connection.alias
    group = Group.objects.using(using).filter(name="FishSite Owner").first()
    if group is None:
        return
    for app_label, model_name, name in (
        ("orders", "order", "Can delete розничная заявка"),
        ("inquiries", "inquiry", "Can delete оптовый запрос"),
    ):
        content_type, _ = ContentType.objects.using(using).get_or_create(app_label=app_label, model=model_name)
        permission, _ = Permission.objects.using(using).get_or_create(
            content_type=content_type, codename=f"delete_{model_name}", defaults={"name": name},
        )
        group.permissions.add(permission)


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("orders", "0005_remove_manager_workflow"),
        ("inquiries", "0006_remove_manager_workflow"),
        ("integrations", "0007_moysklad_order_export_approval"),
    ]

    operations = [
        migrations.AddField(
            model_name="moyskladorderlink", name="deletion_requested_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="moyskladorderlink", name="deletion_requested_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name="moysklad_order_deletions", to=settings.AUTH_USER_MODEL),
        ),
        migrations.RemoveConstraint(model_name="moyskladtask", name="ms_task_kind_valid"),
        migrations.RemoveConstraint(model_name="moyskladtask", name="ms_task_permanent_unique"),
        migrations.AlterField(
            model_name="moyskladtask", name="kind",
            field=models.CharField(choices=[
                ("price", "Цена"), ("export_order", "Передача заявки"), ("delete_order", "Удаление заявки"),
                ("webhook", "Обработка уведомления"), ("catalog_sync", "Сверка каталога"),
                ("price_reconciliation", "Сверка цен"), ("status_sync", "Чтение статусов"),
                ("check_access", "Проверка доступа"),
            ], max_length=32),
        ),
        migrations.AddConstraint(
            model_name="moyskladtask", constraint=models.CheckConstraint(
                condition=models.Q(kind__in=("price", "export_order", "delete_order", "webhook", "catalog_sync", "price_reconciliation", "status_sync", "check_access")),
                name="ms_task_kind_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="moyskladtask", constraint=models.UniqueConstraint(
                fields=("account_id", "environment", "kind", "dedupe_key"),
                condition=models.Q(kind__in=("export_order", "delete_order", "webhook")), name="ms_task_permanent_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="moyskladtask", constraint=models.UniqueConstraint(
                fields=("order_link",), condition=models.Q(kind="delete_order", order_link__isnull=False),
                name="ms_task_order_delete_unique",
            ),
        ),
        migrations.RunPython(grant_owner_request_deletion, migrations.RunPython.noop),
    ]
