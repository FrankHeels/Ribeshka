"""Validate ordinary changes, or an exact-tree recovery pushed by the rollback App."""
import json
import os
import re
import subprocess

from deploy.migration_policy import check_git


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def main():
    event = json.loads(open(os.environ["GITHUB_EVENT_PATH"]).read())
    base = os.environ["MIGRATION_BASE"]
    message = git("log", "-1", "--format=%B")
    match = re.search(r"^FishSite-Restore: ([0-9a-f]{40})$", message, re.M)
    sender = event.get("sender", {})
    if (os.environ["GITHUB_EVENT_NAME"] == "push" and match
            and sender.get("type") == "Bot"
            and sender.get("login") == os.environ.get("ROLLBACK_BOT_LOGIN", "")
            and event.get("after") == git("rev-parse", "HEAD")):
        target = match[1]
        subprocess.run(["git", "merge-base", "--is-ancestor", target, "HEAD"], check=True)
        if git("rev-parse", f"{target}^{{tree}}") != git("rev-parse", "HEAD^{tree}"):
            raise ValueError("Rollback must restore exactly the previously verified source tree")
        print("Exact-tree rollback by configured GitHub App")
        return
    if not re.fullmatch(r"[0-9a-f]{40}", base) or base == "0" * 40:
        raise ValueError("A real migration baseline is required")
    check_git(base)
    print("Expand-only migration policy passed")


if __name__ == "__main__":
    main()
