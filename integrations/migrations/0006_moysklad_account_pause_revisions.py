from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0005_moysklad_export_consent_revision"),
    ]

    operations = [
        migrations.AddField(
            model_name="moyskladapistate",
            name="access_revision",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="moyskladapistate",
            name="export_pause_revision",
            field=models.PositiveBigIntegerField(default=0),
        ),
    ]
