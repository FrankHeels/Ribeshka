"""Контракт безопасного HTTP-транспорта МойСклад.

Запуск без создания тестовой БД:
    .venv/bin/python manage.py test tests.test_moysklad_transport --settings=fishsite.settings_test --noinput

Все HTTP-ответы задаются HTTPX MockTransport; рабочие учётные данные и сеть
в этих сценариях не используются.
"""

import asyncio
import json
from decimal import Decimal
from datetime import datetime, timedelta, timezone as datetime_timezone
from unittest.mock import patch

import httpx
from django.db import connection
from django.test import SimpleTestCase, TransactionTestCase

from tests.moysklad_helpers import API_BASE, moysklad_config


class MoySkladTransportTests(SimpleTestCase):
    # Один фиксированный API host, Bearer, gzip и ожидаемые ограничения
    # транспорта применяются ко всем методам клиента.
    def test_get_uses_fixed_api_host_bearer_and_gzip(self):
        from integrations.moysklad.client import MoySkladClient

        seen = []

        def respond(request):
            seen.append(request)
            return httpx.Response(200, json={"ok": True}, request=request)

        client = MoySkladClient(config=moysklad_config(), transport=httpx.MockTransport(respond))
        try:
            self.assertEqual(client.get("entity/product"), {"ok": True})
        finally:
            client.close()

        self.assertEqual(len(seen), 1)
        self.assertEqual(str(seen[0].url), f"{API_BASE}entity/product")
        self.assertEqual(seen[0].headers["Authorization"], "Bearer synthetic-test-token")
        self.assertIn("gzip", seen[0].headers["Accept-Encoding"].lower())
        timeout = seen[0].extensions["timeout"]
        self.assertEqual(timeout, {"connect": 5.0, "read": 20.0, "write": 20.0, "pool": 5.0})

    # JSON-числа не проходят через float: денежное значение остаётся точным
    # как в теле запроса, так и после разбора ответа.
    def test_post_preserves_decimal_json_without_float_rounding(self):
        from integrations.moysklad.client import MoySkladClient

        seen = []

        def respond(request):
            seen.append(request)
            return httpx.Response(200, content=b'{"price":0.10}', request=request)

        client = MoySkladClient(config=moysklad_config(), transport=httpx.MockTransport(respond))
        try:
            result = client.post("entity/customerorder", json={"price": Decimal("0.10")})
        finally:
            client.close()

        self.assertEqual(json.loads(seen[0].content, parse_float=Decimal)["price"], Decimal("0.10"))
        self.assertEqual(result["price"], Decimal("0.10"))

    # Ссылки из API не могут заменить host, выйти из пространства API или
    # добавить credentials и произвольный query к запросу клиента.
    def test_rejects_off_host_and_unsafe_resource_paths_before_transport(self):
        from integrations.moysklad.client import MoySkladClient
        from integrations.moysklad.contracts import MoySkladError

        calls = []
        client = MoySkladClient(
            config=moysklad_config(),
            transport=httpx.MockTransport(lambda request: calls.append(request)),
        )
        try:
            unsafe_paths = (
                "https://attacker.example/steal",
                "https://user:password@api.moysklad.ru/api/remap/1.2/entity/product",
                "//attacker.example/api/remap/1.2/entity/product",
                "../admin",
                "entity/product/../../admin",
                "entity/product?expand=anything",
                "https://api.moysklad.ru/api/remap/1.2/report/profit",
            )
            for path in unsafe_paths:
                with self.subTest(path=path), self.assertRaises(MoySkladError):
                    client.get(path)
            self.assertEqual(calls, [])
        finally:
            client.close()

    # Запрет редиректов важен для Authorization: сервер не должен перенаправить
    # токен на сторонний адрес.
    def test_redirect_response_is_not_followed(self):
        from integrations.moysklad.client import MoySkladClient
        from integrations.moysklad.contracts import MoySkladError

        calls = []

        def respond(request):
            calls.append(request)
            return httpx.Response(302, headers={"Location": "https://attacker.example/"}, request=request)

        client = MoySkladClient(config=moysklad_config(), transport=httpx.MockTransport(respond))
        try:
            with self.assertRaises(MoySkladError):
                client.get("entity/product")
        finally:
            client.close()
        self.assertEqual(len(calls), 1)

    # API-ошибка хранит безопасный код и класс повтора; секреты из заголовков,
    # payload и ответа не появляются в тексте или repr исключения.
    def test_http_errors_are_classified_without_exposing_secrets(self):
        from integrations.moysklad.client import MoySkladClient
        from integrations.moysklad.contracts import MoySkladError

        secrets = ("synthetic-test-token", "private-phone-555", "response-secret")
        cases = ((401, False), (403, False), (503, True))
        for status, retryable in cases:
            with self.subTest(status=status):
                def respond(request):
                    return httpx.Response(
                        status,
                        content=b'{"message":"response-secret","phone":"private-phone-555"}',
                        request=request,
                    )

                client = MoySkladClient(config=moysklad_config(), transport=httpx.MockTransport(respond))
                try:
                    with self.assertRaises(MoySkladError) as caught:
                        client.post("entity/customerorder", json={"phone": "private-phone-555"})
                finally:
                    client.close()

                error = caught.exception
                self.assertEqual(error.status_code, status)
                self.assertEqual(error.retryable, retryable)
                self.assertTrue(error.code)
                rendered = f"{error!s} {error!r}"
                for secret in secrets:
                    self.assertNotIn(secret, rendered)

    # Retry-After у МойСклад задан в миллисекундах; клиент переводит его в
    # секунды, не включая тело ответа в диагностическое исключение.
    def test_rate_limit_uses_lognex_retry_after_milliseconds(self):
        from integrations.moysklad.client import MoySkladClient
        from integrations.moysklad.contracts import MoySkladError

        def respond(request):
            return httpx.Response(429, headers={"X-Lognex-Retry-After": "2500"},
                                  content=b'{"message":"sensitive-api-detail"}', request=request)

        client = MoySkladClient(config=moysklad_config(), transport=httpx.MockTransport(respond))
        try:
            with self.assertRaises(MoySkladError) as caught:
                client.get("entity/product")
        finally:
            client.close()
        self.assertTrue(caught.exception.retryable)
        self.assertNotIn("private-timeout-detail", str(caught.exception))

        class CancelTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                raise asyncio.CancelledError

        client = MoySkladClient(config=moysklad_config(), transport=CancelTransport())
        try:
            with self.assertRaises(asyncio.CancelledError):
                client.get("entity/product")
        finally:
            client.close()
        self.assertTrue(caught.exception.retryable)
        self.assertTrue(caught.exception.code)
        self.assertEqual(caught.exception.retry_after, 2.5)
        self.assertNotIn("sensitive-api-detail", str(caught.exception))

    # Неожиданный JSON не должен превращать необработанное тело API в текст
    # ошибки или логируемые данные.
    def test_invalid_json_error_does_not_include_response_body(self):
        from integrations.moysklad.client import MoySkladClient
        from integrations.moysklad.contracts import MoySkladError

        def respond(request):
            return httpx.Response(200, content=b"private-response-fragment", request=request)

        client = MoySkladClient(config=moysklad_config(), transport=httpx.MockTransport(respond))
        try:
            with self.assertRaises(MoySkladError) as caught:
                client.get("entity/product")
        finally:
            client.close()
        self.assertNotIn("private-response-fragment", str(caught.exception))

    # HTTPX timeout остаётся временной ошибкой; синхронная обёртка не скрывает
    # отмену asyncio, необходимую общему deadline исполнителя.
    def test_transport_timeout_is_retryable_and_cancellation_propagates(self):
        from integrations.moysklad.client import MoySkladClient
        from integrations.moysklad.contracts import MoySkladError

        def timeout(request):
            raise httpx.ReadTimeout("private-timeout-detail", request=request)

        client = MoySkladClient(config=moysklad_config(), transport=httpx.MockTransport(timeout))
        try:
            with self.assertRaises(MoySkladError) as caught:
                client.get("entity/product")
        finally:
            client.close()

    # Общий deadline покрывает чтение тела, даже если поток периодически
    # передаёт данные и не превышает отдельный HTTPX read timeout.
    def test_total_deadline_covers_slow_response_body(self):
        from integrations.moysklad.client import MoySkladClient
        from integrations.moysklad.contracts import MoySkladError

        class SlowBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'{"part":'
                await asyncio.sleep(0.03)
                yield b'"done"}'

        def respond(request):
            return httpx.Response(200, stream=SlowBody(), request=request)

        client = MoySkladClient(config=moysklad_config(), transport=httpx.MockTransport(respond))
        try:
            with patch("integrations.moysklad.client.TOTAL_TIMEOUT_SECONDS", 0.005):
                with self.assertRaises(MoySkladError) as caught:
                    client.get("entity/product")
        finally:
            client.close()
        self.assertTrue(caught.exception.retryable)

    # Один клиент переиспользует AsyncClient и корректно закрывается вместе с
    # постоянным Runner при явном close или контекстном менеджере.
    def test_context_manager_reuses_transport_and_closes_cleanly(self):
        from integrations.moysklad.client import MoySkladClient

        class TrackingTransport(httpx.MockTransport):
            def __init__(self, handler):
                super().__init__(handler)
                self.close_count = 0

            async def aclose(self):
                self.close_count += 1
                await super().aclose()

        seen = []
        transport = TrackingTransport(
            lambda request: (seen.append(request), httpx.Response(200, json={}, request=request))[1]
        )
        with MoySkladClient(config=moysklad_config(), transport=transport) as client:
            client.get("entity/product")
            client.get("entity/product/00000000-0000-4000-8000-000000000001")
        self.assertEqual(len(seen), 2)
        self.assertEqual(transport.close_count, 1)


