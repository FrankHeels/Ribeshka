"""C1 acceptance tests for the durable MoySklad worker and owner controls."""

import uuid
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings

from catalog.models import Category, Product
from integrations.models import MoySkladSyncState, MoySkladTask, MoySkladWebhookInbox
from tests.moysklad_helpers import (
    ACCOUNT_ID, AGENT_ID, CURRENCY_ID, ENVIRONMENT, INITIAL_STATE_ID, NAMESPACE, ORGANIZATION_ID,
    PRICE_TYPE_ID, UNIT_KG_ID, moysklad_config, product_payload,
)


def owner_user():
    return get_user_model().objects.create_superuser(
        username=f"ms-owner-{uuid.uuid4().hex}", email=f"{uuid.uuid4().hex}@example.test",
        password="test-password",
    )


@override_settings(MOYSKLAD=moysklad_config())
class MoySkladWorkerCommandTests(TestCase):
    # --once завершает bounded проход после не более одной готовой задачи,
    # чтобы scheduler можно было безопасно запускать одноразовым процессом.
    def test_process_command_once_starts_and_stops_worker(self):
        from integrations.moysklad.worker import MoySkladWorker

        with patch.object(MoySkladWorker, "run_once", return_value=False) as run_once:
            call_command("process_moysklad", once=True)
        run_once.assert_called_once()

    # Команды ручного полного каталога и цен только ставят работу в очередь;
    # при их вызове ни один клиентский HTTP метод не выполняется.
    def test_manual_sync_commands_enqueue_without_http(self):
        with patch("integrations.moysklad.client.MoySkladClient") as client_class:
            call_command("sync_moysklad_catalog")
            call_command("reconcile_moysklad_prices")
        client_class.assert_not_called()
        self.assertTrue(MoySkladTask.objects.filter(kind="catalog_sync").exists())
        self.assertTrue(MoySkladTask.objects.filter(kind="price_reconciliation").exists())

    # Диагностическая проверка API read-only: ни успешный run, ни ошибка
    # сетевого чтения не запускают create/update/delete HTTP операции.
    def test_check_command_uses_reads_without_post(self):
        with patch("integrations.moysklad.client.MoySkladClient") as client_class:
            client = client_class.return_value.__enter__.return_value
            client_class.return_value.__enter__.return_value = client

            def read_resource(path, params=None):
                account = {"accountId": str(ACCOUNT_ID)}
                if path == "entity/product":
                    return {"rows": [product_payload()]}
                if path == f"entity/currency/{CURRENCY_ID}":
                    return account | {"id": str(CURRENCY_ID), "name": "Российский рубль", "code": "RUB"}
                if path == f"entity/uom/{UNIT_KG_ID}":
                    return account | {"id": str(UNIT_KG_ID), "name": "кг"}
                if path == f"context/companysettings/pricetype/{PRICE_TYPE_ID}":
                    return account | {"id": str(PRICE_TYPE_ID), "name": "Цена Розницы", "externalCode": "retail"}
                if path == f"entity/organization/{ORGANIZATION_ID}":
                    return account | {"id": str(ORGANIZATION_ID), "name": "Организация"}
                if path == f"entity/counterparty/{AGENT_ID}":
                    return account | {"id": str(AGENT_ID), "name": "Покупатель сайта"}
                if path == "entity/customerorder/metadata":
                    return {"states": [{"id": str(INITIAL_STATE_ID), "name": "Новый",
                                        "stateType": {"name": "Regular"}}]}
                raise AssertionError(f"Unexpected probe GET path: {path}")

            client.get.side_effect = read_resource
            call_command("check_moysklad")
        client.get.assert_called()
        client.post.assert_not_called()
        client.put.assert_not_called()
        client.delete.assert_not_called()
        requested_paths = {call.args[0] for call in client.get.call_args_list}
        self.assertTrue({
            "entity/product",
            f"entity/currency/{CURRENCY_ID}",
            f"entity/uom/{UNIT_KG_ID}",
            f"context/companysettings/pricetype/{PRICE_TYPE_ID}",
            f"entity/organization/{ORGANIZATION_ID}",
            f"entity/counterparty/{AGENT_ID}",
            "entity/customerorder/metadata",
        }.issubset(requested_paths))


