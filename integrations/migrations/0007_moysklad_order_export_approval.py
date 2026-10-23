from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("integrations", "0006_moysklad_account_pause_revisions"),
    ]

    operations = [
        migrations.AddField(
            model_name="moyskladorderlink",
            name="approved_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="moyskladorderlink",
            name="approved_by",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="moysklad_order_approvals", to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
