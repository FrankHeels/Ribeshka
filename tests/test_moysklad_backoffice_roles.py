"""Owner business summary and superuser-only MoySklad operations.

Run only against the repository's isolated PostgreSQL test profile.
"""

import hashlib
import re
import uuid
from datetime import UTC, datetime, timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import NoReverseMatch, reverse
from django.utils.html import strip_tags

from content.consent_revisions import get_current_consent_revision
from content.models import ConsentRevision
from fishsite.backoffice.management.commands.sync_backoffice_roles import OWNER_GROUP
from integrations.models import (
    MoySkladApiState, MoySkladIncident, MoySkladOrderLink, MoySkladProduct,
    MoySkladSyncRun, MoySkladSyncState, MoySkladTask, MoySkladWebhookInbox,
)
from integrations.moysklad import controls
from tests.backoffice_helpers import login_verified_admin
from tests.moysklad_helpers import ACCOUNT_ID, ENVIRONMENT, NAMESPACE, moysklad_config


NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)
ATTENTION = (
    r"внимани|нужна проверка|требует проверки|не (работ|выполня|запущ|настро|готов|одобрен|заверш)"
    r"|выключ|отключ|приостанов|недоступ|ошиб|просроч|устар|требует разбора|на разборе"
)
RAW_MODELS = (
    MoySkladTask, MoySkladSyncRun, MoySkladSyncState, MoySkladApiState,
    MoySkladWebhookInbox, MoySkladIncident,
)


