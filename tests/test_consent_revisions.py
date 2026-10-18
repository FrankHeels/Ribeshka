"""Consent archive invariants and migration behavior on the isolated database."""

# Запуск этого файла:
# .venv/bin/python manage.py test tests.test_consent_revisions --settings=fishsite.settings_test --noinput
# Django использует профиль fishsite.settings_test и отдельную тестовую БД
# test_fishsite на изолированном PostgreSQL; runner проверяет настройки и БД,
# создаёт базу для прогона и удаляет её после него. TestCase изолирует записи
# транзакциями. TransactionTestCase с MigrationExecutor применяет старые и
# новые состояния миграций на тестовой БД и возвращает актуальные миграции.
# Проверки используют Django ORM и assertions, клиент Django Admin, хеширование
# исходного Markdown и миграционные модели из project state; внешние сервисы
# и основная база не используются.

import hashlib

from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models.query import QuerySet
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import ProtectedError
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from content.consent_revisions import get_current_consent_revision
from content.legal_documents import read_legal_document_source
from content.models import ConsentRevision
from inquiries.models import Inquiry
from orders.models import Order
from tests.backoffice_helpers import login_verified_admin


class ConsentRevisionArchiveTests(TestCase):
    # Сценарий: создать розничную заявку и оптовый запрос без ссылки на
    # редакцию согласия. Ожидается ValidationError для обеих новых записей.
    def test_new_requests_require_a_revision_but_legacy_rows_can_remain_unknown(self):
        with self.assertRaises(ValidationError):
            Order.objects.create(
                name="Тест",
                phone="000000000",
                fulfillment_method=Order.FulfillmentMethod.PICKUP,
                privacy_consent=True,
                privacy_consent_at=timezone.now(),
            )
        with self.assertRaises(ValidationError):
            Inquiry.objects.create(
                contact_name="Тест",
                phone="000000000",
                privacy_consent=True,
                privacy_consent_at=timezone.now(),
            )

    # Сценарий: получить текущий Markdown согласия и архивировать редакцию.
    # Проверяются совпадение текста, SHA-256 и идентификатора, повторное
    # получение той же строки, а также запрет изменения/удаления через save,
    # QuerySet.update, bulk_update, экземпляр и QuerySet.delete.
    def test_current_source_is_archived_once_and_supported_orm_paths_are_immutable(self):
        source = read_legal_document_source("consent")
        revision = get_current_consent_revision()
        self.assertEqual(revision.text, source)
        self.assertEqual(revision.sha256, hashlib.sha256(source.encode("utf-8")).hexdigest())
        self.assertEqual(revision.revision_id, f"sha256:{revision.sha256}")
        self.assertEqual(get_current_consent_revision().pk, revision.pk)

        revision.text += "\nПодмена"
        with self.assertRaises(ValidationError):
            revision.save()
        revision.refresh_from_db()
        with self.assertRaises(ValidationError):
            ConsentRevision.objects.filter(pk=revision.pk).update(text="Подмена")
        with self.assertRaises(ValidationError):
            ConsentRevision.objects.bulk_update([revision], ["text"])
        with self.assertRaises(ValidationError):
            revision.delete()
        with self.assertRaises(ValidationError):
            ConsentRevision.objects.filter(pk=revision.pk).delete()

    # Сценарий: связать архивную редакцию с розничной заявкой и оптовым
    # запросом. Проверяются сохранённые ссылки, запрет удаления связанного
    # архива, доступ суперадминистратора к заявке и архиву, ссылка на архив,
    # отображение идентификатора, хеша и текста согласия, а также HTTP 403
    # при попытке удалить редакцию через Django Admin.
    def test_request_references_protect_archive_and_staff_can_find_the_revision(self):
        revision = get_current_consent_revision()
        order = Order.objects.create(
            name="Тест",
            phone="000000000",
            fulfillment_method=Order.FulfillmentMethod.PICKUP,
            privacy_consent=True,
            privacy_consent_at=timezone.now(),
            privacy_consent_revision=revision,
        )
        inquiry = Inquiry.objects.create(
            contact_name="Тест",
            phone="000000000",
            privacy_consent=True,
            privacy_consent_at=timezone.now(),
            privacy_consent_revision=revision,
        )
        self.assertEqual(order.privacy_consent_revision_id, revision.pk)
        self.assertEqual(inquiry.privacy_consent_revision_id, revision.pk)
        with self.assertRaises(ProtectedError):
            QuerySet.delete(ConsentRevision.objects.filter(pk=revision.pk))

        user = get_user_model().objects.create_superuser(username="consent-archive-admin", email="")
        login_verified_admin(self.client, user)
        order_page = self.client.get(reverse("admin:orders_order_change", args=(order.pk,)))
        self.assertEqual(order_page.status_code, 200)
        archive_url = reverse("admin:content_consentrevision_change", args=(revision.pk,))
        self.assertContains(order_page, f'href="{archive_url}"')
        archive_page = self.client.get(archive_url)
        self.assertEqual(archive_page.status_code, 200)
        self.assertContains(archive_page, revision.revision_id)
        self.assertContains(archive_page, revision.sha256)
        self.assertContains(archive_page, "Я добровольно разрешаю")
        self.assertEqual(
            self.client.post(reverse("admin:content_consentrevision_delete", args=(revision.pk,)), {"post": "yes"}).status_code,
            403,
        )


class ConsentRevisionMigrationTests(TransactionTestCase):
    """Existing records must retain consent timestamps without a guessed revision."""

    migrate_from = [
        ("content", "0002_seed_fixed_pages"),
        ("orders", "0001_initial"),
        ("inquiries", "0002_optional_need_and_success_token"),
    ]
    migrate_to = [
        ("content", "0003_consent_revision_archive"),
        ("orders", "0002_consent_revision"),
        ("inquiries", "0003_consent_revision"),
    ]

    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        OldOrder = old_apps.get_model("orders", "Order")
        OldInquiry = old_apps.get_model("inquiries", "Inquiry")
        self.order_pk = OldOrder.objects.create(
            name="Старая розничная запись",
            phone="000000001",
            fulfillment_method="pickup",
            privacy_consent=True,
            privacy_consent_at=timezone.now(),
        ).pk
        self.inquiry_pk = OldInquiry.objects.create(
            contact_name="Старая оптовая запись",
            phone="000000002",
            need="",
            privacy_consent=True,
            privacy_consent_at=timezone.now(),
        ).pk

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        self.new_apps = executor.loader.project_state(self.migrate_to).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    # Сценарий: создать в старом состоянии схемы розничную и оптовую записи
    # с принятым согласием и временем, затем применить миграции архива и ссылок.
    # Тест проверяет у обеих записей сохранённое согласие и непустую дату, но
    # пустую ссылку на редакцию: миграция не должна выдумывать её задним числом.
    def test_existing_rows_keep_consent_but_have_no_invented_revision(self):
        Order = self.new_apps.get_model("orders", "Order")
        Inquiry = self.new_apps.get_model("inquiries", "Inquiry")
        order = Order.objects.get(pk=self.order_pk)
        inquiry = Inquiry.objects.get(pk=self.inquiry_pk)
        self.assertTrue(order.privacy_consent)
        self.assertIsNotNone(order.privacy_consent_at)
        self.assertIsNone(order.privacy_consent_revision_id)
        self.assertTrue(inquiry.privacy_consent)
        self.assertIsNotNone(inquiry.privacy_consent_at)
        self.assertIsNone(inquiry.privacy_consent_revision_id)
