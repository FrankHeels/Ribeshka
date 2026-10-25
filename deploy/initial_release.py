"""One-time migration from the existing Compose stack to a verified baseline.

Keeps the legacy Compose/env/static available for rollback, including after a
process crash. Does not arm the timer or fabricate a successful baseline.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil

from deploy.agent.__main__ import load_config
from deploy.agent.github import GitHub, SHA, app_token
from deploy.agent.runtime import Runtime
from deploy.agent.state import Agent, atomic_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "recover"))
    parser.add_argument("--config", default="/etc/fishsite/deployer.json")
    parser.add_argument("--legacy-compose")
    parser.add_argument("--legacy-env")
    args = parser.parse_args()
    os.umask(0o077)
    config = load_config(args.config)
    runtime = Runtime(config)
    journal_path = runtime.root / "initial-release.json"
    state_path = runtime.root / "state.json"

    def restore(journal):
        runtime.quiesce(journal["workers"])
        command = ["docker", "compose", "--project-name", "fishsite", "--env-file", journal["env"],
                   "-f", journal["compose"], "--profile", "notifications", "--profile", "moysklad"]
        environment = runtime.environment | {"FISHSITE_IMAGE": journal["previous"]["image_id"]}
        runtime.run([*command, "up", "-d", "--no-deps", "--force-recreate", "--wait", "--wait-timeout", "120",
                     "web", *journal["workers"]], env=environment)
        runtime.run([*command, "up", "-d", "--no-deps", "--force-recreate", "nginx"], env=environment)
        runtime.run([*command, "exec", "-T", "nginx", "nginx", "-t"], env=environment)
        for path in ("/", config["catalog_path"]):
            if b"</html>" not in runtime.response(path).lower():
                raise RuntimeError("Legacy restoration did not pass HTTPS checks")
        atomic_json(journal_path, journal | {"phase": "restored"})

    with open(config["lock_file"], "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if state_path.exists():
            raise ValueError("Agent baseline already exists; use the normal agent recovery")
        if journal_path.exists():
            journal = json.loads(journal_path.read_text())
            if journal["phase"] not in {"restored", "complete"} or args.action == "recover":
                restore(journal)
                print("Legacy stack restored; no database restore performed")
                return
        if args.action == "recover":
            raise ValueError("No interrupted initial release")
        if not args.legacy_compose or not args.legacy_env:
            parser.error("First install requires --legacy-compose and --legacy-env")
        github = GitHub(config, app_token(config))
        github.rules_ready()
        sha = github.head()
        release = github.release(sha)
        if not release:
            raise ValueError("Current main has no successful release")
        # Validate the previous public stack before relying on it as recovery target.
        for path in ("/", config["catalog_path"]):
            if b"</html>" not in runtime.response(path).lower():
                raise ValueError("Previous stack is not healthy")
        runtime.prepare(release)
        folder = runtime.root / "legacy"
        folder.mkdir(mode=0o700, exist_ok=True)
        shutil.copyfile(args.legacy_compose, folder / "compose.yaml")
        shutil.copyfile(args.legacy_env, folder / "images.env")
        web_ids = runtime.run(["docker", "ps", "-q", "--filter", "label=com.docker.compose.project=fishsite",
                               "--filter", "label=com.docker.compose.service=web"]).splitlines()
        if len(web_ids) != 1:
            raise ValueError("Expected one previous web container")
        old = json.loads(runtime.run(["docker", "inspect", web_ids[0]]))[0]
        old_sha = old["Config"].get("Labels", {}).get("org.opencontainers.image.revision", "")
        if not SHA.fullmatch(old_sha):
            raise ValueError("Previous image must identify its source commit before baseline installation")
        if old_sha != sha:
            github.compatible(old_sha, sha, github.migrations(old_sha))
        journal = {"phase": "installing", "compose": str(folder / "compose.yaml"), "env": str(folder / "images.env"),
                   "workers": runtime.workers(), "candidate": release, "previous": {
                       "sha": old_sha,
                       "image_id": old["Image"], "legacy": True}}
        atomic_json(journal_path, journal)
        try:
            runtime.quiesce(journal["workers"])
            receipt = runtime.backup(journal["previous"])
            atomic_json(journal_path, journal | {"backup": receipt})
            runtime.migrate(release)
            runtime.static(release)
            runtime.activate(release, journal["workers"])
            runtime.health(release, journal["workers"])
            agent = Agent(state_path, github, runtime)
            agent.bootstrap(sha)
        except Exception:
            # Once state exists, the normal recovery agent owns it. Never roll back
            # behind a persisted baseline based on a late reporting failure.
            if not state_path.exists():
                restore(journal)
            raise
        atomic_json(journal_path, journal | {"phase": "complete", "backup": receipt})
        github.report(agent.state)
        print(f"Verified baseline installed: {sha}. The timer can now be enabled.")


if __name__ == "__main__":
    main()
