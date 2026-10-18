from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("content", "0003_consent_revision_archive"),
        ("integrations", "0004_moysklad_account_api_state_and_failure_time"),
    ]

    operations = [
        migrations.AddField(
            model_name="moyskladsyncstate",
            name="export_consent_revision",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="moysklad_export_approvals", to="content.consentrevision",
            ),
        ),
    ]