@override_settings(MOYSKLAD=moysklad_config())
class MoySkladBackofficeRolesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model()
        cls.owner = users.objects.create_user(username="business-owner", is_active=True)
        call_command("sync_backoffice_roles", cls.owner.username, stdout=StringIO())
        cls.owner.refresh_from_db()
        cls.technical = users.objects.create_superuser(
            username="technical", email="technical@example.test", password="synthetic",
        )
        cls.viewer = users.objects.create_user(username="view-only", is_staff=True)
        cls.viewer.user_permissions.add(Permission.objects.get(
            content_type__app_label="integrations", codename="view_moyskladsyncstate",
        ))

    def setUp(self):
        login_verified_admin(self.client, self.owner)
        self.clock = patch("django.utils.timezone.now", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.http = patch("httpx.Client.send", side_effect=AssertionError("Admin must not call provider HTTP"))
        self.http.start()
        self.addCleanup(self.http.stop)

    def technical_url(self):
        try:
            url = reverse("admin:moysklad_technical")
        except NoReverseMatch:
            self.fail("The superuser technical dashboard must have name admin:moysklad_technical")
        self.assertEqual(url, "/admin/moysklad/technical/")
        return url

    def owner_page(self):
        response = self.client.get(reverse("admin:moysklad_status"))
        self.assertEqual(response.status_code, 200)
        return response

    def text(self, response):
        html = response.content.decode()
        marker = html.find("moysklad-dashboard")
        return " ".join(strip_tags(html[marker:] if marker >= 0 else html).split()).lower()

    def service_text(self, response, label):
        html = response.content.decode()
        blocks = re.findall(r"<(?:section|article)\b[^>]*>(.*?)</(?:section|article)>", html, re.S)
        matches = []
        for block in blocks:
            headings = re.findall(r"<h[23]\b[^>]*>(.*?)</h[23]>", block, re.S)
            if any(re.search(label, strip_tags(heading), re.I) for heading in headings):
                matches.append(" ".join(strip_tags(block).split()).lower())
        self.assertTrue(matches, f"Owner page needs a readable service section matching {label!r}")
        return " ".join(matches)

    def assert_attention(self, response, label=None):
        text = self.service_text(response, label) if label else self.text(response)
        self.assertRegex(text, ATTENTION)
        self.assertNotRegex(text, r"вс[её] работает|вс[её] в порядке|обмен работает исправно")

    def healthy_state(self, **overrides):
        consent = get_current_consent_revision()
        data = {
            "account_id": ACCOUNT_ID, "environment": ENVIRONMENT, "namespace": NAMESPACE,
            "heartbeat_at": NOW - timedelta(seconds=30),
            "export_started_at": NOW - timedelta(days=2), "export_consent_revision": consent,
            "prices_reconciled_at": NOW - timedelta(hours=1),
            "prices_next_at": NOW + timedelta(hours=23),
            "catalog_enabled": True, "prices_enabled": True,
            "export_enabled": True, "statuses_enabled": True,
        }
        return MoySkladSyncState.objects.create(**(data | overrides))

    def task(self, kind, status="pending", **kwargs):
        data = {
            "account_id": ACCOUNT_ID, "environment": ENVIRONMENT,
            "kind": kind, "status": status, "dedupe_key": uuid.uuid4().hex,
        }
        if status == "processing":
            data.update(lease_token=uuid.uuid4(), lease_expires_at=NOW + timedelta(minutes=1))
        return MoySkladTask.objects.create(**(data | kwargs))

    def snapshot(self):
        models = (*RAW_MODELS, MoySkladProduct, MoySkladOrderLink, ConsentRevision, LogEntry)
        return {model._meta.label: list(model.objects.order_by("pk").values()) for model in models}

    def grant_legacy_technical_permissions(self):
        names = ["manage_moysklad", *(f"view_{model._meta.model_name}" for model in RAW_MODELS)]
        self.owner.user_permissions.add(*Permission.objects.filter(
            content_type__app_label="integrations", codename__in=names,
        ))
        self.owner = get_user_model().objects.get(pk=self.owner.pk)

    def test_synced_owner_role_has_business_permissions_without_technical_grants(self):
        self.assertTrue(self.owner.is_staff)
        self.assertFalse(self.owner.is_superuser)
        self.assertTrue(self.owner.groups.filter(name=OWNER_GROUP).exists())
        grants = set(Group.objects.get(name=OWNER_GROUP).permissions.values_list("codename", flat=True))
        self.assertTrue({"view_moyskladsyncstate", "sync_moysklad_catalog", "reconcile_moysklad_prices",
                         "view_moyskladproduct", "view_moyskladorderlink", "repair_moysklad_order"} <= grants)
        self.assertNotIn("manage_moysklad", grants)
        self.assertFalse({f"view_{model._meta.model_name}" for model in RAW_MODELS
                          if model is not MoySkladSyncState} & grants)

    def test_owner_page_has_business_actions_links_and_no_raw_diagnostics(self):
        state = self.healthy_state(webhook_id=uuid.uuid4())
        MoySkladIncident.objects.create(account_id=ACCOUNT_ID, environment=ENVIRONMENT,
                                       scope="catalog", code="temporary_api_error")
        response = self.owner_page()
        self.assertEqual(reverse("admin:moysklad_status"), "/admin/moysklad/")
        for action in ("catalog", "prices"):
            self.assertContains(response, reverse("admin:moysklad_action", args=(action,)))
        for name in ("admin:moysklad_candidates", "admin:catalog_product_changelist",
                     "admin:integrations_moyskladorderlink_changelist"):
            self.assertContains(response, reverse(name))
        for action in ("check", "stream", "start_export"):
            self.assertNotContains(response, reverse("admin:moysklad_action", args=(action,)))
        for raw in (str(ACCOUNT_ID), str(state.webhook_id), state.export_consent_revision.revision_id,
                    "heartbeat", "UUID", "Пауза API", "Окружение:", "temporary_api_error"):
            self.assertNotContains(response, raw)
        self.assertNotContains(response, "/admin/moysklad/technical/")
        self.assertRegex(self.text(response), r"несвязан|без связи|не связан")
        self.assertNotRegex(self.text(response), r"готов[ыо]? к публикации")

    def test_admin_home_owner_note_links_to_business_panel_without_technical_identifiers(self):
        state = self.healthy_state(webhook_id=uuid.uuid4())
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("admin:moysklad_status"))
        note = self.service_text(response, r"^мойсклад$")
        self.assertRegex(note, r"подключ|обмен|состояни|каталог|цен|заявк")
        for raw in ("heartbeat", "uuid", str(ACCOUNT_ID), str(NAMESPACE),
                    str(state.webhook_id), state.export_consent_revision.revision_id):
            self.assertNotIn(raw.lower(), note)

    def test_superuser_simple_page_links_to_separate_technical_dashboard(self):
        self.healthy_state()
        login_verified_admin(self.client, self.technical)
        response = self.owner_page()
        self.assertContains(response, self.technical_url())
        self.assertNotContains(response, "Последний heartbeat")
        self.assertEqual(self.client.get(self.technical_url()).status_code, 200)

    def test_owner_cannot_open_technical_dashboard_even_with_legacy_grants(self):
        self.healthy_state()
        self.grant_legacy_technical_permissions()
        before = self.snapshot()
        self.assertEqual(self.client.get(self.technical_url()).status_code, 403)
        self.assertEqual(self.snapshot(), before)

    def test_owner_technical_posts_with_legacy_grants_have_no_side_effects(self):
        self.healthy_state()
        self.grant_legacy_technical_permissions()
        for action, data in (("check", {}), ("stream", {"stream": "prices", "enabled": "no"}),
                             ("start_export", {"confirm": "yes"})):
            with self.subTest(action=action):
                before = self.snapshot()
                response = self.client.post(reverse("admin:moysklad_action", args=(action,)), data)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(self.snapshot(), before)

    def test_control_services_reject_owner_with_legacy_manage_grant(self):
        self.healthy_state()
        self.grant_legacy_technical_permissions()
        operations = (
            lambda: controls.request_access_check(actor=self.owner),
            lambda: controls.set_stream_enabled("prices", False, actor=self.owner),
            lambda: controls.start_export(actor=self.owner),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                before = self.snapshot()
                with self.assertRaises(PermissionDenied):
                    operation()
                self.assertEqual(self.snapshot(), before)

    def test_superuser_actions_validate_enqueue_audit_and_return_to_technical_page(self):
        state = self.healthy_state()
        login_verified_admin(self.client, self.technical)
        technical_url = self.technical_url()
        for action, data in (("check", {}), ("stream", {"stream": "statuses", "enabled": "no"}),
                             ("start_export", {"confirm": "yes"})):
            with self.subTest(action=action):
                previous_audits = LogEntry.objects.filter(user=self.technical).count()
                response = self.client.post(reverse("admin:moysklad_action", args=(action,)), data)
                self.assertRedirects(response, technical_url, fetch_redirect_response=False)
                self.assertEqual(LogEntry.objects.filter(user=self.technical).count(), previous_audits + 1)
        state.refresh_from_db()
        self.assertFalse(state.statuses_enabled)
        self.assertEqual(state.export_started_at, NOW - timedelta(days=2))
        self.assertEqual(state.export_consent_revision, get_current_consent_revision())
        self.assertEqual(MoySkladTask.objects.get(kind="check_access").status, "pending")
        self.assertFalse(MoySkladTask.objects.filter(kind="export_order").exists())
        for action, data in (("stream", {"stream": "prices", "enabled": "invalid"}),
                             ("start_export", {})):
            with self.subTest(invalid_action=action):
                before = self.snapshot()
                response = self.client.post(reverse("admin:moysklad_action", args=(action,)), data)
                self.assertRedirects(response, technical_url, fetch_redirect_response=False)
                self.assertEqual(self.snapshot(), before)

    def test_owner_catalog_and_prices_actions_remain_background_business_actions(self):
        self.healthy_state()
        for action, kind in (("catalog", "catalog_sync"), ("prices", "price_reconciliation")):
            with self.subTest(action=action):
                response = self.client.post(reverse("admin:moysklad_action", args=(action,)))
                self.assertRedirects(response, reverse("admin:moysklad_status"), fetch_redirect_response=False)
                task = MoySkladTask.objects.get(kind=kind)
                self.assertEqual(task.status, "pending")
                self.assertEqual(task.sync_run.actor_id, self.owner.pk)
        login_verified_admin(self.client, self.viewer)
        self.assertEqual(self.owner_page().status_code, 200)
        for action in ("catalog", "prices"):
            before = self.snapshot()
            self.assertEqual(self.client.post(reverse("admin:moysklad_action", args=(action,))).status_code, 403)
            self.assertEqual(self.snapshot(), before)

    def test_raw_diagnostics_require_superuser_despite_legacy_view_grants(self):
        self.healthy_state()
        self.grant_legacy_technical_permissions()
        for model in RAW_MODELS:
            with self.subTest(model=model._meta.model_name):
                url = reverse(f"admin:integrations_{model._meta.model_name}_changelist")
                self.assertEqual(self.client.get(url).status_code, 403)
        for model in (MoySkladProduct, MoySkladOrderLink):
            url = reverse(f"admin:integrations_{model._meta.model_name}_changelist")
            self.assertEqual(self.client.get(url).status_code, 200)
        login_verified_admin(self.client, self.technical)
        for model in RAW_MODELS:
            url = reverse(f"admin:integrations_{model._meta.model_name}_changelist")
            self.assertEqual(self.client.get(url).status_code, 200)

    def test_gets_are_read_only_without_archiving_consent_or_provider_http(self):
        before = self.snapshot()
        with patch("content.consent_revisions.get_current_consent_revision",
                   side_effect=AssertionError("GET must not archive current consent")) as archive:
            self.owner_page()
            login_verified_admin(self.client, self.technical)
            self.assertEqual(self.client.get(self.technical_url()).status_code, 200)
        archive.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_pages_and_actions_keep_otp_csrf_method_and_cache_guards(self):
        self.healthy_state()
        login_verified_admin(self.client, self.technical)
        urls = (reverse("admin:moysklad_status"), self.technical_url())
        for url in urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertIn("no-store", response["Cache-Control"])
            self.assertIn("noindex", response["X-Robots-Tag"])
        for action in ("check", "stream", "start_export", "catalog", "prices"):
            self.assertEqual(self.client.get(reverse("admin:moysklad_action", args=(action,))).status_code, 405)
        csrf_client = Client(enforce_csrf_checks=True)
        login_verified_admin(csrf_client, self.technical)
        before = self.snapshot()
        self.assertEqual(csrf_client.post(reverse("admin:moysklad_action", args=("check",))).status_code, 403)
        self.assertEqual(self.snapshot(), before)
        unverified = Client()
        unverified.force_login(self.technical)
        for url in (*urls, reverse("admin:moysklad_action", args=("check",))):
            response = unverified.post(url) if "/action/" in url else unverified.get(url)
            self.assertIn(response.status_code, (302, 403))
        self.assertEqual(self.snapshot(), before)
        self.technical.is_staff = False
        self.technical.save(update_fields=("is_staff",))
        self.assertIn(self.client.get(self.technical_url()).status_code, (302, 403))

    def test_missing_configuration_is_helpful_to_owner_but_codes_are_technical_only(self):
        with override_settings(MOYSKLAD=moysklad_config() | {"account_id": "", "namespace": ""}):
            response = self.owner_page()
            self.assertRegex(self.text(response), r"не настроен|настройк|администратор")
            self.assertNotContains(response, "not_configured")
            self.assertNotContains(response, "invalid_configuration")
            login_verified_admin(self.client, self.technical)
            technical = self.client.get(self.technical_url())
            self.assertEqual(technical.status_code, 200)
            self.assertRegex(self.text(technical), r"not_configured|invalid_configuration")

    def test_missing_or_stale_worker_state_does_not_report_healthy(self):
        response = self.owner_page()
        self.assert_attention(response)
        unavailable = r"не (?:запущ|работ|выполня|настро|получ)|недоступ|давно|задерж|устар|нет связи"
        self.assertRegex(self.text(response), unavailable)
        state = self.healthy_state()
        for heartbeat in (None, NOW - timedelta(minutes=2, seconds=1)):
            with self.subTest(heartbeat=heartbeat):
                state.heartbeat_at = heartbeat
                state.save(update_fields=("heartbeat_at",))
                response = self.owner_page()
                self.assert_attention(response)
                self.assertRegex(self.text(response), unavailable)

    def test_fresh_worker_does_not_hide_current_access_failure_or_api_pause(self):
        self.healthy_state()
        api = MoySkladApiState.objects.create(account_id=ACCOUNT_ID, access_error_code="access_denied")
        response = self.owner_page()
        self.assert_attention(response)
        self.assertRegex(self.text(response), r"доступ.{0,150}(?:ошиб|недоступ|приостанов|провер)|нет доступа|не удалось подключ")
        api.access_error_code = ""
        api.api_paused_until = NOW + timedelta(minutes=5)
        api.save()
        response = self.owner_page()
        self.assert_attention(response)
        self.assertRegex(self.text(response), r"приостанов|временно недоступ|пауза обмена|ожидает возобновления")

    def test_disabled_stream_reports_its_own_service_as_off(self):
        state = self.healthy_state()
        for stream, label in (("catalog", r"каталог"), ("prices", r"цен"),
                              ("export", r"передач.*заяв|заявк"), ("statuses", r"статус")):
            with self.subTest(stream=stream):
                setattr(state, f"{stream}_enabled", False)
                state.save(update_fields=(f"{stream}_enabled",))
                response = self.owner_page()
                self.assert_attention(response, label)
                self.assertRegex(self.service_text(response, label), r"выключ|отключ|не запущ|останов")
                setattr(state, f"{stream}_enabled", True)
                state.save(update_fields=(f"{stream}_enabled",))

    def test_exports_need_start_and_current_approved_consent_and_no_account_pause(self):
        state = self.healthy_state()
        original_consent = state.export_consent_revision
        old_text = "Синтетическая старая редакция согласия"
        old_hash = hashlib.sha256(old_text.encode()).hexdigest()
        old_consent = ConsentRevision.objects.create(text=old_text, sha256=old_hash, revision_id=f"sha256:{old_hash}")
        for changes in ({"export_started_at": None}, {"export_consent_revision": None},
                        {"export_consent_revision": old_consent}):
            with self.subTest(changes=changes):
                MoySkladSyncState.objects.filter(pk=state.pk).update(**changes)
                self.assert_attention(self.owner_page(), r"передач.*заяв|заявк")
                MoySkladSyncState.objects.filter(pk=state.pk).update(
                    export_started_at=state.export_started_at, export_consent_revision=original_consent,
                )
        MoySkladApiState.objects.create(account_id=ACCOUNT_ID, export_paused=True)
        self.assert_attention(self.owner_page(), r"передач.*заяв|заявк")

    def test_price_service_reports_missing_overdue_or_unsuccessful_full_check(self):
        state = self.healthy_state()
        for checked_at in (None, NOW - timedelta(hours=26, seconds=1)):
            with self.subTest(checked_at=checked_at):
                MoySkladSyncState.objects.filter(pk=state.pk).update(prices_reconciled_at=checked_at)
                self.assert_attention(self.owner_page(), r"цен")
        MoySkladSyncState.objects.filter(pk=state.pk).update(prices_reconciled_at=NOW - timedelta(hours=1))
        for status in ("failed", "partial"):
            with self.subTest(status=status):
                MoySkladSyncRun.objects.create(account_id=ACCOUNT_ID, environment=ENVIRONMENT,
                                              kind="prices", status=status, started_at=NOW - timedelta(minutes=5),
                                              completed_at=NOW - timedelta(minutes=4))
                self.assert_attention(self.owner_page(), r"цен")

    def test_open_incident_or_manual_review_cannot_be_hidden_by_fresh_heartbeat(self):
        self.healthy_state()
        incident = MoySkladIncident.objects.create(account_id=ACCOUNT_ID, environment=ENVIRONMENT,
                                                  scope="catalog", code="missing_price")
        response = self.owner_page()
        self.assert_attention(response)
        self.assertRegex(self.text(response), r"требует внимания|нужна помощь|есть ошибки|ошибки обмена|проблем")
        incident.closed_at = NOW
        incident.save()
        self.task("export_order", "manual_review")
        self.assert_attention(self.owner_page(), r"передач.*заяв|заявк")

    def test_price_manual_review_warns_prices_even_when_incident_scope_is_catalog(self):
        self.healthy_state()
        self.task("price", "manual_review", last_error_code="missing_price")
        MoySkladIncident.objects.create(account_id=ACCOUNT_ID, environment=ENVIRONMENT,
                                       scope="catalog", code="missing_price")
        response = self.owner_page()
        self.assert_attention(response, r"цен")
        self.assertNotIn("проблем не обнаружено", self.service_text(response, r"цен"))

    def test_status_task_manual_review_warns_statuses_without_status_run(self):
        self.healthy_state()
        self.task("status_sync", "manual_review", last_error_code="external_order_not_found")
        MoySkladIncident.objects.create(account_id=ACCOUNT_ID, environment=ENVIRONMENT,
                                       scope="order_export", code="external_order_not_found")
        self.assertFalse(MoySkladSyncRun.objects.filter(kind="status").exists())
        response = self.owner_page()
        self.assert_attention(response, r"статус")
        self.assertNotIn("проблем не обнаружено", self.service_text(response, r"статус"))

    def test_business_counts_separate_queue_manual_review_and_pending_products(self):
        self.healthy_state()
        self.task("export_order", "pending")
        self.task("export_order", "processing")
        self.task("export_order", "manual_review")
        for _ in range(4):
            self.task("price")
        for index in range(3):
            MoySkladProduct.objects.create(account_id=ACCOUNT_ID, external_id=uuid.uuid4(),
                                           name=f"Несвязанный {index}", is_deferred=index == 0,
                                           price_pending_since=NOW - timedelta(minutes=5))
        response = self.owner_page()
        export = self.service_text(response, r"передач.*заяв|заявк")
        prices = self.service_text(response, r"цен")
        self.assertRegex(export, r"(?:в очереди|ожидают|ожидает|передаются|обработк)[^\d]{0,60}2\b")
        self.assertRegex(export, r"(?:разбор|помощ|внимани)[^\d]{0,60}1\b")
        self.assertRegex(prices, r"товар[^\d]{0,90}3\b|3\s*товар")
        self.assertNotRegex(export, r"очереди\s*/\s*на разборе")
        self.assertEqual(response.context["candidate_count"], 3)

    def test_page_counts_and_failures_are_isolated_by_account_and_environment(self):
        self.healthy_state()
        other_account = uuid.uuid4()
        for target in ({"account_id": other_account, "environment": ENVIRONMENT},
                       {"account_id": ACCOUNT_ID, "environment": "other"}):
            self.task("export_order", "manual_review", **target)
            MoySkladIncident.objects.create(**target, scope="catalog", code="temporary_api_error")
            MoySkladSyncRun.objects.create(**target, kind="prices", status="failed")
        MoySkladProduct.objects.create(account_id=other_account, external_id=uuid.uuid4(),
                                       price_pending_since=NOW)
        response = self.owner_page()
        self.assertEqual(response.context["candidate_count"], 0)
        self.assertEqual(response.context["incident_count"], 0)
        self.assertEqual(response.context["pending_price_count"], 0)
        self.assertEqual(response.context["export_count"], 0)
        self.assertNotContains(response, "temporary_api_error")

    def test_account_api_state_overrides_legacy_state_and_missing_api_state_uses_fallback(self):
        state = self.healthy_state(access_error_code="access_denied", export_paused=True,
                                   api_paused_until=NOW + timedelta(minutes=5))
        login_verified_admin(self.client, self.technical)
        response = self.client.get(self.technical_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["api"]["access_error_code"], "access_denied")
        self.assertTrue(response.context["api"]["export_paused"])
        self.assertEqual(response.context["api"]["paused_until"], state.api_paused_until)
        MoySkladApiState.objects.create(account_id=ACCOUNT_ID)
        response = self.client.get(self.technical_url())
        self.assertEqual(response.context["api"]["access_error_code"], "")
        self.assertFalse(response.context["api"]["export_paused"])
        self.assertIsNone(response.context["api"]["paused_until"])

    def test_technical_diagnostics_have_local_explanations_and_stream_boundaries(self):
        state = self.healthy_state(webhook_id=uuid.uuid4(), webhook_received_at=NOW - timedelta(minutes=2),
                                   webhook_processed_at=NOW - timedelta(minutes=1))
        login_verified_admin(self.client, self.technical)
        response = self.client.get(self.technical_url())
        self.assertEqual(response.status_code, 200)
        for value in (str(ACCOUNT_ID), ENVIRONMENT, str(state.webhook_id),
                      state.export_consent_revision.revision_id):
            self.assertContains(response, value)
        text = self.text(response)
        for explanation in (
            r"heartbeat.{0,350}(?:2\s*мин|двух минут)",
            r"(?:проверка доступа|доступ api).{0,400}(?:не гарант|текущ|сейчас)",
            r"пауз[аы] api.{0,350}(?:запрос|повтор|огранич|приостанов)",
            r"пауз[аы] передач.{0,350}(?:аккаунт|лимит|заявк)",
            r"uuid.{0,300}(?:идентификатор|регистрац).{0,220}(?:не.*секрет|без.*секрет|не.*url|не.*адрес)",
            r"при[её]м.{0,350}обработк.{0,350}(?:получ|принят|врем|очеред)",
            r"последн.{0,100}успешн.{0,400}(?:заверш|выполн)",
            r"следующ.{0,120}сверк.{0,400}(?:план|расписан|не.*успех|заплан)",
            r"каталог.{0,500}(?:вручную|ручн).{0,350}(?:не публику|без.*публикац)",
            r"(?:цен[ыа]|автоматические цены).{0,500}(?:только|лишь).{0,70}связан",
            r"передача заявок.{0,700}нов.{0,350}(?:одобр|соглас)",
            r"историческ.{0,120}автоматически не отправ",
            r"(?:уч[её]тные статусы|статусы).{0,500}(?:мойсклад|уч[её]тн).{0,300}(?:не.*остат|не.*движен)",
            r"(?:редакци|соглас).{0,500}(?:дата|зарегистр|одобр)",
        ):
            with self.subTest(explanation=explanation):
                self.assertRegex(text, explanation)
        for action in ("check", "stream", "start_export"):
            self.assertContains(response, reverse("admin:moysklad_action", args=(action,)))
        self.assertNotContains(response, moysklad_config()["webhook_secret"])
        self.assertNotContains(response, moysklad_config()["api_token"])