@override_settings(MOYSKLAD=moysklad_config())
class MoySkladOwnerControlTests(TestCase):
    # Включение потока требует владельца и проверенную конфигурацию; действие
    # владельца сохраняет актёра и не выполняет API запрос синхронно.
    def test_stream_control_requires_owner_and_records_manual_action(self):
        from integrations.moysklad.controls import set_stream_enabled

        owner = owner_user()
        staff = get_user_model().objects.create_user(username=f"staff-{uuid.uuid4().hex}", is_staff=True)
        with self.assertRaises(PermissionDenied):
            set_stream_enabled("prices", True, actor=staff)
        with patch("integrations.moysklad.client.MoySkladClient") as client_class:
            state = set_stream_enabled("prices", True, actor=owner)
        client_class.assert_not_called()
        self.assertTrue(state.prices_enabled)
        self.assertEqual(state.account_id, ACCOUNT_ID)

    # Ручной экспорт разрешён только владельцу при проверенной конфигурации;
    # он создаёт очередь и не отправляет заказ из запроса панели.
    def test_start_export_requires_owner_and_only_enqueues(self):
        from integrations.moysklad.controls import start_export

        owner = owner_user()
        staff = get_user_model().objects.create_user(username=f"staff-{uuid.uuid4().hex}", is_staff=True)
        with self.assertRaises(PermissionDenied):
            start_export(actor=staff)
        with patch("integrations.moysklad.client.MoySkladClient") as client_class:
            start_export(actor=owner)
        client_class.assert_not_called()


@override_settings(MOYSKLAD=moysklad_config())
class MoySkladWebhookRegistrationTests(TestCase):
    def register_with_config(self, config):
        output = StringIO()
        with (
            override_settings(MOYSKLAD=config),
            patch("integrations.management.commands.configure_moysklad_webhook.locked_state"),
            patch("integrations.management.commands.configure_moysklad_webhook.configure_webhook",
                  return_value="webhook-test-id"),
            patch("integrations.moysklad.client.MoySkladClient") as client_class,
        ):
            call_command("configure_moysklad_webhook", stdout=output)
        return client_class.call_args, output.getvalue()

    def test_webhook_configuration_falls_back_to_api_token(self):
        call, output = self.register_with_config(moysklad_config() | {"admin_token": ""})

        self.assertEqual(call.args[0].api_token, moysklad_config()["api_token"])
        self.assertTrue(call.kwargs["webhook_management"])
        self.assertIn("webhook_configured id=webhook-test-id", output)

    def test_webhook_configuration_prefers_admin_token_when_configured(self):
        admin_token = "synthetic-admin-token"
        call, _output = self.register_with_config(
            moysklad_config() | {"admin_token": admin_token}
        )

        self.assertEqual(call.args[0].api_token, admin_token)
        self.assertTrue(call.kwargs["webhook_management"])

    def test_same_admin_and_api_token_is_allowed_for_webhook_configuration(self):
        api_token = moysklad_config()["api_token"]
        call, _output = self.register_with_config(
            moysklad_config() | {"admin_token": api_token}
        )

        self.assertEqual(call.args[0].api_token, api_token)
        self.assertTrue(call.kwargs["webhook_management"])


class MoySkladWebhookWriteGuardTests(SimpleTestCase):
    def test_webhook_management_client_can_post_and_put_webhook(self):
        from integrations.moysklad.client import MoySkladClient

        seen = []
        transport = httpx.MockTransport(
            lambda request: (seen.append(request), httpx.Response(200, json={}, request=request))[1]
        )
        webhook_id = "30000000-0000-4000-8000-000000000077"
        client = MoySkladClient(
            config=moysklad_config(), transport=transport, webhook_management=True,
        )
        try:
            client.post("entity/webhook", json={"url": "https://example.test/hook"})
            client.put(f"entity/webhook/{webhook_id}", json={"enabled": True})
        finally:
            client.close()

        self.assertEqual([request.method for request in seen], ["POST", "PUT"])
        self.assertEqual([request.url.path for request in seen], ["/api/remap/1.2/entity/webhook",
                                                                   f"/api/remap/1.2/entity/webhook/{webhook_id}"])

    def test_ordinary_client_rejects_webhook_post_and_put_before_http(self):
        from integrations.moysklad.client import MoySkladClient
        from integrations.moysklad.contracts import MoySkladError

        seen = []
        transport = httpx.MockTransport(
            lambda request: (seen.append(request), httpx.Response(200, json={}, request=request))[1]
        )
        for method in ("post", "put"):
            with self.subTest(method=method):
                client = MoySkladClient(config=moysklad_config(), transport=transport)
                try:
                    with self.assertRaises(MoySkladError):
                        getattr(client, method)("entity/webhook", json={})
                finally:
                    client.close()
        self.assertEqual(seen, [])

    def test_readonly_probe_rejects_webhook_write_in_every_client_mode(self):
        from integrations.moysklad.client import MoySkladClient
        from integrations.moysklad.contracts import MoySkladError

        seen = []
        transport = httpx.MockTransport(
            lambda request: (seen.append(request), httpx.Response(200, json={}, request=request))[1]
        )
        for webhook_management in (False, True):
            with self.subTest(webhook_management=webhook_management):
                client = MoySkladClient(
                    config=moysklad_config(), transport=transport,
                    readonly_probe=True, webhook_management=webhook_management,
                )
                try:
                    with self.assertRaises(MoySkladError) as caught:
                        client.post("entity/webhook", json={})
                finally:
                    client.close()
                self.assertEqual(caught.exception.code, "readonly_probe")
        self.assertEqual(seen, [])


