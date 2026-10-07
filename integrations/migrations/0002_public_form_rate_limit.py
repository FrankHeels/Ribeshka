from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="PublicFormRateLimit",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "bucket_key",
                    models.CharField(
                        editable=False,
                        max_length=64,
                        unique=True,
                        verbose_name="Ключ ограничения",
                    ),
                ),
                ("window_started_at", models.DateTimeField(verbose_name="Начало окна")),
                ("request_count", models.PositiveIntegerField(default=0, verbose_name="Число запросов")),
            ],
            options={
                "verbose_name": "ограничение публичной формы",
                "verbose_name_plural": "ограничения публичных форм",
            },
        ),
    ]
