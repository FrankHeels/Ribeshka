"""Real commits, rollback, concurrent POSTs and LISTEN/NOTIFY on the isolated DB."""

# Запуск этого файла:
# .venv/bin/python manage.py test tests.test_request_committed_scenarios --settings=fishsite.settings_test --noinput
# Используется изолированная тестовая PostgreSQL-БД test_fishsite на локальном
# тестовом кластере, а не основная база. TransactionTestCase позволяет проверить
# настоящий commit/rollback и несколько подключений; Django создаёт и удаляет
# тестовую БД. Запросы отправляет Django test Client. Для проверки pg_notify
# открывается отдельное psycopg-подключение с LISTEN; два параллельных POST
# отправляются через ThreadPoolExecutor, каждый с собственным DB-подключением.

from concurrent.futures import ThreadPoolExecutor
import threading
import uuid
from unittest.mock import patch

import psycopg

from django.conf import settings
from django.db import DatabaseError, close_old_connections, connections, transaction
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse

from catalog.models import Category, Product
from inquiries.models import Inquiry
from integrations.models import NotificationOutbox
from integrations.public_forms import enqueue_notification
from orders.cart import CART_SESSION_KEY
from orders.models import Order


class CommittedRequestScenarios(TransactionTestCase):
    """Django owns database creation and cleanup, including failed scenarios."""

    def setUp(self):
        self.marker = uuid.uuid4().hex
        self.remote_addr = f"committed-request-{self.marker}"
        self.client = self.new_client()
        self.category = Category.objects.create(
            name=f"Committed request category {self.marker}",
            slug=f"committed-request-{self.marker}",
        )
        self.product = self.create_product(
            name=f"Committed request fish {self.marker}",
            slug=f"committed-request-fish-{self.marker}",
            price="14.25",
        )

    def new_client(self):
        client = Client()
        client.defaults["REMOTE_ADDR"] = self.remote_addr
        return client

    def create_product(self, *, name, slug, price):
        product = Product.objects.create(
            name=name,
            slug=slug,
            category=self.category,
            price_per_kg=price,
            minimum_weight="0.100",
            weight_step="0.100",
            is_published=True,
        )
        return product

    def get(self, client, url):
        response = client.get(url)
        return response

    def post(self, client, url, data):
        response = client.post(url, data)
        return response

    def add_cart_product(self, client, product=None, weight="1"):
        response = self.post(
            client,
            reverse("orders:cart"),
            {
                "action": "add",
                "product_id": str((product or self.product).pk),
                "weight": weight,
            },
        )
        self.assertEqual(response.status_code, 302)

    def retail_submission(self, client):
        response = self.get(client, reverse("orders:cart"))
        self.assertEqual(response.status_code, 200)
        return response.context["submission"]

    def wholesale_submission(self, client):
        response = self.get(client, reverse("inquiries:wholesale"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="need"')
        return response.context["submission"]

    def retail_data(self, submission, **overrides):
        return {
            "action": "submit",
            **submission,
            "name": f"Committed Retail Contact {self.marker}",
            "phone": "+7 (921) 555-12-34",
            "email": "committed-retail@example.test",
            "pickup_method": "delivery",
            "address": f"Committed test address {self.marker}",
            "comment": f"Committed test note {self.marker}",
            "consent": "yes",
            **overrides,
        }

    def wholesale_data(self, submission, **overrides):
        return {
            **submission,
            "contact_name": f"Committed Wholesale Contact {self.marker}",
            "company": f"Committed Test Company {self.marker}",
            "phone": "+7 (921) 555-56-78",
            "email": "committed-wholesale@example.test",
            "consent": "yes",
            **overrides,
        }

    def open_listener(self):
        params = connections["default"].get_connection_params()
        listener = psycopg.connect(**params, autocommit=True)
        listener.execute("LISTEN notification_outbox")
        return listener

    @staticmethod
    def receive_notifications(listener, *, timeout=2.0, stop_after=2):
        return list(listener.notifies(timeout=timeout, stop_after=stop_after))

    def assert_one_notification(self, listener):
        notifications = self.receive_notifications(listener)
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].channel, "notification_outbox")
        self.assertEqual(notifications[0].payload, "")

    def assert_no_notification(self, listener):
        self.assertEqual(self.receive_notifications(listener, timeout=0.15, stop_after=1), [])

    # Успешная розничная отправка с самовывозом создаёт один заказ, одну позицию
    # и одну outbox-запись, сохраняет подписанную редакцию согласия и после commit
    # посылает ровно один сигнал; корзина очищается, страница успеха доступна.
    def test_retail_commit_persists_one_outbox_and_one_post_commit_notify(self):
        self.add_cart_product(self.client, weight="2")
        submission = self.retail_submission(self.client)
        data = self.retail_data(submission, pickup_method="pickup", address="")

        with self.open_listener() as listener:
            response = self.post(self.client, reverse("orders:cart"), data)
            self.assertEqual(response.status_code, 302)
            order = Order.objects.get(submission_key=submission["submission_key"])
            signed_revision_id = submission["consent_url"].split("/revisions/", 1)[1].strip("/")
            self.assertEqual(order.privacy_consent_revision.revision_id, signed_revision_id)
            self.assertEqual(order.items.count(), 1)
            self.assertEqual(NotificationOutbox.objects.filter(order=order).count(), 1)
            self.assert_one_notification(listener)
            self.assert_no_notification(listener)

        self.assertEqual(self.client.session.get(CART_SESSION_KEY, {}), {})
        self.assertEqual(self.get(self.client, response.url).status_code, 200)

    # Успешная оптовая отправка создаёт одну заявку с подписанной редакцией,
    # компанией и email, пустым need и одной outbox-записью; после commit приходит
    # один сигнал. Страница успеха показывает общий текст без номера заявки.
    def test_wholesale_commit_persists_one_outbox_and_one_post_commit_notify(self):
        submission = self.wholesale_submission(self.client)
        data = self.wholesale_data(submission)

        with self.open_listener() as listener:
            response = self.post(self.client, reverse("inquiries:wholesale"), data)
            self.assertEqual(response.status_code, 302)
            inquiry = Inquiry.objects.get(submission_key=submission["submission_key"])
            signed_revision_id = submission["consent_url"].split("/revisions/", 1)[1].strip("/")
            self.assertEqual(inquiry.privacy_consent_revision.revision_id, signed_revision_id)
            self.assertEqual(inquiry.company, f"Committed Test Company {self.marker}")
            self.assertEqual(inquiry.email, "committed-wholesale@example.test")
            self.assertEqual(inquiry.need, "")
            self.assertEqual(NotificationOutbox.objects.filter(inquiry=inquiry).count(), 1)
            self.assert_one_notification(listener)
            self.assert_no_notification(listener)

        success = self.get(self.client, response.url)
        self.assertEqual(success.status_code, 200)
        self.assertContains(success, "Заявка принята. Менеджер свяжется с вами")
        self.assertNotContains(success, inquiry.inquiry_number)
        self.assertNotIn("inquiry", success.context)
        self.assertNotIn("inquiry_number", success.context)
        self.assertNotContains(success, f"Committed Wholesale Contact {self.marker}")
        self.assertNotContains(success, "+7 (921) 555-56-78")

    # Искусственный внешний rollback после успешного POST удаляет заказ и outbox,
    # сохраняет исходную корзину и не доставляет LISTEN/NOTIFY-сигнал.
    def test_outer_rollback_removes_retail_rows_preserves_cart_and_emits_no_notify(self):
        self.add_cart_product(self.client, weight="3")
        submission = self.retail_submission(self.client)
        data = self.retail_data(submission, pickup_method="pickup", address="")

        class RollbackScenario(Exception):
            pass

        with self.open_listener() as listener:
            with self.assertRaises(RollbackScenario):
                with transaction.atomic():
                    response = self.post(self.client, reverse("orders:cart"), data)
                    self.assertEqual(response.status_code, 302)
                    order = Order.objects.get(submission_key=submission["submission_key"])
                    self.assertEqual(NotificationOutbox.objects.filter(order=order).count(), 1)
                    raise RollbackScenario

            self.assertFalse(Order.objects.filter(submission_key=submission["submission_key"]).exists())
            self.assertFalse(
                NotificationOutbox.objects.filter(order__submission_key=submission["submission_key"]).exists()
            )
            self.assertEqual(self.client.session.get(CART_SESSION_KEY), {str(self.product.pk): "3"})
            self.assert_no_notification(listener)

    # Ошибка БД после постановки outbox и pg_notify возвращает 503; транзакция
    # откатывает заказ и outbox, оставляет корзину, не раскрывает текст исключения
    # и не доставляет сигнал слушателю.
    def test_failure_after_pg_notify_rolls_back_order_outbox_and_keeps_cart(self):
        self.add_cart_product(self.client, weight="4")
        submission = self.retail_submission(self.client)
        data = self.retail_data(submission, pickup_method="pickup", address="")

        def enqueue_then_fail(**kwargs):
            enqueue_notification(**kwargs)
            raise DatabaseError("test failure after transactional pg_notify")

        with self.open_listener() as listener:
            with patch(
                "orders.services.integration_handlers.enqueue_notification",
                side_effect=enqueue_then_fail,
            ):
                response = self.post(self.client, reverse("orders:cart"), data)
            self.assertEqual(response.status_code, 503)
            self.assertFalse(Order.objects.filter(submission_key=submission["submission_key"]).exists())
            self.assertFalse(
                NotificationOutbox.objects.filter(order__submission_key=submission["submission_key"]).exists()
            )
            self.assertEqual(self.client.session.get(CART_SESSION_KEY), {str(self.product.pk): "4"})
            self.assertNotContains(response, "test failure after transactional pg_notify", status_code=503)
            self.assert_no_notification(listener)

    def _concurrent_posts(self, clients, url, data):
        barrier = threading.Barrier(len(clients))

        def send(client):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                response = client.post(url, data)
                return response.status_code, response.url
            finally:
                connections["default"].close()

        with ThreadPoolExecutor(max_workers=len(clients)) as pool:
            results = list(pool.map(send, clients))
        return results

    def clients_for_same_session(self):
        first = self.client
        session_cookie = first.cookies[settings.SESSION_COOKIE_NAME].value
        second = self.new_client()
        second.cookies[settings.SESSION_COOKIE_NAME] = session_cookie
        return first, second

    # Два одновременных розничных POST одного ключа и одной сессии оба получают
    # redirect на один URL, но сохраняются ровно один заказ и одна outbox-запись.
    @override_settings(PUBLIC_FORM_RATE_LIMIT=50)
    def test_concurrent_duplicate_retail_posts_create_exactly_one_order_and_outbox(self):
        self.add_cart_product(self.client, weight="2")
        submission = self.retail_submission(self.client)
        clients = self.clients_for_same_session()
        data = self.retail_data(submission, pickup_method="pickup", address="")

        results = self._concurrent_posts(clients, reverse("orders:cart"), data)

        self.assertEqual([status for status, _url in results], [302, 302])
        self.assertEqual(results[0][1], results[1][1])
        order = Order.objects.get(submission_key=submission["submission_key"])
        self.assertEqual(Order.objects.filter(submission_key=submission["submission_key"]).count(), 1)
        self.assertEqual(NotificationOutbox.objects.filter(order=order).count(), 1)

    # Два одновременных оптовых POST одного ключа и одной сессии оба получают
    # redirect на один URL, но сохраняются ровно одна заявка и одна outbox-запись.
    @override_settings(PUBLIC_FORM_RATE_LIMIT=50)
    def test_concurrent_duplicate_wholesale_posts_create_exactly_one_inquiry_and_outbox(self):
        submission = self.wholesale_submission(self.client)
        clients = self.clients_for_same_session()
        data = self.wholesale_data(submission)

        results = self._concurrent_posts(clients, reverse("inquiries:wholesale"), data)

        self.assertEqual([status for status, _url in results], [302, 302])
        self.assertEqual(results[0][1], results[1][1])
        inquiry = Inquiry.objects.get(submission_key=submission["submission_key"])
        self.assertEqual(Inquiry.objects.filter(submission_key=submission["submission_key"]).count(), 1)
        self.assertEqual(NotificationOutbox.objects.filter(inquiry=inquiry).count(), 1)
