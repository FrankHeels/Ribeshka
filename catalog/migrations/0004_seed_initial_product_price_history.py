from django.db import migrations
from django.utils import timezone


def seed_price_history(apps, schema_editor):
    Product = apps.get_model("catalog", "Product")
    ProductPriceChange = apps.get_model("catalog", "ProductPriceChange")
    database = schema_editor.connection.alias
    changed_at = timezone.now()

    products = list(Product.objects.using(database).filter(price_revision=0).only("pk", "price_per_kg"))
    ProductPriceChange.objects.using(database).bulk_create(
        [
            ProductPriceChange(
                product_id=product.pk,
                previous_price=None,
                new_price=product.price_per_kg,
                revision=1,
                source="manual",
                changed_by_id=None,
                changed_at=changed_at,
            )
            for product in products
        ],
        batch_size=500,
    )
    Product.objects.using(database).filter(pk__in=[product.pk for product in products]).update(
        price_revision=1,
        price_changed_at=changed_at,
        price_source="manual",
    )


class Migration(migrations.Migration):
    dependencies = [("catalog", "0003_alter_product_options_category_seo_description_and_more")]

    operations = [migrations.RunPython(seed_price_history, migrations.RunPython.noop)]
