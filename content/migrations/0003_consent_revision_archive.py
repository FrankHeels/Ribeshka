from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("content", "0002_seed_fixed_pages"),
    ]

    operations = [
        migrations.CreateModel(
            name="ConsentRevision",
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
                ("revision_id", models.CharField(max_length=71, unique=True, verbose_name="Идентификатор редакции")),
                ("sha256", models.CharField(max_length=64, unique=True, verbose_name="SHA-256 исходного Markdown")),
                ("text", models.TextField(verbose_name="Текст исходной редакции")),
                ("registered_at", models.DateTimeField(auto_now_add=True, verbose_name="Зарегистрирована")),
            ],
            options={
                "verbose_name": "редакция согласия на обработку данных",
                "verbose_name_plural": "архив редакций согласия на обработку данных",
                "ordering": ("-registered_at", "-pk"),
            },
        ),
    ]
