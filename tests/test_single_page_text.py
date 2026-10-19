"""One text editor per page, safe public output, and legacy text preservation.

Run with .venv/bin/python manage.py test tests.test_single_page_text
--settings=fishsite.settings_test --noinput. Uses only isolated PostgreSQL;
no external APIs, SMTP or primary database. Migration tests restore latest schema.
"""

from importlib import import_module
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse

from content.models import ContentPage, SiteSettings
from tests.backoffice_helpers import login_verified_admin


class SinglePageTextTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.editor = get_user_model().objects.create_user(username="single-text-editor", is_staff=True)
        cls.editor.user_permissions.set(Permission.objects.filter(content_type__app_label="content"))

    # Every editable page has one body textarea, no section formsets/upload/order
    # controls. Saving works without formset metadata and escapes supplied HTML.
    def test_owner_edits_one_text_on_home_and_information_pages(self):
        login_verified_admin(self.client, self.editor)
        for key, path in (("home", "/"), ("about", "/about/"),
                          ("delivery", "/delivery/")):
            with self.subTest(key=key):
                page = ContentPage.objects.get(key=key)
                url = reverse("admin:content_contentpage_change", args=(page.pk,))
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["inline_admin_formsets"], [])
                self.assertEqual(set(response.context["adminform"].form.fields),
                                 {"body", "title", "seo_title", "seo_description"})
                self.assertNotContains(response, 'name="sections-TOTAL_FORMS"')
                self.assertNotContains(response, 'type="file"')
                self.assertContains(response, '<details>')
                text = f"Содержимое {key}\n\n<script>не выполнять</script>"
                response = self.client.post(url, {
                    "body": text, "title": page.title,
                    "seo_title": page.seo_title, "seo_description": page.seo_description,
                })
                self.assertEqual(response.status_code, 302)
                page.refresh_from_db()
                self.assertEqual(page.body, text)
                public = Client().get(path)
                self.assertContains(public, f"Содержимое {key}", count=1)
                self.assertContains(public, "&lt;script&gt;")
                self.assertNotContains(public, "<script>не выполнять</script>")
                if key == "delivery":
                    self.assertContains(public, 'data-delivery-map')

    # Contacts are managed only in general settings. Their archived page record
    # stays intact, is excluded from the list and cannot be edited via an old URL.
    def test_contacts_are_removed_from_page_editor_without_deleting_data(self):
        page = ContentPage.objects.get(key="contacts")
        page.body = "Архивный текст контактов"
        page.save()
        SiteSettings.objects.update_or_create(pk=1, defaults={"phone_primary": "+7 000 123-45-67"})
        login_verified_admin(self.client, self.editor)
        response = self.client.get(reverse("admin:content_contentpage_changelist"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("contacts", response.context["cl"].queryset.values_list("key", flat=True))
        url = reverse("admin:content_contentpage_change", args=(page.pk,))
        self.assertEqual(self.client.get(url).status_code, 302)
        self.assertEqual(self.client.post(url, {"body": "Подмена", "title": "Подмена"}).status_code, 302)
        page.refresh_from_db()
        self.assertEqual(page.body, "Архивный текст контактов")
        public = Client().get("/contacts/")
        self.assertEqual(public.status_code, 200)
        self.assertContains(public, "+7 000 123-45-67")
        self.assertNotContains(public, "Архивный текст контактов")


class SinglePageTextMigrationTests(TransactionTestCase):
    # Migrate real historical rows, preserve lead-first order and paragraphs,
    # leave source sections/images intact, and keep unrelated home blocks intact.
    def test_legacy_text_is_copied_without_destroying_source_or_legal_content(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        before = [node for node in latest if node[0] != "content"] + [
            ("content", "0007_remove_homepage_settings")]
        try:
            executor.migrate(before)
            old_apps = executor.loader.project_state(before).apps
            Page = old_apps.get_model("content", "ContentPage")
            Section = old_apps.get_model("content", "ContentSection")
            page, _ = Page.objects.get_or_create(key="contacts")
            Section.objects.update_or_create(page=page, key="lead", defaults={
                "title": "Вступление", "body": "Контакты\nс переносом", "display_order": 200})
            Section.objects.create(page=page, key="early", title="Адрес", body="Первый текст", display_order=1)
            Section.objects.create(page=page, key="late", body="Последний текст", display_order=2,
                                   image="content/sections/preserved.jpg", image_alt="Сохранённое фото")
            home, _ = Page.objects.get_or_create(key="home")
            Section.objects.update_or_create(page=home, key="about", defaults={"body": "Текст о компании на главной"})
            privacy, _ = Page.objects.get_or_create(key="privacy")
            Section.objects.update_or_create(page=privacy, key="lead", defaults={"body": "Архивный текст политики"})
            source = list(Section.objects.order_by("pk").values())

            executor = MigrationExecutor(connection)
            executor.migrate(latest)
            new_apps = executor.loader.project_state(latest).apps
            NewPage = new_apps.get_model("content", "ContentPage")
            self.assertEqual(NewPage.objects.get(pk=page.pk).body,
                             "Вступление\n\nКонтакты\nс переносом\n\nАдрес\n\nПервый текст\n\nПоследний текст")
            self.assertEqual(NewPage.objects.get(pk=home.pk).body, "Текст о компании на главной")
            self.assertEqual(NewPage.objects.get(pk=privacy.pk).body, "")
            self.assertEqual(list(new_apps.get_model("content", "ContentSection").objects.order_by("pk").values()), source)
            NewPage.objects.filter(pk=page.pk).update(body="Новая редакция владельца")
            migration = import_module("content.migrations.0008_single_information_page_text")
            migration.copy_information_text(new_apps, SimpleNamespace(connection=connection))
            self.assertEqual(NewPage.objects.get(pk=page.pk).body, "Новая редакция владельца")
        finally:
            MigrationExecutor(connection).migrate(latest)