class MoySkladStatusScheduleTests(TestCase):
    # Активный учётный статус проверяется каждые пять минут, терминальный —
    # раз в сутки, чтобы сохранять актуальность без лишних API чтений.
    def test_status_interval_depends_on_external_status_type(self):
        from integrations.moysklad.status_sync import status_interval

        active = SimpleNamespace(state_type="Regular")
        successful = SimpleNamespace(state_type="Successful")
        unsuccessful = SimpleNamespace(state_type="Unsuccessful")
        self.assertEqual(status_interval(active), timedelta(minutes=5))
        self.assertEqual(status_interval(successful), timedelta(days=1))
        self.assertEqual(status_interval(unsuccessful), timedelta(days=1))


@override_settings(MOYSKLAD=moysklad_config())
class MoySkladWorkerQueueAcceptanceTests(TestCase):
    # Отключённый поток не потребляет retry attempts, а неподходящая среда
    # остаётся вне worker-а текущей конфигурации.
    def test_paused_price_and_foreign_environment_tasks_remain_unclaimed(self):
        from integrations.moysklad.worker import MoySkladWorker

        paused = MoySkladTask.objects.create(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, kind="price",
            dedupe_key=f"paused:{uuid.uuid4()}", object_id=str(uuid.uuid4()),
        )
        foreign = MoySkladTask.objects.create(
            account_id=ACCOUNT_ID, environment="other", kind="price",
            dedupe_key=f"foreign:{uuid.uuid4()}", object_id=str(uuid.uuid4()),
        )
        config = moysklad_config() | {"prices_enabled": False}
        MoySkladWorker(config=config, client=Mock()).run_once()
        paused.refresh_from_db()
        foreign.refresh_from_db()
        self.assertEqual((paused.status, paused.attempt_count), ("pending", 0))
        self.assertEqual((foreign.status, foreign.attempt_count), ("pending", 0))

    # Один bounded tick берёт максимум одну задачу и не запускает полный
    # каталог автоматически, включая первый запуск без ручного owner action.
    def test_single_tick_claims_at_most_one_and_never_starts_catalog(self):
        from integrations.moysklad.worker import MoySkladWorker

        rows = [MoySkladTask.objects.create(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, kind="price",
            dedupe_key=f"bounded:{uuid.uuid4()}", object_id=str(uuid.uuid4()),
        ) for _ in range(3)]
        MoySkladWorker(config=moysklad_config(), client=Mock()).run_once()
        for task in rows:
            task.refresh_from_db()
        self.assertLessEqual(sum(task.attempt_count for task in rows), 1)
        self.assertFalse(MoySkladTask.objects.filter(kind="catalog_sync").exists())

    # При выключенном применении цен worker всё ещё разбирает долговечный
    # webhook inbox, фиксируя событие и оставляя price task ожидающей.
    def test_inbox_processing_continues_while_price_stream_is_paused(self):
        from integrations.moysklad.catalog_sync import link_product, upsert_product
        from integrations.moysklad.worker import MoySkladWorker

        category = Category.objects.create(name="Webhook", slug=f"hook-{uuid.uuid4().hex}")
        product = Product.objects.create(name="Рыба", slug=f"fish-{uuid.uuid4().hex}", category=category,
                                         price_per_kg="20.00")
        external_id = uuid.uuid4()
        owner = owner_user()
        source = upsert_product(product_payload(external_id))
        link_product(product_id=product.pk, external_product_id=source.external_id, actor=owner)
        inbox = MoySkladWebhookInbox.objects.create(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, request_id=uuid.uuid4(),
            payload_hash="0" * 64, events=[{"external_id": str(external_id), "action": "UPDATE"}],
        )
        task = MoySkladTask.objects.create(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, kind="webhook",
            dedupe_key=f"inbox:{inbox.pk}", object_id=str(inbox.pk), inbox=inbox,
        )
        MoySkladSyncState.objects.create(account_id=ACCOUNT_ID, environment=ENVIRONMENT,
                                         namespace=NAMESPACE, prices_enabled=False)

        MoySkladWorker(config=moysklad_config() | {"prices_enabled": False}, client=Mock()).run_once()

        inbox.refresh_from_db()
        task.refresh_from_db()
        self.assertIsNotNone(inbox.processed_at)
        self.assertEqual(task.status, "done")
        self.assertTrue(MoySkladTask.objects.filter(kind="price", external_product=source).exists())
