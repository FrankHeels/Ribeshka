"""Idempotently install main rulesets; requires repository admin and paid private rules."""
import argparse
import json
import subprocess


def api(repository, path, body=None, method=None):
    command = ["gh", "api", f"repos/{repository}/{path}"]
    if body is not None:
        command += ["--method", method or "POST", "--input", "-"]
    result = subprocess.run(command, input=None if body is None else json.dumps(body), text=True, capture_output=True)
    if result.returncode:
        raise SystemExit("GitHub rejected rules configuration. Check repository admin rights and GitHub Pro for a private personal repository. No protection was claimed.")
    return json.loads(result.stdout)


def rulesets(app_id):
    base = {"target": "branch", "enforcement": "active",
            "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}}}
    return [
        base | {"name": "FishSite no history rewrites", "bypass_actors": [],
                "rules": [{"type": "deletion"}, {"type": "non_fast_forward"}]},
        base | {"name": "FishSite reviewed main", "bypass_actors": [
            {"actor_id": app_id, "actor_type": "Integration", "bypass_mode": "always"}],
            "rules": [
                {"type": "pull_request", "parameters": {
                    "required_approving_review_count": 1, "dismiss_stale_reviews_on_push": True,
                    "require_code_owner_review": False, "require_last_push_approval": True,
                    "required_review_thread_resolution": True, "allowed_merge_methods": ["squash"],
                }},
                {"type": "required_status_checks", "parameters": {
                    "strict_required_status_checks_policy": True,
                    "required_status_checks": [{"context": "CI required", "integration_id": 15368}],
                }},
            ]},
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default="FrankHeels/Ribeshka")
    parser.add_argument("--app-id", type=int, required=True)
    parser.add_argument("--bot-login", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.app_id <= 0 or not args.bot_login.endswith("[bot]"):
        parser.error("A real rollback App ID and bot login are required")
    desired = rulesets(args.app_id)
    if args.dry_run:
        print(json.dumps(desired, indent=2))
        return
    existing = api(args.repository, "rulesets?per_page=100")
    for value in desired:
        matches = [r for r in existing if r["name"] == value["name"]]
        if len(matches) > 1:
            raise SystemExit("Ambiguous existing rulesets")
        path = f'rulesets/{matches[0]["id"]}' if matches else "rulesets"
        api(args.repository, path, value, "PUT" if matches else "POST")
    # Use gh directly without printing tokens.
    subprocess.run(["gh", "variable", "set", "ROLLBACK_BOT_LOGIN", "--repo", args.repository,
                    "--body", args.bot_login], check=True, stdout=subprocess.DEVNULL)
    print("main requires fresh review and CI; only the rollback App bypasses PR/CI. Force push/deletion have no bypass.")


if __name__ == "__main__":
    main()
