"""Owner-confirmed deletion of local requests and external retail orders.

Run sequentially with fishsite.settings_test. Provider HTTP is always mocked.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from html.parser import HTMLParser
from io import StringIO
import json
from threading import Event
import uuid
from unittest.mock import Mock, patch

import httpx

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import close_old_connections, connection, connections, transaction
from django.db.models.signals import pre_delete
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from catalog.models import Category, Product
from content.consent_revisions import get_current_consent_revision
from content.models import ConsentRevision
from inquiries.models import Inquiry
from integrations.models import (
    MoySkladApiState, MoySkladIncident, MoySkladOrderLink, MoySkladSyncState, MoySkladTask,
    NotificationOutbox,
)
from orders.models import Order, OrderItem
from tests.backoffice_helpers import login_verified_admin
from tests.moysklad_helpers import (
    ACCOUNT_ID, API_BASE, ENVIRONMENT, NAMESPACE, ORGANIZATION_ID, UNIT_KG_ID,
    moysklad_config, product_payload,
)


class _DeletionFormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.nested = False
        self.forms = []
        self.controls = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.nested |= self.depth > 0
            self.depth += 1
            self.forms.append(attrs)
        if tag in ("input", "button"):
            self.controls.append(attrs)

    def handle_endtag(self, tag):
        if tag == "form":
            self.depth -= 1


class RequestDeletionFixtures:
    def setUp(self):
        self.revision = get_current_consent_revision()
        self.owner = get_user_model().objects.create_user(username="deletion-owner", is_active=True)
        call_command("sync_backoffice_roles", self.owner.username, stdout=StringIO())
        self.owner.refresh_from_db()
        category = Category.objects.create(name="Удаление", slug="request-deletion")
        self.product = Product.objects.create(
            name="Форель", slug="deletion-trout", category=category, price_per_kg="1250.50",
        )

    def make_order(self, **item_snapshots):
        order = Order.objects.create(
            name="Покупатель", phone="+7 900 000-00-00", fulfillment_method="pickup",
            privacy_consent=True, privacy_consent_at=timezone.now(),
            privacy_consent_revision=self.revision,
        )
        OrderItem.objects.create(
            order=order, product=self.product, product_name_snapshot="Форель", price_per_kg_snapshot="1250.50",
            desired_weight="2", **item_snapshots,
        )
        return order

    def make_inquiry(self):
        return Inquiry.objects.create(
            contact_name="Заказчик", company="Компания", need="Исходная потребность",
            phone="+7 900 000-00-01", privacy_consent=True,
            privacy_consent_at=timezone.now(), privacy_consent_revision=self.revision,
        )

    def delete_url(self, obj):
        return reverse(f"admin:{obj._meta.app_label}_{obj._meta.model_name}_delete", args=(obj.pk,))

    def state(self, **changes):
        values = {
            "namespace": NAMESPACE, "export_started_at": timezone.now() - timedelta(days=1),
            "export_consent_revision": self.revision,
            "catalog_enabled": False, "prices_enabled": False,
            "export_enabled": True, "statuses_enabled": False,
            **changes,
        }
        state, _ = MoySkladSyncState.objects.update_or_create(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, defaults=values,
        )
        return state

    def make_link(self, order, *, external=True, attempted=True, **changes):
        self.state()
        config = moysklad_config()
        snapshot = {name: config[name] for name in (
            "account_id", "environment", "namespace", "currency_id", "unit_id",
            "organization_id", "agent_id", "initial_state_id",
        )}
        snapshot.update(consent_revision_id=self.revision.pk, consent_approved=True)
        return MoySkladOrderLink.objects.create(
            order=order, account_id=ACCOUNT_ID, environment=ENVIRONMENT,
            sync_id=uuid.uuid5(NAMESPACE, f"{ACCOUNT_ID}:{order.submission_key}"),
            config_snapshot=snapshot,
            external_id=uuid.uuid4() if external else None, attempted=attempted,
            approved_at=timezone.now(), approved_by=self.owner,
            **changes,
        )

    def export_task(self, link, *, kind="export_order", status="done", **changes):
        if status == "processing":
            changes = {"lease_token": uuid.uuid4(), "lease_expires_at": timezone.now() + timedelta(minutes=5), **changes}
        return MoySkladTask.objects.create(
            account_id=link.account_id, environment=link.environment, kind=kind,
            dedupe_key=f"{kind}:{link.order_id}", order_link=link,
            object_id=str(link.order_id), status=status, **changes,
        )

    def outbox(self, obj, status="pending"):
        changes = {}
        if status == "sent":
            changes["sent_at"] = timezone.now()
        if status == "processing":
            changes.update(lease_token=uuid.uuid4(), lease_expires_at=timezone.now() + timedelta(minutes=5))
        return NotificationOutbox.objects.create(**{obj._meta.model_name: obj}, status=status, **changes)

    def snapshot(self):
        return {model._meta.label: list(model.objects.order_by("pk").values()) for model in (
            Order, OrderItem, Inquiry, NotificationOutbox, MoySkladOrderLink, MoySkladTask,
        )}

    def request_delete(self, order, *, actor=None):
        from orders.services.deletion import request_order_deletion

        return request_order_deletion(order.pk, actor=actor or self.owner)

    def claim_delete(self):
        from integrations.moysklad.queue import claim_task

        task = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("delete_order",))
        self.assertIsNotNone(task, "A durable delete_order task must be claimable")
        return task

    def process_delete(self, client):
        from integrations.moysklad.order_deletion import process_delete_task

        return process_delete_task(self.claim_delete(), client=client)

    def provider(self, link, *, include_sync=False):
        document = {
            "id": str(link.external_id or uuid.uuid4()), "accountId": str(ACCOUNT_ID),
            "meta": {"href": f"{API_BASE}entity/customerorder/{link.external_id}", "type": "customerorder"},
        }
        if include_sync:
            document["syncId"] = str(link.sync_id)
        client = Mock(spec=("get", "delete", "post", "put", "close"))
        client.get.return_value = document
        client.delete.return_value = None
        return client

    def organization(self):
        return {"id": str(ORGANIZATION_ID), "accountId": str(ACCOUNT_ID)}


class RequestDeletionAdministrationTests(RequestDeletionFixtures, TestCase):
    # OTP-владелец открывает подтверждение без изменения заявки или вызова API.
    def test_verified_owner_can_open_retail_deletion_confirmation_without_mutation(self):
        order = self.make_order()
        login_verified_admin(self.client, self.owner)
        with patch("httpx.Client.send") as http:
            response = self.client.get(self.delete_url(order))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Order.objects.filter(pk=order.pk).exists())
        self.assertEqual(order.items.count(), 1)
        parser = _DeletionFormParser()
        parser.feed(response.content.decode())
        self.assertFalse(parser.nested)
        self.assertTrue(any(form.get("method", "").lower() == "post" for form in parser.forms))
        self.assertTrue(any(control.get("name") == "confirm" and control.get("value") == "yes"
                            for control in parser.controls))
        http.assert_not_called()

    # Оптовый запрос имеет такое же отдельное подтверждение удаления.
    def test_verified_owner_can_open_wholesale_deletion_confirmation_without_mutation(self):
        inquiry = self.make_inquiry()
        login_verified_admin(self.client, self.owner)
        with patch("httpx.Client.send") as http:
            response = self.client.get(self.delete_url(inquiry))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Inquiry.objects.filter(pk=inquiry.pk).exists())
        http.assert_not_called()

    # Явное подтверждение удаляет локальные заявки и возвращает к их списку.
    def test_confirmed_local_deletion_redirects_to_matching_changelist_without_http(self):
        login_verified_admin(self.client, self.owner)
        for obj in (self.make_order(), self.make_inquiry()):
            with self.subTest(model=obj._meta.model_name):
                self.outbox(obj)
                with patch("httpx.Client.send") as http:
                    response = self.client.post(self.delete_url(obj), {"confirm": "yes"})
                self.assertRedirects(response, reverse(
                    f"admin:{obj._meta.app_label}_{obj._meta.model_name}_changelist",
                ), fetch_redirect_response=False)
                self.assertFalse(type(obj).objects.filter(pk=obj.pk).exists())
                http.assert_not_called()

    # Переданная заявка остаётся на карточке до фонового удаления; повтор
    # подтверждения сохраняет одно решение и одну задачу без синхронного HTTP.
    @override_settings(MOYSKLAD=moysklad_config())
    def test_confirmed_sent_deletion_queues_once_and_redirects_to_order_card(self):
        order = self.make_order()
        link = self.make_link(order)
        self.export_task(link)
        login_verified_admin(self.client, self.owner)
        with patch("httpx.Client.send") as http:
            first = self.client.post(self.delete_url(order), {"confirm": "yes"})
            link.refresh_from_db()
            marker = (link.deletion_requested_at, link.deletion_requested_by_id)
            second = self.client.post(self.delete_url(order), {"confirm": "yes"})
        for response in (first, second):
            self.assertRedirects(response, reverse("admin:orders_order_change", args=(order.pk,)), fetch_redirect_response=False)
        self.assertIsNotNone(marker[0])
        self.assertEqual(marker[1], self.owner.pk)
        link.refresh_from_db()
        self.assertEqual((link.deletion_requested_at, link.deletion_requested_by_id), marker)
        self.assertTrue(Order.objects.filter(pk=order.pk).exists())
        self.assertEqual(MoySkladTask.objects.filter(kind="delete_order", order_link=link).count(), 1)
        http.assert_not_called()

    # Пустой или другой POST не заменяет осознанное подтверждение.
    def test_post_requires_explicit_confirmation_for_both_request_types(self):
        login_verified_admin(self.client, self.owner)
        for obj in (self.make_order(), self.make_inquiry()):
            for data in ({}, {"confirm": "no"}, {"post": "yes"}):
                with self.subTest(model=obj._meta.model_name, data=data):
                    before = self.snapshot()
                    response = self.client.post(self.delete_url(obj), data)
                    self.assertLess(response.status_code, 500)
                    self.assertEqual(self.snapshot(), before)

    # Нативные права просмотра и удаления проверяются вместе, в том числе
    # для прямого URL и пользователя с одним правом удаления.
    def test_direct_viewer_and_delete_without_view_are_denied(self):
        for obj in (self.make_order(), self.make_inquiry()):
            app, name = obj._meta.app_label, obj._meta.model_name
            for granted in (f"view_{name}", f"delete_{name}"):
                with self.subTest(model=name, granted=granted):
                    user = get_user_model().objects.create_user(username=f"{name}-{granted}", is_staff=True)
                    user.user_permissions.add(Permission.objects.get(content_type__app_label=app, codename=granted))
                    login_verified_admin(self.client, user)
                    before = self.snapshot()
                    self.assertEqual(self.client.get(self.delete_url(obj)).status_code, 403)
                    self.assertEqual(self.client.post(self.delete_url(obj), {"confirm": "yes"}).status_code, 403)
                    self.assertEqual(self.snapshot(), before)

    # Сохранены OTP и CSRF: force_login без TOTP и POST без токена не удаляют.
    def test_otp_and_csrf_are_required_before_any_deletion(self):
        for obj in (self.make_order(), self.make_inquiry()):
            with self.subTest(model=obj._meta.model_name):
                unverified = Client()
                unverified.force_login(self.owner)
                csrf_client = Client(enforce_csrf_checks=True)
                login_verified_admin(csrf_client, self.owner)
                before = self.snapshot()
                self.assertIn(unverified.post(self.delete_url(obj), {"confirm": "yes"}).status_code, (302, 403))
                self.assertEqual(csrf_client.post(self.delete_url(obj), {"confirm": "yes"}).status_code, 403)
                self.assertEqual(self.snapshot(), before)

    # Роль получает только нативное удаление заявок; форма данных остаётся
    # неизменяемой, а общий delete_selected не открывает обход подтверждения.
    def test_owner_has_native_delete_permissions_but_no_edit_or_bulk_delete(self):
        for app, name in (("orders", "order"), ("inquiries", "inquiry")):
            self.assertTrue(self.owner.has_perm(f"{app}.view_{name}"))
            self.assertTrue(self.owner.has_perm(f"{app}.delete_{name}"))
            self.assertFalse(self.owner.has_perm(f"{app}.change_{name}"))
        login_verified_admin(self.client, self.owner)
        for obj in (self.make_order(), self.make_inquiry()):
            response = self.client.get(reverse(f"admin:{obj._meta.app_label}_{obj._meta.model_name}_change", args=(obj.pk,)))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(list(response.context["adminform"].form.fields), [])
            self.assertNotContains(response, 'name="_save"')
            self.assertContains(response, self.delete_url(obj))
            listing = self.client.get(reverse(f"admin:{obj._meta.app_label}_{obj._meta.model_name}_changelist"))
            self.assertNotContains(listing, 'value="delete_selected"')


class LocalRequestDeletionTests(RequestDeletionFixtures, TestCase):
    # Неотправленная заявка удаляется вместе с позициями и своим письмом
    # при любом завершённом состоянии outbox; общие данные сохраняются.
    @override_settings(MOYSKLAD={"account_id": "", "namespace": ""})
    def test_local_requests_remove_only_their_rows_without_provider_configuration(self):
        from inquiries.services import delete_inquiry

        other_order = self.make_order()
        other_inquiry = self.make_inquiry()
        self.outbox(other_order)
        self.outbox(other_inquiry, "sent")
        preserved = self.snapshot()
        for status in ("pending", "sent", "manual_review"):
            for make, delete in ((self.make_order, self.request_delete),
                                 (self.make_inquiry, lambda obj: delete_inquiry(obj.pk, actor=self.owner))):
                with self.subTest(status=status, request=make.__name__):
                    obj = make()
                    outbox = self.outbox(obj, status)
                    with patch("httpx.Client.send") as http:
                        result = delete(obj)
                    self.assertEqual(result, "deleted")
                    self.assertFalse(type(obj).objects.filter(pk=obj.pk).exists())
                    self.assertFalse(NotificationOutbox.objects.filter(pk=outbox.pk).exists())
                    self.assertEqual(self.snapshot(), preserved)
                    self.assertTrue(Product.objects.filter(pk=self.product.pk).exists())
                    self.assertTrue(ConsentRevision.objects.filter(pk=self.revision.pk).exists())
                    http.assert_not_called()

    # Явно одобренная, но ещё не начатая передача также полностью локальна.
    @override_settings(MOYSKLAD=moysklad_config())
    def test_approved_unattempted_link_and_all_its_tasks_are_removed_locally(self):
        order = self.make_order()
        link = self.make_link(order, external=False, attempted=False)
        self.export_task(link, status="pending")
        self.export_task(link, kind="status_sync", status="manual_review")
        self.outbox(order, "manual_review")
        with override_settings(MOYSKLAD={}), patch("httpx.Client.send") as http:
            self.assertEqual(self.request_delete(order), "deleted")
        self.assertFalse(Order.objects.filter(pk=order.pk).exists())
        self.assertFalse(OrderItem.objects.filter(order_id=order.pk).exists())
        self.assertFalse(MoySkladOrderLink.objects.filter(pk=link.pk).exists())
        self.assertFalse(MoySkladTask.objects.filter(order_link_id=link.pk).exists())
        self.assertFalse(NotificationOutbox.objects.filter(order_id=order.pk).exists())
        http.assert_not_called()

    # Ошибка в конце очистки откатывает уже удалённые письма, задачи и связи.
    @override_settings(MOYSKLAD=moysklad_config())
    def test_local_cleanup_is_atomic_when_request_deletion_fails(self):
        order = self.make_order()
        link = self.make_link(order, external=False, attempted=False)
        self.export_task(link, status="pending")
        self.outbox(order)
        before = self.snapshot()

        def abort(sender, instance, **kwargs):
            if instance.pk == order.pk:
                raise RuntimeError("synthetic cleanup failure")

        pre_delete.connect(abort, sender=Order, weak=False)
        try:
            with self.assertRaisesMessage(RuntimeError, "synthetic cleanup failure"):
                self.request_delete(order)
        finally:
            pre_delete.disconnect(abort, sender=Order)
        self.assertEqual(self.snapshot(), before)

    # Активная SMTP-аренда запрещает удаление любого типа заявки без мутаций.
    def test_active_smtp_lease_rejects_retail_and_wholesale_deletion(self):
        from inquiries.services import delete_inquiry

        for obj, delete in ((self.make_order(), self.request_delete),
                            (self.make_inquiry(), lambda obj: delete_inquiry(obj.pk, actor=self.owner))):
            with self.subTest(model=obj._meta.model_name):
                self.outbox(obj, "processing")
                before = self.snapshot()
                with self.assertRaises(ValidationError):
                    delete(obj)
                self.assertEqual(self.snapshot(), before)


@override_settings(MOYSKLAD=moysklad_config())
class ExternalRequestDeletionTests(RequestDeletionFixtures, TestCase):
    def queued_order(self, *, external=True, attempted=True):
        order = self.make_order()
        link = self.make_link(order, external=external, attempted=attempted)
        self.export_task(link)
        self.outbox(order, "sent")
        self.assertEqual(self.request_delete(order), "queued")
        return order, link

    def assert_cleaned(self, order, link):
        self.assertFalse(Order.objects.filter(pk=order.pk).exists())
        self.assertFalse(OrderItem.objects.filter(order_id=order.pk).exists())
        self.assertFalse(NotificationOutbox.objects.filter(order_id=order.pk).exists())
        self.assertFalse(MoySkladOrderLink.objects.filter(pk=link.pk).exists())
        self.assertFalse(MoySkladTask.objects.filter(order_link_id=link.pk).exists())
        self.assertTrue(Product.objects.filter(pk=self.product.pk).exists())
        self.assertTrue(ConsentRevision.objects.filter(pk=self.revision.pk).exists())

    # Удалённая неотправленная заявка больше не удерживает свою объектную
    # ошибку: общий incident остаётся для другой заявки и закрывается после
    # удаления последней. Общие паузы/задержки и доступ аккаунта сохраняются.
    def test_purge_prunes_only_affected_order_incidents_and_closes_last_reference(self):
        from integrations.moysklad.incidents import open_incident

        first, second = self.make_order(), self.make_order()
        for order in (first, second):
            link = self.make_link(order, external=False, attempted=False, last_error_code="incomplete_snapshot")
            self.export_task(link, status="manual_review", last_error_code="incomplete_snapshot")
        target = {"account_id": ACCOUNT_ID, "environment": ENVIRONMENT}
        affected = [first.order_number, second.order_number]
        incident = open_incident(scope="order_export", code="incomplete_snapshot", object_ids=affected, **target)
        unaffected = [open_incident(scope=scope, code=code, object_ids=affected, **target).pk for scope, code in (
            ("order_export", "document_limit"), ("order_export", "processing_delayed"),
            ("access", "access_denied"),
        )]
        preserved = list(MoySkladIncident.objects.filter(pk__in=unaffected).order_by("pk").values())
        self.assertEqual(self.request_delete(first), "deleted")
        incident.refresh_from_db()
        self.assertEqual(incident.object_ids, [second.order_number])
        self.assertIsNone(incident.closed_at)
        self.assertEqual(self.request_delete(second), "deleted")
        incident.refresh_from_db()
        self.assertIsNotNone(incident.closed_at)
        self.assertEqual(list(MoySkladIncident.objects.filter(pk__in=unaffected).order_by("pk").values()), preserved)

    # Снимок планировщика мог быть прочитан до решения владельца. Повторная
    # проверка под блокировкой пропускает маркер и уже удалённую связь.
    def test_stale_status_enqueue_after_marker_or_purge_cannot_recreate_work(self):
        from integrations.moysklad.status_sync import enqueue_status_task

        order = self.make_order()
        link = self.make_link(order)
        stale_link = MoySkladOrderLink.objects.get(pk=link.pk)
        self.assertEqual(self.request_delete(order), "queued")
        self.assertEqual(enqueue_status_task(stale_link.pk), 0)
        self.assertFalse(MoySkladTask.objects.filter(order_link=link, kind="status_sync").exists())
        self.process_delete(self.provider(link))
        self.assertEqual(enqueue_status_task(stale_link.pk), 0)
        self.assertFalse(MoySkladTask.objects.filter(order_link_id=stale_link.pk).exists())

    # Удаление существующего UUID сначала проверяет его аккаунт и идентичность,
    # затем выполняет DELETE только этого заказа и убирает все локальные строки.
    def test_known_external_order_is_verified_deleted_then_cleaned_locally(self):
        order, link = self.queued_order()
        client = self.provider(link)
        self.process_delete(client)
        client.get.assert_called_once_with(f"entity/customerorder/{link.external_id}")
        client.delete.assert_called_once_with(f"entity/customerorder/{link.external_id}")
        client.post.assert_not_called()
        client.put.assert_not_called()
        self.assert_cleaned(order, link)

    # Уже отсутствующий UUID означает успешный итог, при GET и при DELETE.
    def test_known_external_404_at_either_step_finishes_local_cleanup(self):
        from integrations.moysklad.contracts import MoySkladError

        for stage in ("get", "delete"):
            with self.subTest(stage=stage):
                order, link = self.queued_order()
                client = self.provider(link)
                if stage == "get":
                    def get(path, **kwargs):
                        if path == f"entity/organization/{ORGANIZATION_ID}":
                            return self.organization()
                        raise MoySkladError("not_found", status_code=404)
                    client.get.side_effect = get
                else:
                    client.delete.side_effect = MoySkladError("not_found", status_code=404)
                self.process_delete(client)
                self.assert_cleaned(order, link)
                if stage == "get":
                    client.delete.assert_not_called()
                client.post.assert_not_called()

    # При неопределённой отправке используется только исходный syncId, включая
    # корзину МойСклад; найденная единственная идентичность удаляется без POST.
    def test_uncertain_attempt_finds_original_sync_id_including_trash_then_deletes(self):
        order, link = self.queued_order(external=False)
        identifier = uuid.uuid4()
        document = {"id": str(identifier), "accountId": str(ACCOUNT_ID), "syncId": str(link.sync_id),
                    "meta": {"href": f"{API_BASE}entity/customerorder/{identifier}", "type": "customerorder"},
                    "deleted": True}
        client = self.provider(link)
        client.get.return_value = {"meta": {"size": 1, "limit": 100, "offset": 0}, "rows": [document]}
        self.process_delete(client)
        lookup = client.get.call_args_list[0]
        self.assertEqual(lookup.args, ("entity/customerorder",))
        query = lookup.kwargs["params"]["filter"]
        self.assertIn(f"syncId={link.sync_id}", query)
        self.assertIn("isDeleted=true", query)
        self.assertIn("isDeleted=false", query)
        self.assertTrue(1 <= lookup.kwargs["params"]["limit"] <= 1000)
        client.delete.assert_called_once_with(f"entity/customerorder/{identifier}")
        client.post.assert_not_called()
        self.assert_cleaned(order, link)

    # Полностью проверенный пустой поиск доказывает отсутствие отправки и
    # разрешает локальную очистку без создания или удаления внешнего заказа.
    def test_validated_empty_uncertain_lookup_cleans_locally_without_write(self):
        order, link = self.queued_order(external=False)
        client = self.provider(link)
        client.get.side_effect = lambda path, **kwargs: self.organization() if path == f"entity/organization/{ORGANIZATION_ID}" else {
            "meta": {"size": 0, "limit": 100, "offset": 0}, "rows": [],
        }
        self.process_delete(client)
        self.assert_cleaned(order, link)
        client.delete.assert_not_called()
        client.post.assert_not_called()

    # Отсутствие заказа подтверждается доступом именно к замороженному аккаунту:
    # новый токен другого аккаунта не превращает 404/пустой поиск в удаление.
    def test_negative_lookup_cannot_clean_up_without_verified_frozen_account(self):
        from integrations.moysklad.contracts import MoySkladError
        from integrations.moysklad.worker import MoySkladWorker

        organization_cases = (
            {"id": str(ORGANIZATION_ID), "accountId": str(uuid.uuid4())},
            {"id": str(uuid.uuid4()), "accountId": str(ACCOUNT_ID)},
            {"id": str(ORGANIZATION_ID)},
            MoySkladError("not_found", status_code=404),
        )
        for uncertain in (False, True):
            for organization in organization_cases:
                with self.subTest(uncertain=uncertain, organization=organization):
                    order, link = self.queued_order(external=not uncertain)
                    client = self.provider(link)

                    def get(path, **kwargs):
                        if path == f"entity/organization/{ORGANIZATION_ID}":
                            if isinstance(organization, Exception):
                                raise organization
                            return organization
                        if uncertain:
                            return {"meta": {"size": 0, "offset": 0}, "rows": []}
                        raise MoySkladError("not_found", status_code=404)

                    client.get.side_effect = get
                    self.assertTrue(MoySkladWorker(client=client).run_once())
                    self.assertTrue(Order.objects.filter(pk=order.pk).exists())
                    self.assertTrue(MoySkladOrderLink.objects.filter(pk=link.pk).exists())
                    self.assertEqual(order.items.count(), 1)
                    task = MoySkladTask.objects.get(kind="delete_order", order_link=link)
                    self.assertIn(task.status, ("pending", "manual_review"))
                    client.delete.assert_not_called()
                    client.post.assert_not_called()

    # Неполная/противоречивая пагинация не доказывает отсутствие или уникальность
    # заказа, даже если rows выглядит пустым или содержит один элемент.
    def test_uncertain_lookup_requires_complete_unambiguous_collection(self):
        from integrations.moysklad.worker import MoySkladWorker

        responses = (
            {"rows": []},
            {"meta": {"size": True}, "rows": []},
            {"meta": {"size": 1}, "rows": []},
            {"meta": {"size": 0, "offset": 1}, "rows": []},
            {"meta": {"size": 0, "nextHref": f"{API_BASE}entity/customerorder?offset=100"}, "rows": []},
        )
        for response in responses:
            with self.subTest(response=response):
                order, link = self.queued_order(external=False)
                client = self.provider(link)
                client.get.return_value = response
                self.assertTrue(MoySkladWorker(client=client).run_once())
                self.assertTrue(Order.objects.filter(pk=order.pk).exists())
                self.assertTrue(MoySkladOrderLink.objects.filter(pk=link.pk).exists())
                task = MoySkladTask.objects.get(kind="delete_order", order_link=link)
                self.assertIn(task.status, ("pending", "manual_review"))
                client.delete.assert_not_called()
                client.post.assert_not_called()

    # Чужой аккаунт/UUID, отсутствующий accountId или подменённый syncId
    # никогда не становится разрешением на внешний DELETE.
    def test_conflicting_external_identity_never_deletes_or_cleans_local_request(self):
        from integrations.moysklad.worker import MoySkladWorker

        for uncertain, changes in (
            (False, {"id": str(uuid.uuid4())}),
            (False, {"accountId": str(uuid.uuid4())}),
            (False, {"accountId": None}),
            (True, {"syncId": str(uuid.uuid4())}),
            (True, {"accountId": str(uuid.uuid4())}),
            (True, {"id": "invalid-uuid"}),
        ):
            with self.subTest(uncertain=uncertain, changes=changes):
                order, link = self.queued_order(external=not uncertain)
                client = self.provider(link, include_sync=True)
                document = {**client.get.return_value, **changes}
                client.get.return_value = {"meta": {"size": 1}, "rows": [document]} if uncertain else document
                self.assertTrue(MoySkladWorker(client=client).run_once())
                self.assertTrue(Order.objects.filter(pk=order.pk).exists())
                self.assertEqual(order.items.count(), 1)
                client.delete.assert_not_called()
                client.post.assert_not_called()

    # После смены серверной цели старая задача не обращается ни к старому,
    # ни к новому аккаунту и сохраняет заявку для ручной проверки.
    def test_changed_account_or_namespace_prevents_provider_calls(self):
        from integrations.moysklad.order_deletion import process_delete_task

        for field in ("account_id", "namespace"):
            with self.subTest(field=field):
                order, link = self.queued_order()
                task = self.claim_delete()
                client = self.provider(link)
                with override_settings(MOYSKLAD=moysklad_config() | {field: str(uuid.uuid4())}):
                    process_delete_task(task, client=client)
                self.assertTrue(Order.objects.filter(pk=order.pk).exists())
                client.get.assert_not_called()
                client.delete.assert_not_called()
                client.post.assert_not_called()

    # Транспортные и прикладные ошибки оставляют данные и durable-решение;
    # повторяемые ошибки получают pending, конфликты требуют разбора.
    def test_timeout_rate_limit_server_access_and_conflict_preserve_local_state(self):
        from integrations.moysklad.contracts import MoySkladError
        from integrations.moysklad.worker import MoySkladWorker

        cases = (
            ("timeout", None, True), ("rate_limited", 429, True),
            ("server_error", 503, True), ("access_denied", 403, False),
            ("http_error", 409, False),
        )
        for code, status, retryable in cases:
            with self.subTest(code=code):
                MoySkladApiState.objects.filter(account_id=ACCOUNT_ID).delete()
                self.state()
                order, link = self.queued_order()
                client = self.provider(link)
                client.delete.side_effect = MoySkladError(code, status_code=status, retryable=retryable, retry_after=1 if status == 429 else None)
                self.assertTrue(MoySkladWorker(client=client).run_once())
                self.assertTrue(Order.objects.filter(pk=order.pk).exists())
                self.assertEqual(order.items.count(), 1)
                link.refresh_from_db()
                self.assertIsNotNone(link.deletion_requested_at)
                task = MoySkladTask.objects.get(kind="delete_order", order_link=link)
                self.assertIn(task.status, ("pending", "manual_review"))
                self.assertEqual(task.last_error_code, code)
                if retryable:
                    self.assertEqual(task.status, "pending")
                    self.assertGreater(task.next_attempt_at, timezone.now())
                self.assertIsNone(task.lease_token)
                client.post.assert_not_called()
                MoySkladApiState.objects.filter(account_id=ACCOUNT_ID).delete()

    # Если удаление успело выполниться перед таймаутом, последующий 404
    # завершает исходное решение без создания второго внешнего заказа.
    def test_later_404_after_delete_timeout_finishes_same_task(self):
        from integrations.moysklad.contracts import MoySkladError
        from integrations.moysklad.worker import MoySkladWorker

        order, link = self.queued_order()
        client = self.provider(link)
        client.delete.side_effect = MoySkladError("timeout", retryable=True)
        worker = MoySkladWorker(client=client)
        self.assertTrue(worker.run_once())
        task = MoySkladTask.objects.get(kind="delete_order", order_link=link)
        self.assertEqual(task.status, "pending")
        MoySkladTask.objects.filter(pk=task.pk).update(next_attempt_at=timezone.now())
        def get(path, **kwargs):
            if path == f"entity/organization/{ORGANIZATION_ID}":
                return self.organization()
            raise MoySkladError("not_found", status_code=404)
        client.get.side_effect = get
        self.assertTrue(worker.run_once())
        self.assert_cleaned(order, link)
        self.assertEqual(client.delete.call_count, 1)
        client.post.assert_not_called()

    # Общая пауза может начаться после GET: throttle сообщает access_denied
    # без HTTP status_code. Это ожидание доступа, после которого та же задача
    # автоматически продолжает удаление, сохраняя исходное решение владельца.
    def test_code_only_access_pause_after_get_retries_same_delete_task_after_resume(self):
        from integrations.moysklad.contracts import MoySkladError
        from integrations.moysklad.order_deletion import process_delete_task
        from integrations.moysklad.throttling import ApiThrottle

        order, link = self.queued_order()
        link.refresh_from_db()
        marker = (link.deletion_requested_at, link.deletion_requested_by_id)
        task = self.claim_delete()
        client = self.provider(link)
        document = client.get.return_value
        throttle = ApiThrottle()

        def pause_after_get(path, **kwargs):
            throttle.pause_access()
            return document

        client.get.side_effect = pause_after_get
        client.delete.side_effect = MoySkladError("access_denied")
        process_delete_task(task, client=client)
        task.refresh_from_db()
        link.refresh_from_db()
        self.assertEqual(task.status, "pending")
        self.assertEqual(task.last_error_code, "access_denied")
        self.assertIsNone(task.lease_token)
        self.assertTrue(Order.objects.filter(pk=order.pk).exists())
        self.assertEqual(order.items.count(), 1)
        self.assertEqual((link.deletion_requested_at, link.deletion_requested_by_id), marker)
        self.assertEqual(MoySkladTask.objects.filter(kind="delete_order", order_link=link).count(), 1)

        throttle.clear_access_pause()
        MoySkladTask.objects.filter(pk=task.pk).update(next_attempt_at=timezone.now())
        client.get.side_effect = None
        client.delete.side_effect = None
        resumed = self.claim_delete()
        self.assertEqual(resumed.pk, task.pk)
        process_delete_task(resumed, client=client)
        self.assert_cleaned(order, link)
        client.post.assert_not_called()

    # Пауза и отключение потока отправки не блокируют исполнение уже принятого
    # удаления; действующая блокировка доступа аккаунта всё ещё обязательна.
    def test_worker_deletion_ignores_export_pause_but_honors_account_access_gate(self):
        from integrations.moysklad.worker import MoySkladWorker

        order, link = self.queued_order()
        self.state(export_enabled=False)
        api = MoySkladApiState.objects.create(account_id=ACCOUNT_ID, export_paused=True, access_error_code="access_denied")
        client = self.provider(link)
        worker = MoySkladWorker(config=moysklad_config(enabled=False), client=client)
        self.assertFalse(worker.run_once())
        client.get.assert_not_called()
        client.delete.assert_not_called()
        api.access_error_code = ""
        api.api_paused_until = timezone.now() + timedelta(minutes=1)
        api.save()
        self.assertFalse(worker.run_once())
        api.api_paused_until = None
        api.save()
        self.assertTrue(worker.run_once())
        self.assert_cleaned(order, link)

    # После durable-запроса на удаление ни согласование, ни исправление,
    # ни ручной повтор не могут снова включить передачу той же заявки.
    def test_queued_deletion_blocks_all_send_repair_actions_without_mutation(self):
        from integrations.moysklad.order_export import (
            request_existing_order_link, request_export_resolution, request_order_export,
            request_order_retry, set_export_override,
        )

        order, link = self.queued_order(external=False)
        operations = (
            lambda: request_order_export(order.pk, actor=self.owner),
            lambda: request_order_retry(link.pk, actor=self.owner, reason="Синтетический повтор"),
            lambda: request_export_resolution(link.pk, actor=self.owner, reason="Синтетическая проверка"),
            lambda: request_existing_order_link(link.pk, uuid.uuid4(), actor=self.owner, reason="Синтетическая связь"),
            lambda: set_export_override(link.pk, {}, actor=self.owner, reason="Синтетическое исправление"),
        )
        for operation in operations:
            before = self.snapshot()
            with self.assertRaises(ValidationError):
                operation()
            self.assertEqual(self.snapshot(), before)

    # Оба планировщика пропускают помеченную заявку, чтобы статусная очередь
    # не удерживала уже согласованное удаление.
    def test_queued_deletion_is_excluded_from_both_status_schedulers(self):
        from integrations.moysklad.status_sync import schedule_status_tasks
        from integrations.moysklad.worker import MoySkladWorker

        order, link = self.queued_order()
        state = self.state(statuses_enabled=True)
        self.assertEqual(schedule_status_tasks(), 0)
        worker = MoySkladWorker(client=self.provider(link))
        worker._schedule_statuses(state, worker.config, timezone.now())
        self.assertFalse(MoySkladTask.objects.filter(order_link=link, kind="status_sync").exists())

    # Завершённая или требующая разбора задача сохраняет постоянную идентичность:
    # обычный enqueue по тому же ключу не создаёт второй delete_order.
    def test_delete_task_identity_is_permanent_after_completion_or_manual_review(self):
        from integrations.moysklad.queue import enqueue_task

        order, link = self.queued_order()
        task = MoySkladTask.objects.get(kind="delete_order", order_link=link)
        for status in ("done", "manual_review"):
            with self.subTest(status=status):
                MoySkladTask.objects.filter(pk=task.pk).update(status=status)
                before = self.snapshot()
                again = enqueue_task(kind="delete_order", dedupe_key=task.dedupe_key,
                                     account_id=ACCOUNT_ID, environment=ENVIRONMENT, order_link=link,
                                     object_id=str(order.pk))
                self.assertEqual(again.pk, task.pk)
                self.assertEqual(self.snapshot(), before)

    # Повтор подтверждения ожидающего или выполняющегося удаления не меняет
    # время повтора, аудит, маркер решения или действующую аренду.
    def test_confirmed_pending_or_processing_repeat_is_an_exact_noop(self):
        order, link = self.queued_order()
        task = MoySkladTask.objects.get(kind="delete_order", order_link=link)
        for status in ("pending", "processing"):
            with self.subTest(status=status):
                changes = {"status": status, "next_attempt_at": timezone.now() + timedelta(minutes=10)}
                if status == "processing":
                    changes.update(lease_token=uuid.uuid4(), lease_expires_at=timezone.now() + timedelta(minutes=5))
                MoySkladTask.objects.filter(pk=task.pk).update(**changes)
                before = self.snapshot()
                self.assertEqual(self.request_delete(order), "queued")
                self.assertEqual(self.snapshot(), before)

    # Новое явное подтверждение после ошибки возобновляет ту же задачу с
    # аудитом повтора; исходное решение и его автор остаются прежними.
    def test_confirmed_manual_or_failed_retry_reuses_task_and_records_retry(self):
        order, link = self.queued_order()
        task = MoySkladTask.objects.get(kind="delete_order", order_link=link)
        link.refresh_from_db()
        marker = (link.deletion_requested_at, link.deletion_requested_by_id)
        login_verified_admin(self.client, self.owner)
        for status in ("manual_review", "failed"):
            with self.subTest(status=status):
                MoySkladTask.objects.filter(pk=task.pk).update(
                    status=status, last_error_code="conflict", completed_at=timezone.now(),
                    next_attempt_at=timezone.now() + timedelta(hours=1),
                )
                audit_before = list(link.audit_log)
                response = self.client.post(self.delete_url(order), {"confirm": "yes"})
                self.assertEqual(response.status_code, 302)
                task.refresh_from_db()
                link.refresh_from_db()
                self.assertEqual(task.status, "pending")
                self.assertLessEqual(task.next_attempt_at, timezone.now())
                self.assertEqual(task.last_error_code, "")
                self.assertIsNone(task.completed_at)
                self.assertEqual(MoySkladTask.objects.filter(kind="delete_order", order_link=link).count(), 1)
                self.assertEqual((link.deletion_requested_at, link.deletion_requested_by_id), marker)
                self.assertEqual(link.audit_log[:-1], audit_before)
                self.assertEqual(link.audit_log[-1]["action"], "retry_deletion")

    # Уже выполняющийся экспорт или чтение статуса не может потерять объект
    # под своей активной арендой из-за подтверждения владельца.
    @override_settings(MOYSKLAD=moysklad_config())
    def test_active_non_delete_task_rejects_without_deletion_marker(self):
        for kind in ("export_order", "status_sync"):
            with self.subTest(kind=kind):
                order = self.make_order()
                link = self.make_link(order, external=kind == "status_sync", attempted=kind == "status_sync")
                self.export_task(link, kind=kind, status="processing")
                before = self.snapshot()
                with self.assertRaises(ValidationError):
                    self.request_delete(order)
                self.assertEqual(self.snapshot(), before)

    # Сервисная граница также проверяет оба нативных разрешения.
    def test_services_require_view_and_native_delete_permissions(self):
        from inquiries.services import delete_inquiry

        for obj, delete in ((self.make_order(), lambda actor, obj: self.request_delete(obj, actor=actor)),
                            (self.make_inquiry(), lambda actor, obj: delete_inquiry(obj.pk, actor=actor))):
            app, name = obj._meta.app_label, obj._meta.model_name
            for granted in (f"view_{name}", f"delete_{name}"):
                user = get_user_model().objects.create_user(username=f"service-{name}-{granted}", is_staff=True)
                user.user_permissions.add(Permission.objects.get(content_type__app_label=app, codename=granted))
                before = self.snapshot()
                with self.assertRaises(PermissionDenied):
                    delete(user, obj)
                self.assertEqual(self.snapshot(), before)


@override_settings(MOYSKLAD=moysklad_config())
class RequestDeletionConcurrencyTests(RequestDeletionFixtures, TransactionTestCase):
    def invoke(self, operation):
        close_old_connections()
        try:
            with connections["default"].cursor() as cursor:
                cursor.execute("SET lock_timeout = '10s'")
                cursor.execute("SET statement_timeout = '20s'")
            return operation()
        finally:
            connections.close_all()

    # Старый export уже прошёл проверку task lease, но ещё не получил API slot.
    # После истечения task lease новый worker проверяет отсутствие и удаляет
    # заявку; возобновлённый клиент не должен создать бесхозный customerorder.
    def test_export_stalled_before_api_acquire_cannot_post_after_deletion_purge(self):
        from integrations.moysklad.client import MoySkladClient
        from integrations.moysklad.order_export import process_export_task
        from integrations.moysklad.queue import claim_task, ensure_lease
        from integrations.moysklad.throttling import ApiThrottle
        from integrations.moysklad.worker import MoySkladWorker

        product_id = uuid.uuid4()
        order = self.make_order(
            external_account_id_snapshot=ACCOUNT_ID, external_entity_type_snapshot="product",
            external_id_snapshot=product_id, unit_id_snapshot=UNIT_KG_ID,
        )
        link = self.make_link(order, external=False, attempted=False)
        self.export_task(link, status="pending")
        stale = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("export_order",))
        self.assertIsNotNone(stale)
        config = moysklad_config()
        now = [timezone.now()]
        calls, documents, resumed_tokens = [], [], []

        def advance(seconds):
            now[0] += timedelta(seconds=seconds)

        def respond(request):
            path = str(request.url).split("?", 1)[0].removeprefix(API_BASE)
            calls.append((request.method, path))
            if request.method == "GET" and path == f"entity/product/{product_id}":
                return httpx.Response(200, json=product_payload(product_id), request=request)
            if request.method == "GET" and path == "entity/customerorder":
                self.assertEqual(request.url.params["filter"],
                                 f"syncId={link.sync_id};isDeleted=true;isDeleted=false")
                return httpx.Response(200, json={
                    "meta": {"size": len(documents), "offset": 0, "limit": 100}, "rows": documents,
                }, request=request)
            if request.method == "GET" and path == f"entity/organization/{ORGANIZATION_ID}":
                return httpx.Response(200, json=self.organization(), request=request)
            if request.method == "POST" and path == "entity/customerorder":
                document = {
                    **json.loads(request.content), "id": str(uuid.uuid4()), "accountId": str(ACCOUNT_ID),
                }
                documents.append(document)
                return httpx.Response(200, json=document, request=request)
            raise AssertionError(f"Unexpected provider request: {request.method} {path}")

        with MoySkladClient(config=config, transport=httpx.MockTransport(respond)) as exporter, \
                MoySkladClient(config=config, transport=httpx.MockTransport(respond)) as deleter:
            # MockTransport normally bypasses throttling; retain the real shared
            # account leases here so only the interleaving and provider are fake.
            exporter._throttle = ApiThrottle(config, now=lambda: now[0], sleep=advance)
            deleter._throttle = ApiThrottle(config, now=lambda: now[0], sleep=advance)
            acquire = exporter._throttle.acquire

            def delete_before_acquire(*, access_probe=False):
                if not MoySkladOrderLink.objects.filter(pk=link.pk, attempted=True).exists():
                    return acquire(access_probe=access_probe)
                self.assertTrue(ensure_lease(stale))
                self.assertFalse(connection.in_atomic_block)
                MoySkladTask.objects.filter(pk=stale.pk).update(
                    lease_expires_at=timezone.now() - timedelta(seconds=1),
                )
                self.assertEqual(self.request_delete(order), "queued")
                self.assertTrue(MoySkladWorker(config=config, client=deleter).run_once())
                self.assertEqual(calls[-2:], [
                    ("GET", "entity/customerorder"), ("GET", f"entity/organization/{ORGANIZATION_ID}"),
                ])
                self.assertFalse(Order.objects.filter(pk=order.pk).exists())
                self.assertFalse(MoySkladOrderLink.objects.filter(pk=link.pk).exists())
                self.assertFalse(MoySkladTask.objects.filter(object_id=str(order.pk)).exists())
                self.assertEqual(documents, [])
                token = acquire(access_probe=access_probe)
                resumed_tokens.append(token)
                return token

            with patch.object(exporter._throttle, "acquire", side_effect=delete_before_acquire):
                process_export_task(stale, client=exporter)

        self.assertEqual(len(resumed_tokens), 1, "The stale exporter never resumed API acquisition")
        self.assertEqual([call for call in calls if call[0] == "POST"], [],
                         "An expired export sent customerorder after the deletion worker purged its task")
        self.assertEqual(documents, [], "A customerorder survives without its local Order/link/task")

    # Настоящие commit/rollback позволяют подтвердить, что ни GET, ни DELETE
    # не выполняются внутри открытой транзакции приложения.
    def test_provider_io_occurs_outside_database_transactions(self):
        order = self.make_order()
        link = self.make_link(order)
        self.assertEqual(self.request_delete(order), "queued")
        client = self.provider(link)
        document = client.get.return_value

        def get(path, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            return document

        def delete(path):
            self.assertFalse(connection.in_atomic_block)

        client.get.side_effect = get
        client.delete.side_effect = delete
        self.process_delete(client)
        self.assertFalse(Order.objects.filter(pk=order.pk).exists())

    # Между GET и DELETE другой исполнитель получает истёкшую аренду. Старый
    # исполнитель не имеет права удалить внешний заказ или завершить очистку.
    def test_reclaimed_task_lease_between_read_and_delete_fences_stale_worker(self):
        from integrations.moysklad.order_deletion import process_delete_task
        from integrations.moysklad.queue import claim_task

        order = self.make_order()
        link = self.make_link(order)
        self.request_delete(order)
        stale = self.claim_delete()
        client = self.provider(link)
        document = client.get.return_value
        reclaimed = []

        def reclaim():
            MoySkladTask.objects.filter(pk=stale.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
            return claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("delete_order",))

        def get(path, **kwargs):
            with ThreadPoolExecutor(max_workers=1) as executor:
                reclaimed.append(executor.submit(self.invoke, reclaim).result(timeout=15))
            return document

        client.get.side_effect = get
        process_delete_task(stale, client=client)
        client.delete.assert_not_called()
        self.assertTrue(Order.objects.filter(pk=order.pk).exists())
        current = MoySkladTask.objects.get(pk=stale.pk)
        self.assertEqual(current.status, "processing")
        self.assertEqual(current.lease_token, reclaimed[0].lease_token)
        self.assertNotEqual(current.lease_token, stale.lease_token)

    # Удаление удерживает строку Order до commit. Конкурирующее согласование
    # отправки действительно ждёт PostgreSQL lock и после удаления не создаёт
    # ни связь, ни задачу для уже исчезнувшей заявки.
    def test_deletion_serializes_against_concurrent_first_export_approval(self):
        from integrations.moysklad.order_export import request_order_export

        order = self.make_order()
        self.state()
        self.outbox(order)
        deleting = Event()
        release = Event()
        approving = Event()
        pids = {}

        def pause_delete(sender, instance, **kwargs):
            if instance.pk != order.pk:
                return
            with connections["default"].cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                pids["deletion"] = cursor.fetchone()[0]
            deleting.set()
            if not release.wait(timeout=10):
                raise AssertionError("Deletion was not released by the concurrency test")

        def approve():
            actor = get_user_model().objects.get(pk=self.owner.pk)
            with connections["default"].cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                pids["approval"] = cursor.fetchone()[0]
            approving.set()
            try:
                request_order_export(order.pk, actor=actor)
            except (Order.DoesNotExist, ValidationError):
                return "rejected"
            return "approved"

        pre_delete.connect(pause_delete, sender=Order, weak=False)
        blocked = False
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                deletion = executor.submit(self.invoke, lambda: self.request_delete(order))
                self.assertTrue(deleting.wait(timeout=5), "Deletion never reached its atomic cleanup")
                approval = executor.submit(self.invoke, approve)
                try:
                    self.assertTrue(approving.wait(timeout=5))
                    deadline = timezone.now() + timedelta(seconds=5)
                    while timezone.now() < deadline and not approval.done():
                        with connection.cursor() as cursor:
                            cursor.execute("SELECT %s = ANY(pg_blocking_pids(%s))", (pids["deletion"], pids["approval"]))
                            blocked = cursor.fetchone()[0]
                        if blocked:
                            break
                        Event().wait(0.01)
                finally:
                    release.set()
                self.assertEqual(deletion.result(timeout=15), "deleted")
                self.assertEqual(approval.result(timeout=15), "rejected")
        finally:
            release.set()
            pre_delete.disconnect(pause_delete, sender=Order)
        self.assertTrue(blocked, "Export approval did not wait on the deleting Order row")
        self.assertFalse(Order.objects.filter(pk=order.pk).exists())
        self.assertFalse(MoySkladOrderLink.objects.filter(order_id=order.pk).exists())
        self.assertFalse(MoySkladTask.objects.filter(object_id=str(order.pk)).exists())
