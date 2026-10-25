import copy
import unittest
from unittest.mock import patch

from deploy.agent.github import GitHub


SHA, HEAD, GOOD = (c * 40 for c in "abc")
REPOSITORY = "FrankHeels/Ribeshka"
CONFIG = {"repository": REPOSITORY, "repository_policy": "private-free",
          "rollback_bot_login": "fishsite-rollback-frankheels[bot]"}
PR = {"number": 2, "state": "closed", "merged_at": "2026-10-25T20:00:00Z",
      "merge_commit_sha": SHA, "base": {"ref": "main", "repo": {"full_name": REPOSITORY}},
      "head": {"sha": HEAD, "ref": "codex/feature", "repo": {"full_name": REPOSITORY}}}
RUN = {"id": 20, "run_attempt": 1, "head_sha": HEAD, "event": "pull_request",
       "head_branch": "codex/feature",
       "status": "completed", "conclusion": "success", "head_repository": {"full_name": REPOSITORY},
       "pull_requests": [{"number": 2, "head": {"sha": HEAD}, "base": {"ref": "main"}}]}


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.github = GitHub(CONFIG, "test")
        self.pulls, self.runs = [copy.deepcopy(PR)], [copy.deepcopy(RUN)]

    def api(self, path):
        if path == f"/commits/{SHA}/pulls?per_page=100":
            return self.pulls
        if path == f"/actions/workflows/ci.yml/runs?event=pull_request&head_sha={HEAD}&per_page=100":
            return {"workflow_runs": self.runs}
        raise AssertionError(path)

    def admitted(self):
        with patch.object(self.github, "api", side_effect=self.api):
            return self.github.verified_pr(SHA)

    def test_merged_pr_and_latest_head_ci_are_required(self):
        self.assertTrue(self.admitted())
        self.pulls = []
        self.assertFalse(self.admitted())

    def test_open_unmerged_wrong_base_or_different_merge_is_rejected(self):
        for changes in ({"state": "open"}, {"merged_at": None}, {"merge_commit_sha": GOOD},
                        {"base": {"ref": "other", "repo": {"full_name": REPOSITORY}}},
                        {"base": {"ref": "main", "repo": {"full_name": "other/repo"}}}):
            with self.subTest(changes=changes):
                self.pulls = [PR | changes]
                self.assertFalse(self.admitted())

    def test_ci_for_wrong_head_event_repo_or_pull_request_is_rejected(self):
        for changes in ({"head_sha": GOOD}, {"event": "push"}, {"head_branch": "other"},
                        {"head_repository": {"full_name": "other/repo"}},
                        {"pull_requests": [{"number": 3, "head": {"sha": HEAD}, "base": {"ref": "main"}}]},
                        {"conclusion": "failure"}, {"status": "in_progress"}):
            with self.subTest(changes=changes):
                self.runs = [RUN | changes]
                self.assertFalse(self.admitted())

    def test_github_omits_run_pull_links_after_merge_but_keeps_exact_head_identity(self):
        self.runs = [RUN | {"pull_requests": []}]
        self.assertTrue(self.admitted())
        self.runs = [RUN | {"pull_requests": [], "head_branch": "other"}]
        self.assertFalse(self.admitted())

    def test_newer_failed_rerun_invalidates_old_pr_success(self):
        self.runs = [RUN, RUN | {"run_attempt": 2, "conclusion": "failure"}]
        self.assertFalse(self.admitted())

    def test_ambiguous_or_truncated_pr_result_is_rejected(self):
        for pulls in ([PR, PR], [PR] * 100):
            self.pulls = pulls
            self.assertFalse(self.admitted())

    def test_free_policy_does_not_query_unavailable_rules(self):
        with patch.object(self.github, "api", return_value={"private": True, "full_name": REPOSITORY}) as api:
            self.github.rules_ready()
            api.assert_called_once_with("")

    def test_unknown_policy_fails_closed(self):
        github = GitHub(CONFIG | {"repository_policy": "typo"}, "test")
        with patch.object(github, "api") as api, self.assertRaises(ValueError):
            github.rules_ready()
        api.assert_not_called()

    def test_recovery_requires_real_bot_push_and_exact_saved_tree(self):
        run = {"actor": {"type": "Bot", "login": CONFIG["rollback_bot_login"]}}
        commit = {"message": f"Revert failure\n\nFishSite-Restore: {GOOD}", "parents": [{"sha": HEAD}]}
        with patch.object(self.github, "commit", return_value=commit), \
                patch.object(self.github, "contains", return_value=True), \
                patch.object(self.github, "same_tree", return_value=True):
            self.assertTrue(self.github.verified_recovery(SHA, GOOD, run))
            for actor in ({"type": "User", "login": CONFIG["rollback_bot_login"]},
                          {"type": "Bot", "login": "other[bot]"}):
                self.assertFalse(self.github.verified_recovery(SHA, GOOD, {"actor": actor}))
            with patch.object(self.github, "same_tree", return_value=False):
                self.assertFalse(self.github.verified_recovery(SHA, GOOD, run))
            with patch.object(self.github, "contains", return_value=False):
                self.assertFalse(self.github.verified_recovery(SHA, GOOD, run))
            with patch.object(self.github, "commit", return_value=commit | {"message": "fake"}):
                self.assertFalse(self.github.verified_recovery(SHA, GOOD, run))

    def test_direct_push_before_valid_pr_is_still_rejected(self):
        with patch.object(self.github, "verified_pr", side_effect=lambda sha: sha == SHA), \
                patch.object(self.github, "commit", return_value={"parents": [{"sha": HEAD}]}):
            with self.assertRaises(ValueError):
                self.github.verified_range(GOOD, SHA)

    def test_two_merged_prs_form_complete_first_parent_chain(self):
        parents = {SHA: HEAD, HEAD: GOOD}
        with patch.object(self.github, "verified_pr", return_value=True), \
                patch.object(self.github, "commit", side_effect=lambda sha: {"parents": [{"sha": parents[sha]}]}):
            self.github.verified_range(GOOD, SHA)

