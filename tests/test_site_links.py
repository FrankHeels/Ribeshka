"""Editable links and contact/footer presentation.

Run .venv/bin/python manage.py test tests.test_site_links
--settings=fishsite.settings_test --noinput. Uses isolated PostgreSQL,
temporary image storage and synthetic accounts; no external requests or SMTP.
Migration checks restore the latest schema in finally.
"""
from io import BytesIO
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from PIL import Image

from content.models import ContentPage, SiteLink, SiteSettings
from tests.backoffice_helpers import login_verified_admin


def logo_upload():
    data = BytesIO()
    Image.new("RGB", (32, 32), "gold").save(data, format="PNG")
    return SimpleUploadedFile("test-logo.png", data.getvalue(), content_type="image/png")


class SiteLinksTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.settings = SiteSettings.objects.get(pk=1)

    # A blank URL stays hidden; a named URL has text; an uploaded image replaces
    # that visible text while keeping its accessible name and the original target.
    def test_link_display_modes_and_no_contacts_in_footer(self):
        with TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            SiteLink.objects.create(settings=self.settings, name="Скрытая ссылка", url="")
            SiteLink.objects.create(settings=self.settings, name="Текстовая ссылка", url="https://text.example.test/")
            logo = SiteLink.objects.create(settings=self.settings, name="Логотип ссылки", url="https://logo.example.test/", image=logo_upload())
            self.settings.phone_primary = "+7 000 123-45-67"
            self.settings.phone_secondary = self.settings.phone_primary
            self.settings.opening_hours = "Понедельник — пятница\n09:00–20:00"
            self.settings.email = "seller@example.test"
            self.settings.address = "Тестовый адрес"
            self.settings.save()
            response = self.client.get("/contacts/")
            self.assertNotContains(response, "Скрытая ссылка")
            self.assertContains(response, '>Текстовая ссылка</a>', html=False)
            self.assertContains(response, f'<img src="{logo.image.url}" alt="Логотип ссылки" width="32" height="32" loading="lazy" decoding="async">', html=True)
            self.assertNotContains(response, '>Логотип ссылки</a>')
            self.assertContains(response, 'aria-label="Логотип ссылки"')
            html = response.content.decode()
            footer = html.split('<footer class="site-footer">', 1)[1].split('</footer>', 1)[0]
            for forbidden in ("tel:", "mailto:", self.settings.address, "Текстовая ссылка", "site-social-links"):
                self.assertNotIn(forbidden, footer)
            self.assertContains(response, 'class="contacts-phone"', count=1)
            self.assertContains(response, 'class="contacts-schedule"')
            left = footer.split('aria-label="Разделы сайта">', 1)[1].split('</nav>', 1)[0]
            right = footer.split('aria-label="Правовая информация">', 1)[1].split('</nav>', 1)[0]
            labels = ("Ассортимент", "О компании", "Доставка", "Контакты")
            self.assertEqual(sorted(labels, key=left.index), list(labels))
            self.assertEqual(left.count('<a '), 4)
            self.assertNotIn('Доставка и самовывоз', left)
            self.assertNotIn('/wholesale/', footer)
            self.assertEqual(right.count('<a '), 3)
            self.assertIn('Политика обработки персональных данных', right)
            self.assertIn('Согласие на обработку персональных данных', right)
            self.assertIn('Согласие на Яндекс Метрику', right)
            header = html.split('<header class="site-header">', 1)[1].split('</header>', 1)[0]
            self.assertIn('>Опт</a>', header)
            self.assertNotIn('Оптовый запрос', header)
            self.assertNotIn('<span>Корзина</span>', header)
            self.assertIn('aria-label="Корзина', header)
            home = self.client.get("/")
            self.assertContains(home, 'https://text.example.test/')
            self.assertContains(home, logo.image.url)

    # Existing settings permission is sufficient for link add/upload/delete;
    # links do not require a second settings page or a new owner-role assignment.
    def test_owner_can_add_upload_and_delete_link_in_settings(self):
        editor = get_user_model().objects.create_user(username="links-editor", is_staff=True)
        editor.user_permissions.set(Permission.objects.filter(
            content_type__app_label="content", codename__in=("view_sitesettings", "change_sitesettings")))
        login_verified_admin(self.client, editor)
        url = reverse("admin:content_sitesettings_change", args=(1,))
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("whatsapp_url", response.context["adminform"].form.fields)
        self.assertEqual(response.context["inline_admin_formsets"][0].formset.prefix, "links")
        fields = ("phone_primary", "phone_secondary", "email", "address", "opening_hours", "footer_notice")
        data = {field: getattr(self.settings, field) for field in fields}
        data.update({"links-TOTAL_FORMS": "1", "links-INITIAL_FORMS": "0",
                     "links-MIN_NUM_FORMS": "0", "links-MAX_NUM_FORMS": "1000",
                     "links-0-name": "Мессенджер", "links-0-url": "https://example.test/chat"})
        with TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            response = self.client.post(url, {**data, "links-0-image": logo_upload()})
            self.assertEqual(response.status_code, 302, response.context and response.context.get("errors"))
            link = SiteLink.objects.get(name="Мессенджер")
            self.assertTrue(link.image)
            response = self.client.post(url, {**data, "links-INITIAL_FORMS": "1", "links-0-id": link.pk,
                                              "links-0-DELETE": "on"})
            self.assertEqual(response.status_code, 302)
            self.assertFalse(SiteLink.objects.filter(pk=link.pk).exists())

    # A view-only staff member cannot mutate links through a forged formset.
    def test_view_only_staff_cannot_change_links(self):
        viewer = get_user_model().objects.create_user(username="links-viewer", is_staff=True)
        viewer.user_permissions.set(Permission.objects.filter(content_type__app_label="content", codename="view_sitesettings"))
        login_verified_admin(self.client, viewer)
        response = self.client.post(reverse("admin:content_sitesettings_change", args=(1,)), {
            "links-TOTAL_FORMS": "1", "links-INITIAL_FORMS": "0",
            "links-0-name": "Подмена", "links-0-url": "https://example.test/"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(SiteLink.objects.filter(name="Подмена").exists())

    # User-supplied links and files retain the site's existing URL/image checks.
    def test_unsafe_target_and_non_image_upload_are_rejected(self):
        with self.assertRaises(ValidationError):
            SiteLink(settings=self.settings, name="Небезопасно", url="javascript:alert(1)").save()
        with self.assertRaises(ValidationError):
            SiteLink(settings=self.settings, name="SVG", url="https://example.test/",
                     image=SimpleUploadedFile("logo.svg", b'<svg onload="alert(1)"/>', content_type="image/svg+xml")).save()


class SiteLinksMigrationTests(TransactionTestCase):
    # Previously configured URLs survive the transition; empty legacy fields
    # do not become visible links, and other settings/page text remain intact.
    def test_existing_urls_are_migrated(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        before = [node for node in latest if node[0] != "content"] + [("content", "0008_single_information_page_text")]
        try:
            executor.migrate(before)
            old_apps = executor.loader.project_state(before).apps
            Settings = old_apps.get_model("content", "SiteSettings")
            Settings.objects.update_or_create(pk=1, defaults={
                "whatsapp_url": "https://example.test/wa", "telegram_url": "https://example.test/tg",
                "vk_url": "", "phone_primary": "0000000000", "footer_copy": "Архивный текст"})
            old_apps.get_model("content", "ContentPage").objects.update_or_create(key="contacts", defaults={"body": "Сохранённый текст"})
            executor = MigrationExecutor(connection)
            executor.migrate(latest)
            new_apps = executor.loader.project_state(latest).apps
            self.assertEqual(list(new_apps.get_model("content", "SiteLink").objects.values_list("name", "url")), [
                ("WhatsApp", "https://example.test/wa"), ("Telegram", "https://example.test/tg")])
            saved = new_apps.get_model("content", "SiteSettings").objects.get(pk=1)
            self.assertEqual(saved.phone_primary, "0000000000")
            self.assertEqual(saved.footer_copy, "Архивный текст")
            self.assertEqual(new_apps.get_model("content", "ContentPage").objects.get(key="contacts").body, "Сохранённый текст")
        finally:
            MigrationExecutor(connection).migrate(latest)
