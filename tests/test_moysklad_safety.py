"""Safety acceptance tests for retries, shared API limits, and response bodies."""

import asyncio
import gzip
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
import uuid
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx
from django.db import close_old_connections
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from integrations.models import MoySkladTask, MoySkladSyncState
from tests.moysklad_helpers import ACCOUNT_ID, ENVIRONMENT, moysklad_config


class MoySkladRetrySafetyTests(TransactionTestCase):
    reset_sequences = True

    # Автоматические задачи используют базовые паузы 30/120/600/1800 секунд;
    # начиная с пятой ошибки повторяется 1800 секунд с ограниченным разбросом.
    def test_retry_backoff_and_jitter_are_bounded_and_first_error_is_stable(self):
        from integrations.moysklad.queue import claim_task, enqueue_task, retry_task

        task = enqueue_task(
            kind="price", dedupe_key=f"backoff:{uuid.uuid4()}",
            account_id=ACCOUNT_ID, environment=ENVIRONMENT,
        )
        expected = (30, 120, 600, 1800, 1800, 1800)
        first_error_at = None
        for attempt, base in enumerate(expected, start=1):
            MoySkladTask.objects.filter(pk=task.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
            claimed = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("price",))
            self.assertEqual(claimed.pk, task.pk)
            now = timezone.now()
            if attempt < 5:
                patch_jitter = patch("random.uniform")
            else:
                patch_jitter = patch("random.uniform", return_value=1.0)
            with patch("integrations.moysklad.queue.timezone.now", return_value=now), patch_jitter as jitter:
                retry_task(claimed, "temporary_api_error")
            task.refresh_from_db()
            delay = (task.next_attempt_at - now).total_seconds()
            if attempt < 5:
                self.assertEqual(delay, base)
                jitter.assert_not_called()
            else:
                jitter.assert_called_once_with(0.8, 1.2)
                self.assertGreaterEqual(delay, base * 0.8)
                self.assertLessEqual(delay, base * 1.2)
            self.assertEqual(task.status, MoySkladTask.Status.PENDING)
            self.assertEqual(task.last_error_code, "temporary_api_error")
            if first_error_at is None:
                first_error_at = task.first_error_at
                self.assertIsNotNone(first_error_at)
            else:
                self.assertEqual(task.first_error_at, first_error_at)

        MoySkladTask.objects.filter(pk=task.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        claimed = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("price",))
        from integrations.moysklad.queue import complete_task

        complete_task(claimed)
        task.refresh_from_db()
        self.assertIsNone(task.first_error_at)

    # Возраст давно созданной, но только что упавшей задачи не означает
    # длительный API-инцидент: отсчёт ведётся от первой ошибки.
    @override_settings(MOYSKLAD=moysklad_config())
    def test_incident_monitor_uses_first_failure_time_instead_of_task_age(self):
        from integrations.moysklad.incidents import monitor_integration
        from integrations.models import MoySkladIncident

        now = timezone.now()
        MoySkladSyncState.objects.create(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT,
            namespace=uuid.UUID(moysklad_config()["namespace"]), heartbeat_at=now,
            catalog_enabled=False, prices_enabled=False, export_enabled=False, statuses_enabled=False,
        )
        task = MoySkladTask.objects.create(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, kind="price",
            dedupe_key=f"old-row:{uuid.uuid4()}", status="pending", last_error_code="temporary_api_error",
        )
        MoySkladTask.objects.filter(pk=task.pk).update(created_at=now - timedelta(hours=3))
        monitor_integration(now=now)
        self.assertFalse(MoySkladIncident.objects.filter(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, scope="api", code="temporary_failure",
        ).exists())

    # Подтверждённая первая ошибка старше 15 минут создаёт API-инцидент даже
    # если сама задача создана только что; возраст задачи не подменяет таймер.
    @override_settings(MOYSKLAD=moysklad_config())
    def test_incident_opens_after_first_error_has_persisted_for_fifteen_minutes(self):
        from integrations.moysklad.incidents import monitor_integration
        from integrations.models import MoySkladIncident

        now = timezone.now()
        self.assertIn("first_error_at", {field.name for field in MoySkladTask._meta.fields})
        MoySkladSyncState.objects.create(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT,
            namespace=uuid.UUID(moysklad_config()["namespace"]), heartbeat_at=now,
            catalog_enabled=False, prices_enabled=False, export_enabled=False, statuses_enabled=False,
        )
        MoySkladTask.objects.create(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, kind="price",
            dedupe_key=f"first-error:{uuid.uuid4()}", status="pending",
            last_error_code="temporary_api_error", first_error_at=now - timedelta(minutes=16),
        )

        monitor_integration(now=now)

        self.assertTrue(MoySkladIncident.objects.filter(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, scope="api", code="temporary_failure",
            closed_at__isnull=True,
        ).exists())


