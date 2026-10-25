import hashlib
import io
import json
import unittest
from unittest.mock import patch
import zipfile

from deploy.agent.github import GitHub, validate_release
from deploy.configure_github import rulesets

SHA = "a" * 40
CONFIG = {"repository": "FrankHeels/Ribeshka", "image": "ghcr.io/frankheels/ribeshka", "platform": "linux/amd64"}
RUN = {"head_sha": SHA, "id": 17, "run_attempt": 2, "event": "push", "head_branch": "main",
       "head_repository": {"full_name": CONFIG["repository"]}, "status": "completed", "conclusion": "success"}
MANIFEST = {"version": 1, **CONFIG, "sha": SHA, "run_id": 17, "run_attempt": 2, "digest": "sha256:" + "b" * 64}


class ProvenanceTests(unittest.TestCase):
    def test_each_provenance_field_is_bound_to_the_successful_run(self):
        validate_release(MANIFEST, CONFIG, RUN)
        for key in MANIFEST:
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_release(MANIFEST | {key: "wrong"}, CONFIG, RUN)

    def test_pr_failure_pending_and_foreign_repository_are_never_deployable(self):
        github = GitHub(CONFIG, "test")
        for changes in ({"event": "pull_request"}, {"conclusion": "failure"}, {"status": "in_progress"},
                        {"head_branch": "feature"}, {"head_repository": {"full_name": "attacker/repo"}}):
            with self.subTest(changes=changes), patch.object(github, "api", return_value={"workflow_runs": [RUN | changes]}):
                self.assertIsNone(github.release(SHA))

    def test_latest_failed_rerun_invalidates_old_success(self):
        github = GitHub(CONFIG, "test")
        with patch.object(github, "api", return_value={"workflow_runs": [RUN, RUN | {"run_attempt": 3, "conclusion": "failure"}]}):
            self.assertIsNone(github.release(SHA))

    def test_successful_release_ci_without_merged_pr_never_downloads_artifact(self):
        github = GitHub(CONFIG, "test")
        with patch.object(github, "api", return_value={"workflow_runs": [RUN]}) as api, \
                patch.object(github, "verified_pr", return_value=False):
            self.assertIsNone(github.release(SHA))
            self.assertEqual(api.call_count, 1)

    def test_recovery_uses_separate_saved_target_admission(self):
        github = GitHub(CONFIG, "test")
        good = "c" * 40
        with patch.object(github, "api", return_value={"workflow_runs": [RUN]}), \
                patch.object(github, "verified_recovery", return_value=False) as check, \
                patch.object(github, "verified_pr") as pull:
            self.assertIsNone(github.release(SHA, recovery=good))
            check.assert_called_once_with(SHA, good, RUN)
            pull.assert_not_called()

    def test_downloaded_manifest_digest_is_verified_without_extracting_paths(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("release.json", json.dumps(MANIFEST))
        raw = archive.getvalue()
        artifact = {"name": f"release-{SHA}-2", "id": 8, "expired": False, "size_in_bytes": len(raw),
                    "digest": "sha256:" + hashlib.sha256(raw).hexdigest()}
        github = GitHub(CONFIG, "test")
        def api(path, **kwargs):
            if path.endswith("/zip"):
                return raw
            if "/artifacts?" in path:
                return {"artifacts": [artifact]}
            return {"workflow_runs": [RUN]}
        with patch.object(github, "api", side_effect=api), patch.object(github, "verified_pr", return_value=True):
            self.assertEqual(github.release(SHA), MANIFEST)
            artifact["digest"] = "sha256:" + "0" * 64
            with self.assertRaises(ValueError):
                github.release(SHA)

    def test_bot_cannot_force_push_or_delete_main(self):
        history, reviews = rulesets(123)
        self.assertEqual(history["bypass_actors"], [])
        self.assertEqual({r["type"] for r in history["rules"]}, {"deletion", "non_fast_forward"})
        self.assertEqual(reviews["bypass_actors"], [{"actor_id": 123, "actor_type": "Integration", "bypass_mode": "always"}])
        github = GitHub(CONFIG, "test")
        with patch.object(github, "api") as api:
            github.advance(SHA)
            api.assert_called_once_with("/git/refs/heads/main", {"sha": SHA, "force": False}, "PATCH")
