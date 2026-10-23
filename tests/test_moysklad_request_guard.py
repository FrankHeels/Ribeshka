"""Task ownership at the final HTTP boundary; provider traffic stays synthetic.

Run: .venv/bin/python manage.py test tests.test_moysklad_request_guard --settings=fishsite.settings_test --noinput
"""

from datetime import timedelta
import uuid
from unittest.mock import patch

import httpx
from django.test import TestCase
from django.utils import timezone

from integrations.models import MoySkladApiState, MoySkladTask
from integrations.moysklad.client import MoySkladClient
from integrations.moysklad.contracts import LeaseLost
from integrations.moysklad.request_guard import task_request_scope
from integrations.moysklad.throttling import ApiThrottle
from tests.moysklad_helpers import ACCOUNT_ID, ENVIRONMENT, moysklad_config


class MoySkladRequestGuardTests(TestCase):
    def setUp(self):
        self.requests = []

        def respond(request):
            self.requests.append(request)
            return httpx.Response(200, json={"ok": True}, request=request)

        self.api = MoySkladClient(
            config=moysklad_config(), readonly_probe=True, transport=httpx.MockTransport(respond),
        )
        self.addCleanup(self.api.close)

    def task(self):
        return MoySkladTask.objects.create(
            account_id=ACCOUNT_ID, environment=ENVIRONMENT, kind="check_access",
            dedupe_key=str(uuid.uuid4()), status="processing", lease_token=uuid.uuid4(),
            lease_expires_at=timezone.now() + timedelta(minutes=5),
        )

    def expire(self, task):
        MoySkladTask.objects.filter(pk=task.pk).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1),
        )

    # Task теряет lease уже после получения API slot. Отмена должна освободить
    # настоящий account lease и не допустить ни одного запроса к провайдеру.
    def test_lease_lost_after_api_acquire_releases_account_slot_without_http(self):
        task = self.task()
        self.api._throttle = ApiThrottle(moysklad_config())
        acquire = self.api._throttle.acquire
        tokens = []

        def expire_after_acquire(*, access_probe=False):
            token = acquire(access_probe=access_probe)
            state = MoySkladApiState.objects.get(account_id=ACCOUNT_ID)
            self.assertEqual(state.api_lease_token, token)
            tokens.append(token)
            self.expire(task)
            return token

        with patch.object(self.api._throttle, "acquire", side_effect=expire_after_acquire):
            with self.assertRaises(LeaseLost), task_request_scope(task):
                self.api.get("entity/product")

        self.assertEqual(len(tokens), 1)
        self.assertEqual(self.requests, [])
        state = MoySkladApiState.objects.get(account_id=ACCOUNT_ID)
        self.assertIsNone(state.api_lease_token)
        self.assertIsNone(state.api_lease_expires_at)

    # Завершённый scope не привязывает последующую диагностику к старой task,
    # даже если её lease истёк после успешного запроса.
    def test_normal_scope_exit_allows_later_unscoped_diagnostic_request(self):
        task = self.task()
        with task_request_scope(task):
            self.assertEqual(self.api.get("entity/product"), {"ok": True})

        self.expire(task)
        self.assertEqual(self.api.get("entity/product"), {"ok": True})
        self.assertEqual(len(self.requests), 2)

    # Исключение в обработчике также снимает scope: истёкшая задача не должна
    # блокировать следующий диагностический запрос того же клиента.
    def test_exception_scope_exit_allows_later_unscoped_diagnostic_request(self):
        task = self.task()
        with self.assertRaisesRegex(RuntimeError, "worker failed"):
            with task_request_scope(task):
                self.expire(task)
                raise RuntimeError("worker failed")

        self.assertEqual(self.api.get("entity/product"), {"ok": True})
        self.assertEqual(len(self.requests), 1)

    # Вложенная живая task разрешает свой HTTP, затем восстанавливает истёкшую
    # внешнюю task. После выхода из обоих scope диагностика снова свободна.
    def test_nested_scope_restores_outer_task_then_clears_it_on_exit(self):
        outer, inner = self.task(), self.task()
        self.expire(outer)
        with task_request_scope(outer):
            with task_request_scope(inner):
                self.assertEqual(self.api.get("entity/product"), {"ok": True})
            with self.assertRaises(LeaseLost):
                self.api.get("entity/product")

        self.assertEqual(self.api.get("entity/product"), {"ok": True})
        self.assertEqual(len(self.requests), 2)
