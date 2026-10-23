"""Removal of obsolete homepage cover settings preserves the editable CMS."""

# Запуск: `.venv/bin/python manage.py test tests.test_homepage_cover_removal --settings=fishsite.settings_test --noinput`.
# Нужны зависимости проекта и отдельный PostgreSQL fishsite-tests на 127.0.0.1:15432,
# подготовленный по docs/test-environment.md. Runner создаёт и удаляет test_fishsite;
# основная БД, SMTP и внешние API не используются. Данные создаются внутри тестов.
# SimpleTestCase проверяет шаблон и регистрацию без БД; TestCase откатывает CMS и
# права владельца; TransactionTestCase меняет историческую схему через MigrationExecutor
# и в finally возвращает все последние миграции. Файл обложки создаётся во временном MEDIA_ROOT.

from decimal import Decimal
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from django.apps import apps
from django.conf import settings
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import NoReverseMatch, reverse

from catalog.models import Category, Product
from content.models import ContentPage, ContentSection, HomepageRecommendation, SiteSettings
from fishsite.backoffice.management.commands.sync_backoffice_roles import OWNER_PERMISSIONS
from tests.backoffice_helpers import login_verified_admin


class HeroActionParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.actions = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and "button-primary" in attrs.get("class", "").split():
            self.current = [attrs.get("href"), ""]

    def handle_data(self, data):
        if self.current is not None:
            self.current[1] += data

    def handle_endtag(self, tag):
        if tag == "a" and self.current is not None:
            self.actions.append((self.current[0], " ".join(self.current[1].split())))
            self.current = None


class HomepageCoverTemplateTests(SimpleTestCase):
    # Передать прежние настройки напрямую в шаблон: фото игнорируется, а ссылка
    # и надпись основной кнопки всегда ведут к ассортименту с фиксированным текстом.
    def test_legacy_context_cannot_restore_cover_photo_or_override_action(self):
        legacy_photo = "/media/content/homepage/legacy-cover.jpg"
        rendered = render_to_string("home.html", {
            "hero_photo": SimpleNamespace(url=legacy_photo),
            "homepage_settings": SimpleNamespace(hero_image_alt="Старая обложка"),
            "hero_button_url": "https://legacy.example.test/offer",
            "hero_button_label": "Устаревший призыв",
        })
        parser = HeroActionParser()
        parser.feed(rendered)
        with self.subTest(behavior="photo"):
            self.assertNotIn(legacy_photo, rendered)
        with self.subTest(behavior="action"):
            self.assertEqual(parser.actions, [("#assortment", "Перейти к ассортименту")])

    # Текущая модель, admin route, регистрация, меню и спецификация роли больше
    # не должны предлагать удалённую настройку независимо от старых данных в БД.
    def test_obsolete_cover_is_absent_from_runtime_and_backoffice(self):
        with self.subTest(surface="apps registry"):
            with self.assertRaises(LookupError):
                apps.get_model("content", "HomepageSettings")
        with self.subTest(surface="admin registry"):
            self.assertNotIn("content.homepagesettings", {
                model._meta.label_lower for model in admin.site._registry
            })
        with self.subTest(surface="named admin route"):
            with self.assertRaises(NoReverseMatch):
                reverse("admin:content_homepagesettings_change", args=(1,))
        with self.subTest(surface="sidebar"):
            items = [
                item for group in settings.UNFOLD["SIDEBAR"]["navigation"]
                for item in group.get("items", ())
            ]
            self.assertFalse(any(
                "homepagesettings" in str(item.get("link", ""))
                or str(item.get("title", "")).casefold() == "обложка главной"
                for item in items
            ))
        with self.subTest(surface="owner permission specification"):
            self.assertFalse(any(
                "homepagesettings" in codename
                for codenames in OWNER_PERMISSIONS.values() for codename in codenames
            ))


class HomepageContentPreservationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.home, _ = ContentPage.objects.update_or_create(
            key="home", defaults={"title": "Наш редактируемый заголовок", "body": "Наш единый текст главной"},
        )
        cls.hero, _ = ContentSection.objects.update_or_create(
            page=cls.home, key="hero",
            defaults={"title": "Редактируемое вступление", "body": "Наш текст первого экрана"},
        )
        cls.benefits = ["Качество владельца", "Производство владельца", "Стандарты владельца"]
        for index, title in enumerate(cls.benefits, start=1):
            ContentSection.objects.update_or_create(
                page=cls.home, key=f"hero_value_{index}", defaults={"title": title},
            )
        cls.site, _ = SiteSettings.objects.update_or_create(
            pk=1, defaults={"phone_primary": "+7 000 123-45-67"},
        )
        category = Category.objects.create(name="Рыба для главной", slug="cover-removal-fish")
        cls.product = Product.objects.create(
            name="Рекомендованная рыба", slug="cover-removal-product", category=category,
            price_per_kg=Decimal("321.00"), is_published=True,
        )
        HomepageRecommendation.objects.update_or_create(slot=1, defaults={"product": cls.product})
        HomepageRecommendation.objects.update_or_create(slot=2, defaults={"product": None})

    # GET главной сохраняет заголовок, единый текст и товар; телефон доступен
    # на странице контактов и не дублируется на главной. Старые context-поля
    # и запрос к таблице настроек обложки больше не нужны.
    def test_home_keeps_editable_content_without_obsolete_context_or_query(self):
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 200)
        for text in [self.home.title, self.home.body]:
            self.assertContains(response, text)
        self.assertNotContains(response, self.site.phone_primary)
        contacts_url = reverse("content:contacts")
        self.assertContains(response, f'href="{contacts_url}"')
        self.assertContains(self.client.get(contacts_url), self.site.phone_primary)
        self.assertEqual(response.context["featured_products"], [self.product])
        self.assertContains(response, self.product.name)
        self.assertContains(response, "Рекомендуемые товары")
        self.assertNotContains(response, 'class="empty-state-feature"')
        with self.subTest(behavior="context"):
            for key in ("homepage_settings", "hero_photo", "hero_button_label", "hero_button_url"):
                with self.subTest(key=key):
                    self.assertNotIn(key, response.context)
        with self.subTest(behavior="queries"):
            self.assertFalse(any(
                "content_homepagesettings" in query["sql"].lower() for query in queries
            ), "Главная всё ещё обращается к удаляемой таблице content_homepagesettings.")

    # Обычный владелец после синхронизации роли продолжает открывать формы страницы
    # с единым текстом, общие настройки и выбор рекомендуемых товаров через OTP-admin.
    def test_owner_keeps_access_to_page_sections_site_settings_and_recommendations(self):
        owner = get_user_model().objects.create_user(username="cover-removal-owner")
        call_command("sync_backoffice_roles", owner.username, stdout=StringIO())
        owner.refresh_from_db()
        login_verified_admin(self.client, owner)

        page_response = self.client.get(reverse("admin:content_contentpage_change", args=(self.home.pk,)))
        self.assertEqual(page_response.status_code, 200)
        self.assertFalse(page_response.context["adminform"].form.fields["title"].disabled)
        self.assertFalse(page_response.context["adminform"].form.fields["body"].disabled)
        self.assertEqual(page_response.context["inline_admin_formsets"], [])
        for route, args in (
            ("admin:content_sitesettings_change", (1,)),
            ("admin:content_homepagerecommendation_changelist", ()),
        ):
            with self.subTest(route=route):
                self.assertEqual(self.client.get(reverse(route, args=args)).status_code, 200)


