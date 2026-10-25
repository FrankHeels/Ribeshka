import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deploy.agent.state import Agent, atomic_json

A, B, C, R = (c * 40 for c in "abcr")


def release(sha):
    return {"sha": sha, "image": "ghcr.io/frankheels/ribeshka", "digest": "sha256:" + sha[0] * 64}


class GitHub:
    def __init__(self):
        self.tip = B
        self.releases = {A: release(A), B: release(B)}
        self.reverts = []
        self.race = False
        self.lost_response = False
        self.fail_revert = False
        self.parents = {B: A, C: B}

    def head(self):
        return self.tip

    def release(self, sha, *, recovery=None):
        return self.releases.get(sha)

    def compatible(self, base, head, ledger):
        return {"catalog/migrations/0009_add.py": "hash"}

    def migrations(self, sha):
        return {"catalog/migrations/0001_initial.py": "baseline"}

    def create_revert(self, head, good):
        if self.fail_revert:
            raise RuntimeError("API unavailable")
        self.reverts.append((head, good))
        self.parents[R] = head
        return R

    def advance(self, sha):
        if self.race:
            self.race = False
            self.tip = C
            raise RuntimeError("ref advanced during update")
        self.tip = sha
        if self.lost_response:
            self.lost_response = False
            raise RuntimeError("response lost after successful update")

    def contains(self, ancestor, head):
        for _ in range(10):
            if ancestor == head:
                return True
            head = self.parents.get(head)
        return False

    def same_tree(self, left, right):
        return left == R and right == A


class Runtime:
    def __init__(self):
        self.calls = []
        self.fail = None
        self.after_prepare = None

    def workers(self):
        return ["worker", "moysklad-worker"]

    def call(self, name, rel=None, workers=None):
        self.calls.append((name, rel and rel["sha"], workers))
        if self.fail == (name, rel and rel["sha"]):
            raise RuntimeError("simulated failure")

    def prepare(self, rel):
        self.call("prepare", rel)
        if self.after_prepare:
            self.after_prepare()

    def quiesce(self, workers):
        self.call("quiesce", workers=workers)

    def backup(self, rel):
        self.call("backup", rel)
        return {"id": "verified-pair"}

    def migrate(self, rel):
        self.call("migrate", rel)

    def static(self, rel):
        self.call("static", rel)

    def activate(self, rel, workers):
        self.call("activate", rel, workers)

    def health(self, rel, workers):
        self.call("health", rel, workers)


