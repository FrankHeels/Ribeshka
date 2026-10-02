from django.db import migrations


PAGE_TITLES = (
    ("home", ""),
    ("about", "О компании"),
    ("delivery", "Доставка и самовывоз"),
    ("contacts", "Контакты"),
    ("privacy", "Политика обработки персональных данных"),
)

HOME_SECTION_KEYS = (
    "hero",
    "categories",
    "products",
    "about",
    "advantage_1",
    "advantage_2",
    "advantage_3",
    "advantage_4",
    "steps",
    "step_1",
    "step_2",
    "step_3",
    "delivery",
    "contacts",
)


def seed_fixed_content(apps, schema_editor):
    ContentPage = apps.get_model("content", "ContentPage")
    ContentSection = apps.get_model("content", "ContentSection")
    database = schema_editor.connection.alias

    pages = {}
    for key, title in PAGE_TITLES:
        page, _ = ContentPage.objects.using(database).get_or_create(
            key=key,
            defaults={"title": title, "seo_title": "", "seo_description": ""},
        )
        pages[key] = page

    for order, key in enumerate(HOME_SECTION_KEYS):
        ContentSection.objects.using(database).get_or_create(
            page=pages["home"],
            key=key,
            defaults={"title": "", "body": "", "display_order": order},
        )
    for page_key in ("about", "delivery", "contacts", "privacy"):
        ContentSection.objects.using(database).get_or_create(
            page=pages[page_key],
            key="lead",
            defaults={"title": "", "body": "", "display_order": 0},
        )


class Migration(migrations.Migration):
    dependencies = [("content", "0001_initial")]

    operations = [migrations.RunPython(seed_fixed_content, migrations.RunPython.noop)]