class MoySkladAccountThrottleTests(TransactionTestCase):
    reset_sequences = True

    def config(self, environment, namespace):
        return moysklad_config() | {"environment": environment, "namespace": str(namespace)}

    # Лимит и пауза API относятся к аккаунту: разные namespaces и окружения
    # не создают независимые слоты для того же account_id.
    @override_settings(MOYSKLAD_TEST_MODE=False)
    def test_different_environments_share_one_account_lease_and_pause(self):
        from integrations.moysklad.contracts import MoySkladError
        from integrations.moysklad.throttling import ApiThrottle
        from integrations.moysklad.models import MoySkladApiState

        first = ApiThrottle(self.config("sandbox-a", uuid.uuid4()), sleep=lambda _delay: None)
        second = ApiThrottle(self.config("sandbox-b", uuid.uuid4()), sleep=lambda _delay: None)
        token = first.acquire()
        with self.assertRaises(MoySkladError) as caught:
            second.acquire()
        self.assertEqual(caught.exception.code, "api_busy")
        self.assertEqual(MoySkladApiState.objects.filter(account_id=ACCOUNT_ID).count(), 1)
        first.pause_access()
        with self.assertRaises(MoySkladError) as caught:
            second.acquire()
        self.assertEqual(caught.exception.code, "access_denied")
        first.release(token)


    # Pre-request heartbeat продлевает ещё действующий lease до now+70 секунд;
    # после просрочки прежний владелец уже не может продлить новый lease.
    @override_settings(MOYSKLAD_TEST_MODE=False)
    def test_heartbeat_renews_owned_lease_and_stale_token_cannot_renew_replacement(self):
        from integrations.moysklad.contracts import LeaseLost
        from integrations.moysklad.models import MoySkladApiState
        from integrations.moysklad.throttling import API_LEASE_SECONDS, ApiThrottle

        now = [timezone.now()]
        clock = lambda: now[0]
        config = self.config("sandbox-a", uuid.uuid4())
        first = ApiThrottle(config, now=clock, sleep=lambda _delay: None)
        token = first.acquire()
        original_expiry = MoySkladApiState.objects.get(account_id=ACCOUNT_ID).api_lease_expires_at
        now[0] += timedelta(seconds=60)
        first.heartbeat(token)
        state = MoySkladApiState.objects.get(account_id=ACCOUNT_ID)
        self.assertEqual(state.api_lease_expires_at, now[0] + timedelta(seconds=API_LEASE_SECONDS))
        self.assertGreater(state.api_lease_expires_at, original_expiry)

        now[0] += timedelta(seconds=71)
        second = ApiThrottle(self.config("sandbox-b", uuid.uuid4()), now=clock, sleep=lambda _delay: None)
        replacement = second.acquire()
        with self.assertRaises(LeaseLost):
            first.heartbeat(token)
        state.refresh_from_db()
        self.assertEqual(state.api_lease_token, replacement)

    # Одновременное первое обращение создаёт один аккаунтный throttle row и
    # выдаёт не более одного актуального слота.
    @override_settings(MOYSKLAD_TEST_MODE=False)
    def test_concurrent_first_acquire_creates_one_account_state_and_one_lease(self):
        from integrations.moysklad.contracts import MoySkladError
        from integrations.moysklad.models import MoySkladApiState
        from integrations.moysklad.throttling import ApiThrottle

        barrier = threading.Barrier(2)

        def acquire(environment):
            close_old_connections()
            try:
                throttle = ApiThrottle(self.config(environment, uuid.uuid4()), sleep=lambda _delay: None)
                barrier.wait(timeout=5)
                try:
                    return ("ok", throttle.acquire())
                except MoySkladError as error:
                    return (error.code, None)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(acquire, ("sandbox-a", "sandbox-b")))
        self.assertEqual(sum(result[0] == "ok" for result in results), 1)
        self.assertEqual(MoySkladApiState.objects.filter(account_id=ACCOUNT_ID).count(), 1)


