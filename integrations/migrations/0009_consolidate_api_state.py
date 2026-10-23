from django.db import migrations


API_FIELDS = (
    "api_next_at", "api_lease_token", "api_lease_expires_at",
    "api_paused_until", "access_error_code", "export_paused",
)


def consolidate_api_state(apps, schema_editor):
    """Retain the most conservative account state before removing its copies."""
    ApiState = apps.get_model("integrations", "MoySkladApiState")
    SyncState = apps.get_model("integrations", "MoySkladSyncState")
    using = schema_editor.connection.alias
    for workflow in SyncState.objects.using(using).order_by("pk").iterator():
        state, _ = ApiState.objects.using(using).get_or_create(account_id=workflow.account_id)
        for field in ("api_next_at", "api_paused_until"):
            value = getattr(workflow, field)
            current = getattr(state, field)
            if value is not None and (current is None or value > current):
                setattr(state, field, value)
        # Keep the token and deadline together. The existing API lease wins ties.
        if workflow.api_lease_expires_at is not None and (
            state.api_lease_expires_at is None or workflow.api_lease_expires_at > state.api_lease_expires_at
        ):
            state.api_lease_token = workflow.api_lease_token
            state.api_lease_expires_at = workflow.api_lease_expires_at
        state.access_error_code = state.access_error_code or workflow.access_error_code
        state.export_paused = state.export_paused or workflow.export_paused
        state.save(using=using, update_fields=API_FIELDS)


def restore_workflow_api_state(apps, schema_editor):
    """Older code must see the current account pauses after a rollback."""
    ApiState = apps.get_model("integrations", "MoySkladApiState")
    SyncState = apps.get_model("integrations", "MoySkladSyncState")
    using = schema_editor.connection.alias
    for state in ApiState.objects.using(using).iterator():
        SyncState.objects.using(using).filter(account_id=state.account_id).update(
            **{field: getattr(state, field) for field in API_FIELDS},
        )


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0008_moysklad_order_deletion"),
    ]

    operations = [
        migrations.RunPython(consolidate_api_state, restore_workflow_api_state),
        migrations.RemoveConstraint(model_name="moyskladsyncstate", name="ms_api_lease_consistent"),
        migrations.RemoveField(model_name="moyskladsyncstate", name="api_next_at"),
        migrations.RemoveField(model_name="moyskladsyncstate", name="api_lease_token"),
        migrations.RemoveField(model_name="moyskladsyncstate", name="api_lease_expires_at"),
        migrations.RemoveField(model_name="moyskladsyncstate", name="api_paused_until"),
        migrations.RemoveField(model_name="moyskladsyncstate", name="access_error_code"),
        migrations.RemoveField(model_name="moyskladsyncstate", name="export_paused"),
    ]
