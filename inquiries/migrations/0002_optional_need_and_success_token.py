import uuid

from django.db import migrations, models


def populate_success_tokens(apps, schema_editor):
    Inquiry = apps.get_model("inquiries", "Inquiry")
    database = schema_editor.connection.alias
    for inquiry in Inquiry.objects.using(database).filter(success_token__isnull=True).iterator():
        Inquiry.objects.using(database).filter(pk=inquiry.pk).update(success_token=uuid.uuid4())


class Migration(migrations.Migration):

    dependencies = [
        ("inquiries", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="inquiry",
            name="need",
            field=models.TextField(blank=True, default="", verbose_name="Описание потребности"),
        ),
        migrations.AddField(
            model_name="inquiry",
            name="success_token",
            field=models.UUIDField(null=True, editable=False, verbose_name="Токен страницы успеха"),
        ),
        migrations.RunPython(populate_success_tokens, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="inquiry",
            name="success_token",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True, verbose_name="Токен страницы успеха"),
        ),
    ]
