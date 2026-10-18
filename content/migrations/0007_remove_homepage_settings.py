from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("content", "0006_homepagesettings_show_hero_image"),
    ]

    operations = [
        migrations.DeleteModel(name="HomepageSettings"),
    ]