class FakeClock:
    """Часы, в которых ожидание сдвигает время без реальной задержки."""

    def __init__(self):
        self.value = datetime(2026, 10, 15, tzinfo=datetime_timezone.utc)
        self.sleeps = []

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += timedelta(seconds=seconds)


class MoySkladThrottleTests(TransactionTestCase):
    reset_sequences = True

    def throttle(self, clock):
        from integrations.moysklad.throttling import ApiThrottle

        return ApiThrottle(config=moysklad_config(), now=clock.now, sleep=clock.sleep)

    # Два объекта throttler с одинаковым аккаунтом видят общий темп 2 запроса
    # в секунду; любой ожидающий вызов спит после короткой DB-транзакции.
    def test_account_rate_limit_is_shared_and_wait_happens_outside_transaction(self):
        clock = FakeClock()
        first = self.throttle(clock)
        second = self.throttle(clock)
        sleep_was_outside_atomic = []

        def sleep(seconds):
            sleep_was_outside_atomic.append(not connection.in_atomic_block)
            clock.sleep(seconds)

        first.sleep = sleep
        second.sleep = sleep
        token1 = first.acquire()
        first.release(token1)
        token2 = second.acquire()
        second.release(token2)
        token3 = first.acquire()
        first.release(token3)

        self.assertEqual(len(clock.sleeps), 2)
        self.assertAlmostEqual(sum(clock.sleeps), 1.0, places=3)
        self.assertTrue(sleep_was_outside_atomic)
        self.assertTrue(all(sleep_was_outside_atomic))

    # Просроченный исполнитель не может освободить lease, уже захваченный
    # новым владельцем того же аккаунта.
    def test_stale_release_cannot_unlock_new_concurrent_request(self):
        from integrations.moysklad.contracts import MoySkladError

        clock = FakeClock()
        old = self.throttle(clock)
        new = self.throttle(clock)
        old_token = old.acquire()
        # API-вызов может занимать 60 секунд; срок захвата должен истечь после
        # максимального deadline, прежде чем его сможет получить новый процесс.
        clock.value += timedelta(seconds=71)
        new_token = new.acquire()
        old.release(old_token)

        with self.assertRaises(MoySkladError) as caught:
            old.acquire()
        self.assertTrue(caught.exception.retryable)
        self.assertNotEqual(old_token, new_token)

        new.release(new_token)

    # 429 ставит общую паузу аккаунта и возвращает вычислимый retry_after;
    # приостановка сохраняется между объектами throttler.
    def test_defer_pauses_all_throttlers_for_account(self):
        from integrations.moysklad.contracts import MoySkladError

        clock = FakeClock()
        first = self.throttle(clock)
        second = self.throttle(clock)
        first.defer(2.5)

        with self.assertRaises(MoySkladError) as caught:
            second.acquire()
        self.assertTrue(caught.exception.retryable)
        self.assertAlmostEqual(caught.exception.retry_after, 2.5, places=3)

        clock.value += timedelta(seconds=2.5)
        token = second.acquire()
        second.release(token)
