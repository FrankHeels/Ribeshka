from django.db import migrations


HERO_VALUES = (
    ("hero_value_1", "Сырье высочайшего качества"),
    ("hero_value_2", "Собственное производство"),
    ("hero_value_3", "Новейшие стандарты"),
)


def seed_owner_content(apps, schema_editor):
    ContentPage = apps.get_model("content", "ContentPage")
    ContentSection = apps.get_model("content", "ContentSection")
    HomepageSettings = apps.get_model("content", "HomepageSettings")
    SiteSettings = apps.get_model("content", "SiteSettings")
    HomepageRecommendation = apps.get_model("content", "HomepageRecommendation")
    Product = apps.get_model("catalog", "Product")
    database = schema_editor.connection.alias

    HomepageSettings.objects.using(database).get_or_create(
        pk=1,
        defaults={"button_label": "Перейти к ассортименту", "button_url": "#assortment"},
    )
    SiteSettings.objects.using(database).get_or_create(pk=1)

    home = ContentPage.objects.using(database).filter(key="home").first()
    if home:
        for order, (key, title) in enumerate(HERO_VALUES, start=14):
            section, created = ContentSection.objects.using(database).get_or_create(
                page_id=home.pk,
                key=key,
                defaults={"title": title, "body": "", "display_order": order},
            )
            if not created and not section.title:
                section.title = title
                section.display_order = order
                section.save(using=database, update_fields=("title", "display_order"))

    product_ids = list(
        Product.objects.using(database)
        .filter(is_published=True)
        .order_by("name", "pk")
        .values_list("pk", flat=True)[:2]
    )
    for slot in (1, 2):
        HomepageRecommendation.objects.using(database).get_or_create(
            slot=slot,
            defaults={"product_id": product_ids[slot - 1] if len(product_ids) >= slot else None},
        )


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0004_seed_initial_product_price_history"),
        ("content", "0004_contentsection_image_contentsection_image_alt_and_more"),
    ]

    operations = [migrations.RunPython(seed_owner_content, migrations.RunPython.noop)]
