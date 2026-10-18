from django.db import migrations, models


def preserve_account_pauses(apps, schema_editor):
    """Keep the most conservative saved transport state across environments."""
    ApiState = apps.get_model("integrations", "MoySkladApiState")
    SyncState = apps.get_model("integrations", "MoySkladSyncState")
    using = schema_editor.connection.alias
    for workflow in SyncState.objects.using(using).order_by("pk").iterator():
        state, _ = ApiState.objects.using(using).get_or_create(account_id=workflow.account_id)
        for field in ("api_next_at", "api_paused_until"):
            value = getattr(workflow, field)
            if value is not None and (getattr(state, field) is None or value > getattr(state, field)):
                setattr(state, field, value)
        if workflow.api_lease_expires_at is not None and (
            state.api_lease_expires_at is None or workflow.api_lease_expires_at > state.api_lease_expires_at
        ):
            state.api_lease_token = workflow.api_lease_token
            state.api_lease_expires_at = workflow.api_lease_expires_at
        state.access_error_code = state.access_error_code or workflow.access_error_code
        state.export_paused = state.export_paused or workflow.export_paused
        state.save(using=using)


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0003_moyskladincident_moyskladorderlink_moyskladproduct_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="MoySkladApiState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("account_id", models.UUIDField(unique=True)),
                ("api_next_at", models.DateTimeField(blank=True, null=True)),
                ("api_lease_token", models.UUIDField(blank=True, null=True)),
                ("api_lease_expires_at", models.DateTimeField(blank=True, null=True)),
                ("api_paused_until", models.DateTimeField(blank=True, null=True)),
                ("access_error_code", models.CharField(blank=True, max_length=64)),
                ("export_paused", models.BooleanField(default=False)),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=(
                            models.Q(api_lease_token__isnull=True, api_lease_expires_at__isnull=True)
                            | models.Q(api_lease_token__isnull=False, api_lease_expires_at__isnull=False)
                        ),
                        name="ms_account_api_lease_consistent",
                    ),
                ],
            },
        ),
        migrations.AddField(
            model_name="moyskladproduct",
            name="metadata_updated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="moyskladtask",
            name="first_error_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RemoveConstraint(model_name="moyskladtask", name="ms_task_kind_valid"),
        migrations.AlterField(
            model_name="moyskladtask",
            name="kind",
            field=models.CharField(
                choices=[
                    ("price", "Цена"),
                    ("export_order", "Передача заявки"),
                    ("webhook", "Обработка уведомления"),
                    ("catalog_sync", "Сверка каталога"),
                    ("price_reconciliation", "Сверка цен"),
                    ("status_sync", "Чтение статусов"),
                    ("check_access", "Проверка доступа"),
                ],
                max_length=32,
            ),
        ),
        migrations.AddConstraint(
            model_name="moyskladtask",
            constraint=models.CheckConstraint(
                condition=models.Q(kind__in=(
                    "price", "export_order", "webhook", "catalog_sync", "price_reconciliation", "status_sync", "check_access",
                )),
                name="ms_task_kind_valid",
            ),
        ),
        migrations.RunPython(preserve_account_pauses, migrations.RunPython.noop),
    ]