class MoySkladQueueRaceTests(TransactionTestCase):
    reset_sequences = True

    # Два параллельных enqueue одного dedupe key возвращают одну запись,
    # даже если оба запроса начинают проверку до commit конкурента.
    def test_concurrent_enqueue_same_key_creates_one_task(self):
        from integrations.moysklad.queue import enqueue_task

        key = f"enqueue-race:{uuid.uuid4()}"
        barrier = threading.Barrier(2)

        def enqueue():
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                task = enqueue_task(kind="price", dedupe_key=key, account_id=ACCOUNT_ID,
                                    environment=ENVIRONMENT)
                return task.pk
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(lambda _unused: enqueue(), range(2)))
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(MoySkladTask.objects.filter(dedupe_key=key).count(), 1)

    # Два worker-а, заявивших один ready task одновременно, получают ровно
    # одного владельца; второй не может перевести ту же задачу в processing.
    def test_concurrent_claim_assigns_one_owner(self):
        from integrations.moysklad.queue import claim_task, enqueue_task

        task = enqueue_task(kind="price", dedupe_key=f"claim-race:{uuid.uuid4()}",
                            account_id=ACCOUNT_ID, environment=ENVIRONMENT)
        barrier = threading.Barrier(2)

        def claim():
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                claimed = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("price",))
                return claimed.pk if claimed else None
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(lambda _unused: claim(), range(2)))
        self.assertEqual(ids.count(task.pk), 1)
        self.assertEqual(ids.count(None), 1)
        task.refresh_from_db()
        self.assertEqual(task.status, MoySkladTask.Status.PROCESSING)