class HomepageCoverRemovalMigrationTests(TransactionTestCase):
    migrate_from = ("content", "0006_homepagesettings_show_hero_image")
    migrate_to = ("content", "0007_remove_homepage_settings")

    # На исторической схеме заполнить CMS, общие настройки и рекомендацию с товаром;
    # новая миграция удаляет только таблицу обложки, сохраняя остальные записи и файл.
    def test_removal_preserves_cms_recommendation_product_and_uploaded_file(self):
        executor = MigrationExecutor(connection)
        self.assertIn(self.migrate_to, executor.loader.graph.nodes, "Отсутствует миграция удаления HomepageSettings.")
        latest = executor.loader.graph.leaf_nodes()
        from_targets = [node for node in latest if node[0] != "content"] + [self.migrate_from]
        to_targets = [node for node in latest if node[0] != "content"] + [self.migrate_to]
        with TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            try:
                executor.migrate(from_targets)
                old_apps = executor.loader.project_state(from_targets).apps
                OldPage = old_apps.get_model("content", "ContentPage")
                OldSection = old_apps.get_model("content", "ContentSection")
                OldSite = old_apps.get_model("content", "SiteSettings")
                OldCover = old_apps.get_model("content", "HomepageSettings")
                OldRecommendation = old_apps.get_model("content", "HomepageRecommendation")
                OldCategory = old_apps.get_model("catalog", "Category")
                OldProduct = old_apps.get_model("catalog", "Product")
                page, _ = OldPage.objects.update_or_create(
                    key="home", defaults={"title": "Сохранённая главная", "seo_title": "Сохранённый SEO"},
                )
                for key in ("hero", "hero_value_1", "hero_value_2", "hero_value_3"):
                    OldSection.objects.update_or_create(
                        page_id=page.pk, key=key,
                        defaults={"title": f"Сохранённый {key}", "body": f"Текст {key}"},
                    )
                section_fields = ("pk", "key", "title", "body", "image", "image_alt", "display_order")
                sections_before = list(OldSection.objects.filter(page_id=page.pk).order_by("pk").values_list(*section_fields))
                OldSite.objects.update_or_create(pk=1, defaults={
                    "phone_primary": "+7 000 765-43-21", "address": "Тестовый адрес", "footer_copy": "Сохранённый подвал",
                })
                cover_file = Path(media_root) / "content/homepage/old-cover.jpg"
                cover_file.parent.mkdir(parents=True)
                cover_file.write_bytes(b"synthetic uploaded cover")
                OldCover.objects.update_or_create(pk=1, defaults={
                    "hero_image": "content/homepage/old-cover.jpg", "hero_image_alt": "Старое фото",
                    "button_label": "Старая кнопка", "button_url": "/old-offer/",
                })
                category = OldCategory.objects.create(name="Сохранённая категория", slug="migration-cover-category")
                product = OldProduct.objects.create(
                    name="Сохранённая рыба", slug="migration-cover-product", category_id=category.pk,
                    price_per_kg=Decimal("456.78"), is_published=True,
                )
                recommendation, _ = OldRecommendation.objects.update_or_create(slot=1, defaults={"product_id": product.pk})
                self.assertIn("content_homepagesettings", connection.introspection.table_names())

                executor = MigrationExecutor(connection)
                executor.migrate(to_targets)
                new_apps = executor.loader.project_state(to_targets).apps
                self.assertNotIn("content_homepagesettings", connection.introspection.table_names())
                with self.assertRaises(LookupError):
                    new_apps.get_model("content", "HomepageSettings")
                saved_page = new_apps.get_model("content", "ContentPage").objects.get(pk=page.pk)
                self.assertEqual((saved_page.title, saved_page.seo_title), ("Сохранённая главная", "Сохранённый SEO"))
                self.assertEqual(list(
                    new_apps.get_model("content", "ContentSection").objects.filter(page_id=page.pk)
                    .order_by("pk").values_list(*section_fields)
                ), sections_before)
                saved_site = new_apps.get_model("content", "SiteSettings").objects.get(pk=1)
                self.assertEqual(
                    (saved_site.phone_primary, saved_site.address, saved_site.footer_copy),
                    ("+7 000 765-43-21", "Тестовый адрес", "Сохранённый подвал"),
                )
                saved_product = new_apps.get_model("catalog", "Product").objects.get(pk=product.pk)
                self.assertEqual(
                    (saved_product.name, saved_product.category_id, saved_product.price_per_kg),
                    ("Сохранённая рыба", category.pk, Decimal("456.78")),
                )
                saved_recommendation = new_apps.get_model("content", "HomepageRecommendation").objects.get(pk=recommendation.pk)
                self.assertEqual((saved_recommendation.slot, saved_recommendation.product_id), (1, product.pk))
                self.assertEqual(cover_file.read_bytes(), b"synthetic uploaded cover")
            finally:
                executor = MigrationExecutor(connection)
                executor.migrate(latest)
