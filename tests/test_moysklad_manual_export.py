"""Owner approval is required before the first retail customerorder POST.

Run sequentially against fishsite.settings_test; provider HTTP is mocked.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from html.parser import HTMLParser
from io import StringIO
from threading import Barrier
import uuid
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import close_old_connections, connections, transaction
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from catalog.models import Category, Product
from content.consent_revisions import get_current_consent_revision
from inquiries.models import Inquiry
from integrations.models import MoySkladOrderLink, MoySkladSyncState, MoySkladTask, NotificationOutbox
from orders.models import Order, OrderItem
from tests.backoffice_helpers import login_verified_admin
from tests.moysklad_helpers import (
    ACCOUNT_ID, ENVIRONMENT, NAMESPACE, UNIT_KG_ID, moysklad_config, product_payload,
)
from tests.test_moysklad_exports import customer_order_response


class _FormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.nested = False
        self.forms = []

    def handle_starttag(self, tag, attrs):
        if tag == "form":
            self.nested |= self.depth > 0
            self.depth += 1
            self.forms.append(dict(attrs))

    def handle_endtag(self, tag):
        if tag == "form":
            self.depth -= 1


class ManualExportFixtures:
    def setUp(self):
        self.revision = get_current_consent_revision()
        self.owner = get_user_model().objects.create_user(username="manual-owner", is_active=True)
        call_command("sync_backoffice_roles", self.owner.username, stdout=StringIO())
        self.owner.refresh_from_db()
        self.viewer = get_user_model().objects.create_user(username="manual-viewer", is_staff=True)
        self.viewer.user_permissions.add(Permission.objects.get(
            content_type__app_label="orders", codename="view_order",
        ))
        self.state = MoySkladSyncState.objects.create(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, namespace=NAMESPACE,
            export_started_at=timezone.now() - timedelta(days=1),
            export_consent_revision=self.revision, export_enabled=True,
        )
        category = Category.objects.create(name="Ручная отправка", slug="manual-export")
        self.product = Product.objects.create(
            name="Форель", slug="manual-trout", category=category,
            price_per_kg=Decimal("1250.50"), is_published=True,
        )
        self.external_id = uuid.UUID("22000000-0000-4000-8000-000000000001")

    def make_order(self):
        order = Order.objects.create(
            name="Покупатель", phone="+7 900 000-00-00", email="manual@example.test",
            fulfillment_method=Order.FulfillmentMethod.DELIVERY,
            delivery_address="Калининград", comment="Позвонить перед доставкой",
            privacy_consent=True, privacy_consent_at=timezone.now(),
            privacy_consent_revision=self.revision,
        )
        OrderItem.objects.create(
            order=order, product=self.product, product_name_snapshot=self.product.name,
            price_per_kg_snapshot=Decimal("1250.50"), desired_weight=Decimal("3"),
            line_total=Decimal("3751.50"), external_account_id_snapshot=ACCOUNT_ID,
            external_entity_type_snapshot="product", external_id_snapshot=self.external_id,
            unit_id_snapshot=UNIT_KG_ID, price_revision_snapshot=1,
            price_source_snapshot="moysklad", price_checked_at_snapshot=timezone.now(),
        )
        return order

    def export_url(self, order):
        try:
            url = reverse("admin:moysklad_order_export", args=(order.pk,))
        except NoReverseMatch:
            self.fail("Missing POST-only admin:moysklad_order_export route for an Order pk")
        self.assertEqual(url, f"/admin/moysklad/orders/{order.pk}/send/")
        return url

    def snapshot(self, order):
        return (
            Order.objects.values().get(pk=order.pk),
            list(order.items.order_by("pk").values()),
        )

    def claim_export(self):
        from integrations.moysklad.queue import claim_task

        return claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("export_order",))

    def provider(self, link):
        client = Mock()
        client.get.return_value = product_payload(self.external_id, price="1250.50")
        client.post.side_effect = lambda path, json: customer_order_response(json, uuid.uuid4(), link.sync_id)
        return client

    def prepare_legacy_link(self, order, **changes):
        from integrations.moysklad.order_export import _prepare_payload, enqueue_order_export

        link = enqueue_order_export(order)
        link.prepared_payload = _prepare_payload(link)
        for field, value in changes.items():
            setattr(link, field, value)
        link.save(update_fields=("prepared_payload", *changes))
        return link


@override_settings(MOYSKLAD=moysklad_config())
class MoySkladManualExportTests(ManualExportFixtures, TestCase):
    def map_product(self):
        from integrations.moysklad.catalog_sync import link_product, upsert_product
        from integrations.moysklad.price_sync import process_price_task
        from integrations.moysklad.queue import claim_task

        payload = product_payload(self.external_id, price="1250.50")
        upsert_product(payload)
        link_product(product_id=self.product.pk, external_product_id=self.external_id, actor=self.owner)
        process_price_task(claim_task(kinds=("price",)), client=Mock(get=Mock(return_value=payload)))

    def submit_retail_twice(self):
        self.map_product()
        self.client.post(reverse("orders:cart"), {
            "action": "add", "product_id": str(self.product.pk), "weight": "3",
        })
        submission = self.client.get(reverse("orders:cart")).context["submission"]
        data = {
            "action": "submit", **submission, "name": "Покупатель",
            "phone": "+7 900 000-00-00", "email": "manual@example.test",
            "pickup_method": "delivery", "address": "Калининград", "consent": "yes",
        }
        with patch("httpx.Client.send") as http:
            first = self.client.post(reverse("orders:cart"), data)
            second = self.client.post(reverse("orders:cart"), data)
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(first.url, second.url)
        order = Order.objects.get(submission_key=submission["submission_key"])
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(order.items.count(), 1)
        self.assertEqual(NotificationOutbox.objects.filter(order=order).count(), 1)
        self.assertFalse(MoySkladOrderLink.objects.exists())
        self.assertFalse(MoySkladTask.objects.filter(kind="export_order").exists())
        http.assert_not_called()

    # Принятие и повтор подписанной заявки сохраняют одну заявку и письмо;
    # включённый обмен не создаёт решение владельца и экспортную очередь.
    def test_signed_retail_submit_and_repeat_remain_local_with_export_enabled(self):
        self.submit_retail_twice()

    # Пауза обмена также не превращает публичное оформление в отложенный экспорт.
    def test_signed_retail_submit_and_repeat_remain_local_with_export_paused(self):
        self.state.export_enabled = False
        self.state.save(update_fields=("export_enabled",))
        self.submit_retail_twice()

    # Владелец видит прямую POST-кнопку и подсказку связаться с покупателем;
    # просмотр неизменяемой заявки не создаёт очередь или вложенные формы.
    def test_readonly_order_has_post_button_customer_contact_help_and_no_get_mutation(self):
        order = self.make_order()
        before = self.snapshot(order)
        login_verified_admin(self.client, self.owner)
        with patch("httpx.Client.send") as http:
            response = self.client.get(reverse("admin:orders_order_change", args=(order.pk,)))
        self.assertContains(response, "Отправить в МойСклад")
        self.assertRegex(response.content.decode().lower(), r"свяж|связать|связаться|связи|соглас.*покупател|позвон")
        parser = _FormParser()
        parser.feed(response.content.decode())
        self.assertFalse(parser.nested, "The export action must not nest a form inside the admin form")
        self.assertTrue(any(form.get("action") == self.export_url(order) and form.get("method", "").lower() == "post"
                            for form in parser.forms))
        self.assertEqual(self.snapshot(order), before)
        self.assertFalse(MoySkladOrderLink.objects.exists())
        self.assertFalse(MoySkladTask.objects.filter(kind="export_order").exists())
        http.assert_not_called()

    # Обычный OTP-владелец отправляет заявку без права редактирования: одно
    # решение, связь и задача, без HTTP и без изменения принятого снимка.
    def test_owner_post_persists_one_approval_and_task_without_http_or_snapshot_changes(self):
        order = self.make_order()
        before = self.snapshot(order)
        self.assertFalse(self.owner.is_superuser)
        self.assertFalse(self.owner.has_perm("orders.change_order"))
        self.assertTrue(self.owner.has_perm("orders.view_order"))
        self.assertTrue(self.owner.has_perm("integrations.repair_moysklad_order"))
        login_verified_admin(self.client, self.owner)
        with patch("httpx.Client.send") as http:
            response = self.client.post(self.export_url(order))
            self.assertRedirects(response, reverse("admin:orders_order_change", args=(order.pk,)), fetch_redirect_response=False)
            link = MoySkladOrderLink.objects.get(order=order)
            first_approval = (link.approved_at, link.approved_by_id, list(link.audit_log))
            self.assertIsNotNone(link.approved_at)
            self.assertEqual(link.approved_by_id, self.owner.pk)
            repeat = self.client.post(self.export_url(order))
        self.assertEqual(repeat.status_code, 302)
        link.refresh_from_db()
        self.assertEqual((link.approved_at, link.approved_by_id, link.audit_log), first_approval)
        self.assertEqual(MoySkladOrderLink.objects.filter(order=order).count(), 1)
        self.assertEqual(MoySkladTask.objects.filter(kind="export_order", order_link=link).count(), 1)
        self.assertEqual(self.snapshot(order), before)
        http.assert_not_called()

    # Фоновый исполнитель отправляет снимок только после явного решения.
    def test_worker_sends_approved_order_later(self):
        from integrations.moysklad.order_export import process_export_task, request_order_export

        order = self.make_order()
        before = self.snapshot(order)
        with patch("httpx.Client.send") as http:
            link = request_order_export(order.pk, actor=self.owner)
        http.assert_not_called()
        client = self.provider(link)
        task = self.claim_export()
        process_export_task(task, client=client)
        client.post.assert_called_once()
        self.assertEqual(client.post.call_args.kwargs["json"]["positions"][0]["price"], 125050)
        link.refresh_from_db()
        task.refresh_from_db()
        self.assertIsNotNone(link.external_id)
        self.assertEqual(task.status, "done")
        self.assertEqual(self.snapshot(order), before)

    # Старую очередь исполнитель удерживает до решения владельца; одно
    # решение возобновляет прежнюю задачу и отправляет прежний снимок.
    def test_first_approval_resumes_same_legacy_task_after_worker_holds_it(self):
        from integrations.moysklad.order_export import enqueue_order_export, process_export_task

        order = self.make_order()
        before = self.snapshot(order)
        link = enqueue_order_export(order)
        original_identity = (link.pk, link.sync_id, link.config_snapshot)
        task = self.claim_export()
        task_id = task.pk
        client = self.provider(link)
        process_export_task(task, client=client)
        task.refresh_from_db()
        self.assertEqual(task.status, "manual_review")
        self.assertEqual(task.last_error_code, "owner_approval_required")
        client.post.assert_not_called()

        login_verified_admin(self.client, self.owner)
        with patch("httpx.Client.send") as http:
            response = self.client.post(self.export_url(order))
        self.assertEqual(response.status_code, 302)
        http.assert_not_called()
        link.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual((link.pk, link.sync_id, link.config_snapshot), original_identity)
        self.assertEqual(task.pk, task_id)
        self.assertEqual(task.status, "pending")
        self.assertEqual(task.last_error_code, "")
        self.assertEqual(link.last_error_code, "")
        self.assertEqual(link.approved_by_id, self.owner.pk)
        approved_at = link.approved_at
        self.assertIsNotNone(approved_at)

        resumed = self.claim_export()
        self.assertEqual(resumed.pk, task_id)
        process_export_task(resumed, client=client)
        client.post.assert_called_once()
        link.refresh_from_db()
        task.refresh_from_db()
        self.assertIsNotNone(link.external_id)
        self.assertEqual(link.approved_at, approved_at)
        self.assertEqual(task.status, "done")
        self.assertEqual(MoySkladOrderLink.objects.filter(order=order).count(), 1)
        self.assertEqual(MoySkladTask.objects.filter(order_link=link).count(), 1)
        self.assertEqual(self.snapshot(order), before)

    # Повтор кнопки после решения не сбрасывает успех, ручной разбор или
    # действующую lease; время решения и все сохранённые данные неизменны.
    def test_repeated_approval_preserves_done_manual_review_and_processing_tasks(self):
        from integrations.moysklad.order_export import request_order_export

        login_verified_admin(self.client, self.owner)
        for status in ("done", "manual_review", "processing"):
            with self.subTest(status=status):
                order = self.make_order()
                link = request_order_export(order.pk, actor=self.owner)
                task = MoySkladTask.objects.get(order_link=link)
                updates = {
                    "status": status, "attempt_count": 4,
                    "next_attempt_at": timezone.now() + timedelta(hours=1),
                    "last_error_code": "timeout" if status == "manual_review" else "",
                }
                if status == "processing":
                    updates.update(lease_token=uuid.uuid4(), lease_expires_at=timezone.now() + timedelta(minutes=5))
                else:
                    updates["completed_at"] = timezone.now()
                MoySkladTask.objects.filter(pk=task.pk).update(**updates)
                if status == "done":
                    MoySkladOrderLink.objects.filter(pk=link.pk).update(external_id=uuid.uuid4(), exported_at=timezone.now())
                before_link = MoySkladOrderLink.objects.values().get(pk=link.pk)
                before_task = MoySkladTask.objects.values().get(pk=task.pk)
                before_order = self.snapshot(order)

                with patch("httpx.Client.send") as http:
                    response = self.client.post(self.export_url(order))
                self.assertEqual(response.status_code, 302)
                self.assertEqual(MoySkladOrderLink.objects.values().get(pk=link.pk), before_link)
                self.assertEqual(MoySkladTask.objects.values().get(pk=task.pk), before_task)
                self.assertEqual(self.snapshot(order), before_order)
                self.assertEqual(MoySkladTask.objects.filter(order_link=link).count(), 1)
                http.assert_not_called()

    # Старые идентификаторы аккаунта, окружения и namespace не получают
    # одобрение от кнопки текущей интеграции.
    def test_stale_link_account_environment_and_namespace_cannot_be_approved(self):
        from integrations.moysklad.order_export import enqueue_order_export, request_order_export

        stale_targets = (
            {"account_id": uuid.uuid4()},
            {"environment": "old-test"},
            {"config_snapshot": {"namespace": str(uuid.uuid4())}},
        )
        for changes in stale_targets:
            with self.subTest(changes=changes):
                order = self.make_order()
                link = enqueue_order_export(order)
                MoySkladOrderLink.objects.filter(pk=link.pk).update(**changes)
                before_link = MoySkladOrderLink.objects.values().get(pk=link.pk)
                before_task = list(MoySkladTask.objects.filter(order_link=link).values())
                with self.assertRaises(ValidationError):
                    request_order_export(order.pk, actor=self.owner)
                self.assertEqual(MoySkladOrderLink.objects.values().get(pk=link.pk), before_link)
                self.assertEqual(list(MoySkladTask.objects.filter(order_link=link).values()), before_task)

    # Изменённый namespace состояния не создаёт новую одобренную связь.
    def test_stale_state_namespace_cannot_create_approval(self):
        from integrations.moysklad.order_export import request_order_export

        self.state.namespace = uuid.uuid4()
        self.state.save(update_fields=("namespace",))
        with self.assertRaises(ValidationError):
            request_order_export(self.make_order().pk, actor=self.owner)
        self.assertFalse(MoySkladOrderLink.objects.exists())
        self.assertFalse(MoySkladTask.objects.filter(kind="export_order").exists())

    # Недоступная очередь откатывает новую связь и решение вместе;
    # неизменяемая заявка продолжает существовать локально.
    def test_queue_creation_failure_rolls_back_new_link_task_and_approval(self):
        from integrations.moysklad.order_export import request_order_export

        order = self.make_order()
        before = self.snapshot(order)
        with patch("integrations.moysklad.order_export.enqueue_task", side_effect=RuntimeError("queue unavailable")):
            with self.assertRaisesRegex(RuntimeError, "queue unavailable"):
                request_order_export(order.pk, actor=self.owner)
        self.assertFalse(MoySkladOrderLink.objects.filter(order=order).exists())
        self.assertFalse(MoySkladTask.objects.filter(kind="export_order").exists())
        self.assertEqual(self.snapshot(order), before)

    # Даже ошибка после сброса прежней задачи откатывает её состояние,
    # actor/time и аудит первого решения владельца в общей транзакции.
    def test_task_reset_failure_rolls_back_existing_link_approval_and_task_changes(self):
        from integrations.moysklad.order_export import _reset_export_task, enqueue_order_export, request_order_export

        order = self.make_order()
        link = enqueue_order_export(order)
        task = MoySkladTask.objects.get(order_link=link)
        MoySkladTask.objects.filter(pk=task.pk).update(
            status="manual_review", last_error_code="owner_approval_required", completed_at=timezone.now(),
        )
        before_link = MoySkladOrderLink.objects.values().get(pk=link.pk)
        before_task = MoySkladTask.objects.values().get(pk=task.pk)
        before_order = self.snapshot(order)

        def reset_then_fail(locked_link):
            _reset_export_task(locked_link)
            raise RuntimeError("reset interrupted")

        with patch("integrations.moysklad.order_export._reset_export_task", side_effect=reset_then_fail):
            with self.assertRaisesRegex(RuntimeError, "reset interrupted"):
                request_order_export(order.pk, actor=self.owner)
        self.assertEqual(MoySkladOrderLink.objects.values().get(pk=link.pk), before_link)
        self.assertEqual(MoySkladTask.objects.values().get(pk=task.pk), before_task)
        self.assertEqual(self.snapshot(order), before_order)

    # Читатель не видит кнопку отправки, а успешно переданная заявка
    # больше не предлагает первое решение владельца.
    def test_viewer_and_successfully_exported_card_have_no_first_send_button(self):
        from integrations.moysklad.order_export import process_export_task, request_order_export

        order = self.make_order()
        login_verified_admin(self.client, self.viewer)
        page = self.client.get(reverse("admin:orders_order_change", args=(order.pk,)))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "Отправить в МойСклад")
        self.assertNotContains(page, self.export_url(order))

        link = request_order_export(order.pk, actor=self.owner)
        provider = self.provider(link)
        process_export_task(self.claim_export(), client=provider)
        provider.post.assert_called_once()
        login_verified_admin(self.client, self.owner)
        page = self.client.get(reverse("admin:orders_order_change", args=(order.pk,)))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "Отправить в МойСклад")
        self.assertNotContains(page, self.export_url(order))

    # Панель считает новую локальную заявку без связи и очереди, а после
    # решения владельца убирает её из ожидающих решения.
    def test_dashboard_counts_local_public_order_until_owner_approval(self):
        self.submit_retail_twice()
        order = Order.objects.get()
        login_verified_admin(self.client, self.owner)
        page = self.client.get(reverse("admin:moysklad_status"))
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["export_approval_count"], 1)
        self.assertContains(page, "Ожидают решения владельца")
        self.assertEqual(self.client.post(self.export_url(order)).status_code, 302)
        page = self.client.get(reverse("admin:moysklad_status"))
        self.assertEqual(page.context["export_approval_count"], 0)

    # Счётчик включает прежнюю ожидающую связь текущей интеграции и исключает
    # одобренные, переданные и относящиеся к прежней конфигурации заявки.
    def test_dashboard_approval_count_excludes_decided_exported_and_foreign_links(self):
        from integrations.moysklad.order_export import enqueue_order_export, request_order_export

        waiting = enqueue_order_export(self.make_order())
        request_order_export(self.make_order().pk, actor=self.owner)
        exported = enqueue_order_export(self.make_order())
        MoySkladOrderLink.objects.filter(pk=exported.pk).update(external_id=uuid.uuid4())
        for stale in ({"account_id": uuid.uuid4()}, {"environment": "old-test"},
                      {"config_snapshot": {"namespace": str(uuid.uuid4())}}):
            link = enqueue_order_export(self.make_order())
            MoySkladOrderLink.objects.filter(pk=link.pk).update(**stale)
        login_verified_admin(self.client, self.owner)
        page = self.client.get(reverse("admin:moysklad_status"))
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["export_approval_count"], 1)
        self.assertIsNone(waiting.approved_at)

    # Старая заявка отправляется только отдельным решением владельца;
    # исторический путь фиксирует причину и не подхватывает остальные заявки.
    def test_explicit_historical_order_approval_does_not_backfill_other_orders(self):
        from integrations.moysklad.order_export import request_order_export

        order = self.make_order()
        other = self.make_order()
        Order.objects.filter(pk__in=(order.pk, other.pk)).update(created_at=self.state.export_started_at - timedelta(days=1))
        before = self.snapshot(order)
        link = request_order_export(order.pk, actor=self.owner)
        self.assertEqual(link.approved_by_id, self.owner.pk)
        self.assertTrue(any(row.get("action") == "historical_export" and row.get("reason") and row.get("actor") == self.owner.pk
                            for row in link.audit_log))
        self.assertFalse(MoySkladOrderLink.objects.filter(order=other).exists())
        self.assertEqual(self.snapshot(order), before)

    # Просмотр endpoint и POST без CSRF не одобряют передачу.
    def test_export_endpoint_is_post_only_and_csrf_protected(self):
        order = self.make_order()
        login_verified_admin(self.client, self.owner)
        self.assertEqual(self.client.get(self.export_url(order)).status_code, 405)
        csrf_client = Client(enforce_csrf_checks=True)
        login_verified_admin(csrf_client, self.owner)
        self.assertEqual(csrf_client.post(self.export_url(order)).status_code, 403)
        self.assertFalse(MoySkladOrderLink.objects.exists())

    # Читатель и пользователь без OTP не могут отправить заявку прямым POST.
    def test_viewer_and_unverified_owner_cannot_approve_via_direct_url(self):
        order = self.make_order()
        login_verified_admin(self.client, self.viewer)
        self.assertEqual(self.client.post(self.export_url(order)).status_code, 403)
        self.client.force_login(self.owner)
        response = self.client.post(self.export_url(order))
        self.assertIn(response.status_code, (302, 403))
        self.assertFalse(MoySkladOrderLink.objects.exists())

    # Сервис требует оба существующих права даже при обходе HTTP-страницы.
    def test_service_requires_repair_and_view_permissions(self):
        from integrations.moysklad.order_export import request_order_export

        order = self.make_order()
        repair_only = get_user_model().objects.create_user(username="repair-only", is_staff=True)
        repair_only.user_permissions.add(Permission.objects.get(
            content_type__app_label="integrations", codename="repair_moysklad_order",
        ))
        for actor in (self.viewer, repair_only, None):
            with self.subTest(actor=actor):
                with self.assertRaises(PermissionDenied):
                    request_order_export(order.pk, actor=actor)
        self.assertFalse(MoySkladOrderLink.objects.exists())

    # Глобальный запуск интеграции обязателен; решение не запускает её само.
    def test_service_rejects_export_before_global_start(self):
        from integrations.moysklad.order_export import request_order_export

        self.state.export_started_at = None
        self.state.save(update_fields=("export_started_at",))
        with self.assertRaises(ValidationError):
            request_order_export(self.make_order().pk, actor=self.owner)
        self.assertFalse(MoySkladOrderLink.objects.exists())

    def assert_unapproved_not_sent(self, repair=None):
        from integrations.moysklad.order_export import enqueue_order_export, process_export_task

        order = self.make_order()
        before = self.snapshot(order)
        link = enqueue_order_export(order)
        if repair:
            repair(link)
        client = self.provider(link)
        process_export_task(self.claim_export(), client=client)
        client.post.assert_not_called()
        link.refresh_from_db()
        self.assertIsNone(link.approved_at)
        self.assertIsNone(link.approved_by_id)
        self.assertFalse(link.attempted)
        self.assertIsNone(link.external_id)
        self.assertEqual(self.snapshot(order), before)

    # Существовавшая до миграции очередь не является решением владельца.
    def test_old_queued_unapproved_order_cannot_create_external_document(self):
        self.assert_unapproved_not_sent()

    # Обычный повтор не подменяет отдельное решение отправить заявку.
    def test_retry_does_not_approve_unapproved_order(self):
        from integrations.moysklad.order_export import request_order_retry

        self.assert_unapproved_not_sent(lambda link: request_order_retry(link.pk, actor=self.owner, reason="Повторить проверку"))

    # Исправление внешних связей не одобряет передачу персональных данных.
    def test_override_does_not_approve_unapproved_order(self):
        from integrations.moysklad.order_export import set_export_override

        def repair(link):
            set_export_override(link.pk, {str(link.order.items.get().pk): {
                "account_id": str(ACCOUNT_ID), "entity_type": "product",
                "external_id": str(self.external_id), "unit_id": str(UNIT_KG_ID),
            }}, actor=self.owner, reason="Проверенная связь")

        self.assert_unapproved_not_sent(repair)

    # Неопределённый старый POST можно разыскать, но отрицательный поиск
    # не разрешает создать документ без отдельного решения владельца.
    def test_unapproved_uncertain_attempt_with_empty_search_does_not_post(self):
        from integrations.moysklad.order_export import process_export_task

        link = self.prepare_legacy_link(self.make_order(), attempted=True)
        client = self.provider(link)
        client.get.side_effect = lambda path, **kwargs: ({"meta": {"size": 0}, "rows": []}
                                                       if path == "entity/customerorder" else product_payload(self.external_id, price="1250.50"))
        process_export_task(self.claim_export(), client=client)
        self.assertTrue(any(call.args[0] == "entity/customerorder" for call in client.get.call_args_list))
        client.post.assert_not_called()
        link.refresh_from_db()
        self.assertIsNone(link.external_id)

    # Найденный результат прежней передачи сохраняется без нового POST.
    def test_unapproved_uncertain_attempt_can_record_existing_document(self):
        from integrations.moysklad.order_export import process_export_task

        link = self.prepare_legacy_link(self.make_order(), attempted=True)
        external_id = uuid.uuid4()
        document = customer_order_response(link.prepared_payload, external_id, link.sync_id)
        client = Mock(get=Mock(return_value={"meta": {"size": 1}, "rows": [document]}))
        process_export_task(self.claim_export(), client=client)
        client.post.assert_not_called()
        link.refresh_from_db()
        self.assertEqual(link.external_id, external_id)
        self.assertIsNotNone(link.exported_at)

    # Уже связанный документ доступен для безопасного чтения после миграции.
    def test_unapproved_already_exported_document_remains_readable(self):
        from integrations.moysklad.order_export import process_export_task

        external_id = uuid.uuid4()
        link = self.prepare_legacy_link(self.make_order(), external_id=external_id)
        document = customer_order_response(link.prepared_payload, external_id, link.sync_id)
        client = Mock(get=Mock(return_value=document))
        process_export_task(self.claim_export(), client=client)
        client.get.assert_called_once_with(f"entity/customerorder/{external_id}")
        client.post.assert_not_called()
        link.refresh_from_db()
        self.assertEqual(link.external_id, external_id)

    # Оптовое обращение не получает очередь или кнопку розничной передачи.
    def test_wholesale_submission_and_admin_have_no_export(self):
        submission = self.client.get(reverse("inquiries:wholesale")).context["submission"]
        with patch("httpx.Client.send") as http:
            response = self.client.post(reverse("inquiries:wholesale"), {
                **submission, "contact_name": "Оптовый покупатель", "phone": "+7 900 000-00-01", "consent": "yes",
            })
            self.assertEqual(response.status_code, 302)
            inquiry = Inquiry.objects.get()
            self.assertEqual(NotificationOutbox.objects.filter(inquiry=inquiry).count(), 1)
            login_verified_admin(self.client, self.owner)
            page = self.client.get(reverse("admin:inquiries_inquiry_change", args=(inquiry.pk,)))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "Отправить в МойСклад")
        self.assertFalse(MoySkladOrderLink.objects.exists())
        self.assertFalse(MoySkladTask.objects.filter(kind="export_order").exists())
        http.assert_not_called()


@override_settings(MOYSKLAD=moysklad_config())
class MoySkladManualApprovalConcurrencyTests(ManualExportFixtures, TransactionTestCase):
    # Запись результата worker блокирует связь и задачу, сохраняя возможность
    # параллельно держать неизменяемую Order: это исключает обратный порядок
    # Order/link и deadlock с первым решением владельца.
    def test_worker_result_write_does_not_wait_for_locked_immutable_order(self):
        from integrations.moysklad.order_export import _save_link, request_order_export

        order = self.make_order()
        link = request_order_export(order.pk, actor=self.owner)
        task = self.claim_export()
        external_id = uuid.uuid4()

        def save_result():
            close_old_connections()
            try:
                with connections["default"].cursor() as cursor:
                    cursor.execute("SET lock_timeout = '1500ms'")
                    cursor.execute("SET statement_timeout = '4s'")
                return _save_link(task, external_id=external_id).pk
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=1) as executor:
            with transaction.atomic():
                Order.objects.select_for_update().get(pk=order.pk)
                # The Order lock is held before the worker starts. An accidental
                # joined-row FOR UPDATE fails by lock_timeout, without sleeps.
                result = executor.submit(save_result).result(timeout=6)
                self.assertEqual(result, link.pk)
        link.refresh_from_db()
        self.assertEqual(link.external_id, external_id)

    # Два параллельных решения на отдельных PostgreSQL-соединениях оставляют
    # одну связь, одну задачу и первое неизменное решение владельца.
    def test_concurrent_owner_approvals_create_one_link_task_and_decision(self):
        from integrations.moysklad.order_export import request_order_export

        order = self.make_order()
        before = self.snapshot(order)
        barrier = Barrier(2, timeout=15)

        def approve():
            close_old_connections()
            try:
                with connections["default"].cursor() as cursor:
                    cursor.execute("SET lock_timeout = '10s'")
                actor = get_user_model().objects.get(pk=self.owner.pk)
                barrier.wait()
                link = request_order_export(order.pk, actor=actor)
                return link.pk, link.approved_at, link.approved_by_id
            finally:
                connections.close_all()

        with patch("httpx.Client.send") as http:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(approve) for _ in range(2)]
                results = [future.result(timeout=30) for future in futures]
        self.assertEqual(results[0], results[1])
        self.assertIsNotNone(results[0][1])
        self.assertEqual(MoySkladOrderLink.objects.filter(order=order).count(), 1)
        self.assertEqual(MoySkladTask.objects.filter(kind="export_order", order_link__order=order).count(), 1)
        self.assertEqual(self.snapshot(order), before)
        http.assert_not_called()