@override_settings(MOYSKLAD=moysklad_config())
class MoySkladMetadataPriceIsolationTests(TransactionTestCase):
    # Поздняя цена не должна трактоваться как версия редакционных метаданных:
    # archived из t3 остаётся блокировкой, а t2 не продвигает базовую цену.
    def test_stale_price_read_cannot_clear_newer_archived_metadata(self):
        from django.contrib.auth import get_user_model
        from catalog.models import Category, Product
        from integrations.moysklad.catalog_sync import link_product, upsert_product
        from integrations.moysklad.price_sync import process_price_task, request_price
        from integrations.models import MoySkladTask
        from integrations.moysklad.queue import claim_task
        from tests.moysklad_helpers import product_payload

        category = Category.objects.create(name="МойСклад", slug=f"ms-{uuid.uuid4().hex}")
        product = Product.objects.create(name="Карточка", slug=f"card-{uuid.uuid4().hex}", category=category,
                                         price_per_kg="100.00", is_published=True)
        external_id = uuid.uuid4()
        source = upsert_product(product_payload(external_id, updated="2026-10-15 10:00:00.000"))
        owner = get_user_model().objects.create_superuser(
            username=f"owner-{uuid.uuid4().hex}", email=f"{uuid.uuid4().hex}@example.test",
            password="test-password",
        )
        link_product(product_id=product.pk, external_product_id=external_id, actor=owner)
        # Обработать первый запрос, чтобы зафиксировать baseline цены t1.
        request_price(external_product_id=external_id, force_apply=True)
        task = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("price",))
        client = type("Client", (), {"get": lambda self, _path: product_payload(
            external_id, price="125.50", updated="2026-10-15 10:01:00.000")})()
        process_price_task(task, client=client)
        source.refresh_from_db()
        first_checked_price = source.last_checked_price
        first_checked_at = source.price_checked_at

        # t3: новый каталоговый ответ архивирует товар; t2: ранее начатое GET
        # цены возвращает более старые активные метаданные.
        archived = upsert_product(product_payload(external_id, archived=True,
                                                  updated="2026-10-15 10:03:00.000"))
        self.assertTrue(archived.archived)
        pending = request_price(external_product_id=external_id, force_apply=False)
        stale = claim_task(account_id=ACCOUNT_ID, environment=ENVIRONMENT, kinds=("price",))
        stale_client = type("Client", (), {"get": lambda self, _path: product_payload(
            external_id, price="130.00", archived=False, updated="2026-10-15 10:02:00.000")})()

        process_price_task(stale, client=stale_client)

        source.refresh_from_db()
        product.refresh_from_db()
        stale.refresh_from_db()
        self.assertTrue(source.archived)
        self.assertEqual(source.metadata_updated_at,
                         timezone.datetime(2026, 10, 15, 10, 3, tzinfo=ZoneInfo("Europe/Moscow")))
        self.assertEqual((source.last_checked_price, source.price_checked_at),
                         (first_checked_price, first_checked_at))
        self.assertEqual(product.integration_block_reason, "archived")
        self.assertEqual(product.price_per_kg, Decimal("125.50"))
        self.assertEqual(stale.status, "pending")


class MoySkladResponseSizeTests(SimpleTestCase):
    # Предел применяется к распакованному gzip body; ошибка не содержит его
    # фрагментов и использует только безопасный код.
    def test_gzip_response_is_limited_after_decompression(self):
        from integrations.moysklad import client as client_module
        from integrations.moysklad.client import MoySkladClient
        from integrations.moysklad.contracts import MoySkladError

        decoded = b'{"secret":"' + b"x" * 128 + b'"}'
        encoded = gzip.compress(decoded)

        class ResponseBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield encoded

        def respond(request):
            return httpx.Response(
                200, headers={"content-encoding": "gzip"}, stream=ResponseBody(), request=request,
            )

        client = MoySkladClient(config=moysklad_config(), transport=httpx.MockTransport(respond))
        try:
            with patch.object(client_module, "MAX_RESPONSE_BYTES", 32, create=True):
                with self.assertRaises(MoySkladError) as caught:
                    client.get("entity/product")
        finally:
            client.close()
        self.assertEqual(caught.exception.code, "response_too_large")
        self.assertNotIn("x" * 32, str(caught.exception))


class MoySkladCanonicalPriceTypeTests(SimpleTestCase):
    # JSON API возвращает тип цены через context/companysettings; это штатная
    # ссылка должна распознаваться и применяться как выбранная цена аккаунта.
    def test_product_parser_accepts_canonical_price_type_reference(self):
        from integrations.moysklad.config import get_config
        from integrations.moysklad.mapping import API_BASE, parse_product
        from tests.moysklad_helpers import moysklad_config, product_payload

        payload = product_payload()
        payload["salePrices"][0]["priceType"]["meta"]["href"] = (
            f"{API_BASE}context/companysettings/pricetype/{moysklad_config()['price_type_id']}"
        )

        parsed = parse_product(payload, get_config(moysklad_config()))

        self.assertEqual(parsed["price"], Decimal("125.50"))
        self.assertEqual(parsed["last_error_code"], "")