class DeploymentScenarios(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "state.json"
        self.github, self.runtime = GitHub(), Runtime()
        self.agent = Agent(self.path, self.github, self.runtime)
        self.agent.save("healthy", current=release(A), workers=["worker"], migration_ledger={})

    def reload(self):
        self.agent = Agent(self.path, self.github, self.runtime)

    def test_success_backup_before_migrations_static_before_switch_and_preserves_workers(self):
        self.agent.tick()
        self.assertEqual([c[0] for c in self.runtime.calls], ["prepare", "quiesce", "backup", "migrate", "static", "activate", "health"])
        self.assertEqual(self.agent.state["current"]["sha"], B)
        self.assertEqual(self.agent.state["previous"]["sha"], A)
        self.assertEqual(self.runtime.calls[-2][2], ["worker", "moysklad-worker"])
        self.assertEqual(self.agent.state["phase"], "healthy")

    def test_failed_pending_or_absent_ci_never_reaches_host_operations(self):
        self.github.releases.pop(B)
        self.agent.tick()
        self.assertEqual(self.runtime.calls, [])
        self.assertEqual(self.agent.state["current"]["sha"], A)

    def test_failure_restores_all_processes_and_waits_for_recovery_ci(self):
        for action in ("backup", "migrate", "static", "activate", "health"):
            with self.subTest(action=action):
                self.setUp()
                self.runtime.fail = (action, A if action == "backup" else B)
                self.agent.tick()
                self.assertEqual(self.agent.state["phase"], "awaiting_recovery_ci")
                self.assertEqual(self.github.reverts, [(B, A)])
                self.assertEqual(self.runtime.calls[-2][0:2], ("activate", A))
                self.assertEqual(self.runtime.calls[-1][0:2], ("health", A))
                self.assertEqual(self.agent.state["current"]["sha"], A)
                self.assertNotIn("restore_database", [c[0] for c in self.runtime.calls])

    def test_recovery_ci_produces_same_sha_on_server_and_main_without_migrations(self):
        self.runtime.fail = ("health", B)
        self.agent.tick()
        self.github.releases[R] = release(R)
        self.runtime.calls.clear()
        self.reload()
        self.agent.tick()
        self.assertEqual(self.agent.state["phase"], "healthy")
        self.assertEqual(self.agent.state["current"]["sha"], self.github.tip)
        self.assertNotIn("migrate", [c[0] for c in self.runtime.calls])
        self.assertEqual(len(self.github.reverts), 1)

    def test_waiting_does_not_restart_healthy_previous_release_every_poll(self):
        self.runtime.fail = ("migrate", B)
        self.agent.tick()
        self.runtime.calls.clear()
        self.agent.tick()
        self.assertEqual([c[0] for c in self.runtime.calls], ["health"])

    def test_two_sequential_merges_install_in_order(self):
        self.agent.tick()
        self.github.tip = C
        self.github.releases[C] = release(C)
        self.agent.tick()
        self.assertEqual([c[1] for c in self.runtime.calls if c[0] == "activate"], [B, C])
        self.assertEqual(self.agent.state["previous"]["sha"], B)

    def test_superseded_candidate_is_not_installed(self):
        self.runtime.after_prepare = lambda: setattr(self.github, "tip", C)
        self.agent.tick()
        self.assertEqual([c[0] for c in self.runtime.calls], ["prepare"])
        self.assertEqual(self.agent.state["current"]["sha"], A)

    def test_second_merge_is_blocked_until_recovery_finishes(self):
        self.runtime.fail = ("migrate", B)
        self.agent.tick()
        self.github.parents[C] = R
        self.github.tip = C
        self.github.releases[C] = release(C)
        self.runtime.calls.clear()
        self.agent.tick()
        self.assertNotIn(C, [c[1] for c in self.runtime.calls])
        self.assertEqual(self.agent.state["phase"], "awaiting_recovery_ci")
        self.github.releases[R] = release(R)
        self.agent.tick()
        self.assertEqual(self.agent.state["current"]["sha"], R)
        self.agent.tick()
        self.assertEqual(self.agent.state["current"]["sha"], C)

    def test_process_restart_in_each_mutating_phase_restores_before_any_new_release(self):
        for phase in ("deploying", "migrating", "switching", "recovering"):
            with self.subTest(phase=phase):
                self.setUp()
                self.agent.save(phase, candidate=release(B))
                self.reload()
                self.agent.tick()
                self.assertEqual(self.runtime.calls[0][0], "quiesce")
                self.assertEqual(self.runtime.calls[1][0:2], ("activate", A))
                self.assertEqual(self.agent.state["phase"], "awaiting_recovery_ci")

    def test_restored_phases_check_health_without_restarting_on_github_failure(self):
        for phase in ("reverted_runtime", "reverting_main", "awaiting_recovery_ci"):
            with self.subTest(phase=phase):
                self.setUp()
                self.agent.save(phase, candidate=release(B), running_sha=A)
                self.github.fail_revert = True
                with self.assertRaises(RuntimeError):
                    self.agent.tick()
                self.assertEqual([c[0] for c in self.runtime.calls], ["health"])

    def test_unhealthy_restored_runtime_is_restarted_before_revert_retry(self):
        self.agent.save("reverted_runtime", candidate=release(B), running_sha=A)
        with patch.object(self.runtime, "health", side_effect=[RuntimeError("unavailable"), None]):
            self.agent.tick()
        self.assertEqual([c[0] for c in self.runtime.calls], ["quiesce", "activate"])
        self.assertEqual(self.agent.state["phase"], "awaiting_recovery_ci")

    def test_revert_api_failure_keeps_recovery_block_and_retries(self):
        self.github.fail_revert = True
        self.runtime.fail = ("health", B)
        with self.assertRaises(RuntimeError):
            self.agent.tick()
        self.reload()
        self.assertNotEqual(self.agent.state["phase"], "healthy")
        self.github.fail_revert = False
        self.agent.tick()
        self.assertEqual(self.agent.state["phase"], "awaiting_recovery_ci")

    def test_lost_ref_update_response_does_not_create_second_revert(self):
        self.github.lost_response = True
        self.runtime.fail = ("migrate", B)
        with self.assertRaises(RuntimeError):
            self.agent.tick()
        self.reload()
        self.agent.tick()
        self.assertEqual(len(self.github.reverts), 1)

    def test_ref_race_reverts_new_head_without_force_or_lost_history(self):
        self.github.race = True
        self.runtime.fail = ("migrate", B)
        with self.assertRaises(RuntimeError):
            self.agent.tick()
        self.reload()
        self.agent.tick()
        self.assertEqual(self.github.reverts, [(B, A), (C, A)])

    def test_failed_old_health_blocks_revert_and_future_deployments(self):
        self.agent.save("recovering")
        self.runtime.fail = ("health", A)
        with self.assertRaises(RuntimeError):
            self.agent.tick()
        self.assertEqual(self.github.reverts, [])
        self.assertEqual(self.agent.state["phase"], "recovering")

    def test_bootstrap_requires_verified_current_main_and_running_health(self):
        self.path.unlink()
        self.reload()
        with self.assertRaises(ValueError):
            self.agent.bootstrap(A)
        self.agent.bootstrap(B)
        self.assertEqual([c[0] for c in self.runtime.calls], ["health"])
        self.assertEqual(self.agent.state["current"]["sha"], B)
        self.assertTrue(self.agent.state["migration_ledger"])

    def test_corrupt_or_unknown_state_is_not_reset(self):
        self.path.write_text("broken")
        with self.assertRaises(json.JSONDecodeError):
            self.reload()
        atomic_json(self.path, {"version": 999})
        with self.assertRaises(ValueError):
            self.reload()
