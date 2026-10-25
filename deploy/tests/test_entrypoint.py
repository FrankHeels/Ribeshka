"""Exercise the real CLI boundary with isolated state and synthetic adapters."""
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from deploy.agent import __main__ as entrypoint
from deploy.agent.github import APIError, GitHub as APIClient
from deploy.agent.runtime import OperationFailed
from deploy.agent.state import Agent
from deploy.tests.test_state import A, B, GitHub, Runtime, release


class ReportingGitHub(GitHub):
    def __init__(self):
        super().__init__()
        self.reports = []
        self.status_unavailable = False

    def rules_ready(self):
        pass

    def api(self, path, data):
        if self.status_unavailable:
            raise APIError("private-response-marker")
        self.reports.append((path, copy.deepcopy(data)))

    report = APIClient.report


class EntrypointRecoveryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.path = self.root / "state.json"
        self.config = {"repository": "example/fishsite", "state_dir": str(self.root),
                       "lock_file": str(self.root / "operations.lock")}
        self.github, self.runtime = ReportingGitHub(), Runtime()
        self.agent = Agent(self.path, self.github, self.runtime)
        self.agent.save("healthy", current=release(A), workers=["worker"], migration_ledger={})

    def invoke(self, *, real_client=False):
        stdout, stderr = io.StringIO(), io.StringIO()
        old_umask = os.umask(0o077)
        try:
            with patch.object(entrypoint, "load_config", return_value=self.config), \
                    patch.object(entrypoint, "Runtime", return_value=self.runtime), \
                    patch.object(entrypoint, "GitHub", APIClient if real_client else lambda *a: self.github), \
                    patch("sys.argv", ["agent", "once"]), \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                result = entrypoint.main()
        finally:
            os.umask(old_umask)
        return result, stdout.getvalue() + stderr.getvalue()

    def state(self):
        return json.loads(self.path.read_text())

    def test_interrupted_deploy_restores_before_authentication_and_retries_without_restart(self):
        self.agent.save("migrating", candidate=release(B), main_sha=B)

        def unavailable_token(config):
            self.assertIn(("activate", A, ["worker"]), self.runtime.calls)
            raise APIError("private-response-marker")

        with patch("deploy.agent.github.app_token", side_effect=unavailable_token):
            result, output = self.invoke(real_client=True)
        self.assertEqual(result, 1)
        self.assertEqual(self.state()["phase"], "reverted_runtime")
        self.assertIn("APIError", self.state()["blocked_reason"])
        self.assertNotIn("private-response-marker", output + self.path.read_text())
        self.runtime.calls.clear()
        with patch("deploy.agent.github.app_token", side_effect=APIError("unavailable")):
            self.assertEqual(self.invoke(real_client=True)[0], 1)
        self.assertEqual([c[0] for c in self.runtime.calls], ["health"])

    def test_prepare_failure_persists_reason_and_publishes_error_without_stopping_site(self):
        with patch.object(self.runtime, "prepare", side_effect=OperationFailed("private-response-marker")):
            result, output = self.invoke()
        state = self.state()
        self.assertEqual(result, 1)
        self.assertEqual(state["phase"], "healthy")
        self.assertEqual(state["current"]["sha"], A)
        self.assertEqual(state["main_sha"], B)
        self.assertIn("OperationFailed", state["blocked_reason"])
        self.assertEqual(self.runtime.calls, [])
        path, report = self.github.reports[-1]
        self.assertEqual(path, f"/statuses/{B}")
        self.assertEqual(report["state"], "error")
        self.assertIn(A[:12], report["description"])
        self.assertNotIn("private-response-marker", output + self.path.read_text() + json.dumps(report))

    def test_revert_failure_is_reported_and_retries_do_not_restart_restored_site(self):
        self.runtime.fail = ("migrate", B)
        self.github.fail_revert = True
        self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(self.state()["phase"], "reverted_runtime")
        self.assertEqual(self.github.reports[-1][1]["state"], "error")
        self.runtime.calls.clear()
        self.assertEqual(self.invoke()[0], 1)
        self.assertEqual([c[0] for c in self.runtime.calls], ["health"])
        self.assertEqual(len(self.github.reports), 1)

    def test_unavailable_status_api_keeps_reason_and_retries_reporting_later(self):
        self.github.status_unavailable = True
        self.runtime.fail = ("prepare", B)
        self.assertEqual(self.invoke()[0], 1)
        self.assertTrue(self.state()["blocked_reason"])
        self.assertFalse((self.root / "reported.json").exists())
        self.github.status_unavailable = False
        self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(self.github.reports[-1][1]["state"], "error")

    def test_success_clears_block_and_replaces_error_status(self):
        self.runtime.fail = ("prepare", B)
        self.assertEqual(self.invoke()[0], 1)
        self.runtime.fail = None
        self.assertEqual(self.invoke()[0], 0)
        self.assertIsNone(self.state()["blocked_reason"])
        self.assertEqual(self.state()["current"]["sha"], B)
        self.assertEqual([r[1]["state"] for r in self.github.reports], ["error", "success"])

    def test_pending_ci_clears_old_preparation_error_without_starting_deployment(self):
        self.runtime.fail = ("prepare", B)
        self.assertEqual(self.invoke()[0], 1)
        self.github.releases.pop(B)
        self.runtime.calls.clear()
        self.assertEqual(self.invoke()[0], 0)
        self.assertIsNone(self.state()["blocked_reason"])
        self.assertEqual(self.runtime.calls, [])
        self.assertEqual(self.github.reports[-1][1]["state"], "pending")

    def test_failed_local_recovery_persists_error_before_attempting_status_publication(self):
        self.agent.save("switching", candidate=release(B), main_sha=B)
        self.runtime.fail = ("activate", A)

        def unavailable_token(config):
            self.assertIn(("activate", A, ["worker"]), self.runtime.calls)
            self.assertIn("RuntimeError", self.state()["blocked_reason"])
            raise APIError("unavailable")

        with patch("deploy.agent.github.app_token", side_effect=unavailable_token):
            self.assertEqual(self.invoke(real_client=True)[0], 1)
        # Reporting may try GitHub, but only after local recovery was attempted.
        self.assertIn(("activate", A, ["worker"]), self.runtime.calls)
        self.assertEqual(self.state()["phase"], "recovering")
        self.assertIn("RuntimeError", self.state()["blocked_reason"])
        self.assertNotIn("prepare", [c[0] for c in self.runtime.calls])

    def test_status_outage_after_success_is_retried_without_redeploying(self):
        self.github.status_unavailable = True
        self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(self.state()["current"]["sha"], B)
        self.assertIn("status publication", self.state()["blocked_reason"])
        self.github.status_unavailable = False
        self.runtime.calls.clear()
        self.assertEqual(self.invoke()[0], 0)
        self.assertEqual(self.runtime.calls, [])
        self.assertIsNone(self.state()["blocked_reason"])
        self.assertEqual(self.github.reports[-1][1]["state"], "success")

    def test_changed_failure_reason_is_reported_for_same_phase_and_sha(self):
        for error in (OperationFailed("private-response-marker"), TimeoutError("private-response-marker")):
            with patch.object(self.runtime, "prepare", side_effect=error):
                self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(len(self.github.reports), 2)
        self.assertIn("OperationFailed", self.github.reports[0][1]["description"])
        self.assertIn("TimeoutError", self.github.reports[1][1]["description"])

    def test_admission_error_is_not_reported_as_success_when_running_sha_matches_main(self):
        self.github.tip = A
        with patch.object(self.github, "rules_ready", side_effect=APIError("unavailable")):
            self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(self.github.reports[-1][1]["state"], "error")
        self.assertEqual(self.runtime.calls, [])
        self.assertEqual(self.invoke()[0], 0)
        self.assertEqual(self.github.reports[-1][1]["state"], "success")

    def test_api_authenticates_only_on_first_request_and_reuses_token(self):
        with patch("deploy.agent.github.app_token", return_value="synthetic") as token, \
                patch("deploy.agent.github.request", return_value={}) as request:
            client = APIClient(self.config)
            token.assert_not_called()
            client.api("/one")
            client.api("/two")
        token.assert_called_once_with(self.config)
        self.assertEqual(request.call_count, 2)

