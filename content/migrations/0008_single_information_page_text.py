from django.db import migrations, models


def copy_information_text(apps, schema_editor):
    Page = apps.get_model("content", "ContentPage")
    Section = apps.get_model("content", "ContentSection")
    database = schema_editor.connection.alias
    for page in Page.objects.using(database).filter(key__in=("home", "about", "delivery", "contacts")):
        if page.body:
            continue
        sections = list(Section.objects.using(database).filter(page_id=page.pk).order_by("display_order", "pk"))
        if page.key == "home":
            sections = [section for section in sections if section.key == "about"]
        else:
            sections.sort(key=lambda section: section.key != "lead")
        paragraphs = [
            text.strip()
            for section in sections
            for text in (section.title, section.body)
            if text.strip()
        ]
        Page.objects.using(database).filter(pk=page.pk).update(body="\n\n".join(paragraphs))


class Migration(migrations.Migration):
    dependencies = [("content", "0007_remove_homepage_settings")]

    operations = [
        migrations.AddField(
            model_name="contentpage", name="body",
            field=models.TextField("Текст страницы", blank=True),
        ),
        # Retain legacy sections as an archive; editor and public pages use body.
        migrations.RunPython(copy_information_text, migrations.RunPython.noop),
    ]