class MoySkladAccessProbeThrottleTests(TransactionTestCase):
    # Проверка восстановления доступа может пройти глобальный access denial;
    # тот же флаг не обходит занятый слот или активный rate-limit.
    @override_settings(MOYSKLAD_TEST_MODE=False)
    def test_access_probe_bypasses_only_access_error(self):
        from integrations.moysklad.contracts import MoySkladError
        from integrations.moysklad.models import MoySkladApiState
        from integrations.moysklad.throttling import ApiThrottle
        from tests.moysklad_helpers import ACCOUNT_ID, moysklad_config

        now = timezone.now()
        state = MoySkladApiState.objects.create(account_id=ACCOUNT_ID, access_error_code="access_denied")
        throttle = ApiThrottle(moysklad_config(), now=lambda: now, sleep=lambda _delay: None)
        with self.assertRaises(MoySkladError) as caught:
            throttle.acquire()
        self.assertEqual(caught.exception.code, "access_denied")
        token = throttle.acquire(access_probe=True)
        throttle.release(token)
        state.refresh_from_db()
        self.assertEqual(state.access_error_code, "access_denied")
        state.api_paused_until = now + timedelta(seconds=45)
        state.save(update_fields=("api_paused_until",))
        throttle.clear_access_pause()
        state.refresh_from_db()
        self.assertEqual(state.access_error_code, "")
        self.assertEqual(state.api_paused_until, now + timedelta(seconds=45))
        state.access_error_code = "access_denied"
        state.api_paused_until = None
        state.api_lease_token = uuid.uuid4()
        state.api_lease_expires_at = now + timedelta(seconds=30)
        state.save(update_fields=("access_error_code", "api_paused_until", "api_lease_token", "api_lease_expires_at"))
        with self.assertRaises(MoySkladError) as caught:
            throttle.acquire(access_probe=True)
        self.assertEqual(caught.exception.code, "api_busy")
        state.api_lease_token = None
        state.api_lease_expires_at = None
        state.api_paused_until = now + timedelta(seconds=30)
        state.save(update_fields=("api_lease_token", "api_lease_expires_at", "api_paused_until"))
        with self.assertRaises(MoySkladError) as caught:
            throttle.acquire(access_probe=True)
        self.assertEqual(caught.exception.code, "rate_limited")

class MoySkladReadonlyProbeClientTests(SimpleTestCase):
    # Операторский probe должен быть явно read-only: даже при тестовом
    # MockTransport POST/PUT запрещены до передачи HTTP в transport.
    def test_client_readonly_probe_allows_get_and_rejects_writes(self):
        import httpx
        from integrations.moysklad.client import MoySkladClient
        from integrations.moysklad.contracts import MoySkladError
        from tests.moysklad_helpers import moysklad_config

        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"ok": True}, request=request)

        client = MoySkladClient(config=moysklad_config(), readonly_probe=True,
                                transport=httpx.MockTransport(respond))
        try:
            self.assertEqual(client.get("entity/product"), {"ok": True})
            for method in (client.post, client.put):
                with self.assertRaises(MoySkladError):
                    method("entity/customerorder", json={})
        finally:
            client.close()
        self.assertEqual([request.method for request in requests], ["GET"])

    # Прайс-тип загружается из context/companysettings, оставаясь на
    # фиксированном API host и внутри разрешённого resource allowlist.
    def test_client_allows_canonical_price_type_resource(self):
        import httpx
        from integrations.moysklad.client import MoySkladClient
        from tests.moysklad_helpers import API_BASE, PRICE_TYPE_ID, moysklad_config

        requests = []
        client = MoySkladClient(config=moysklad_config(),
                                transport=httpx.MockTransport(lambda request: (
                                    requests.append(request) or httpx.Response(200, json={"id": str(PRICE_TYPE_ID)}, request=request)
                                )))
        try:
            result = client.get(f"context/companysettings/pricetype/{PRICE_TYPE_ID}")
        finally:
            client.close()
        self.assertEqual(result["id"], str(PRICE_TYPE_ID))
        self.assertEqual(str(requests[0].url), f"{API_BASE}context/companysettings/pricetype/{PRICE_TYPE_ID}")
