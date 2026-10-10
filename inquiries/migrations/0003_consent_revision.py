import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("content", "0003_consent_revision_archive"),
        ("inquiries", "0002_optional_need_and_success_token"),
    ]

    operations = [
        migrations.AddField(
            model_name="inquiry",
            name="privacy_consent_revision",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="wholesale_inquiries",
                to="content.consentrevision",
                verbose_name="Редакция согласия",
            ),
        ),
    ]
