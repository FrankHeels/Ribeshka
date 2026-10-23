from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0006_product_integration_block_reason_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="product",
            name="is_popular",
            field=models.BooleanField(default=False, verbose_name="Хит продаж"),
        ),
    ]
