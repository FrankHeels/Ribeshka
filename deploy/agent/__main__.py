"""Run with python3 -m deploy.agent --config /etc/fishsite/deployer.json once."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import sys

from .github import GitHub, app_token
from .runtime import Runtime
from .state import Agent, atomic_json


def load_config(path):
    config = json.loads(Path(path).read_text())
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", config["repository"]):
        raise ValueError("Invalid repository")
    if config["image"] != "ghcr.io/" + config["repository"].lower() or config["platform"] != "linux/amd64":
        raise ValueError("Image repository/architecture do not match this VPS")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]+[a-z0-9]", config["hostname"]):
        raise ValueError("Expected an ASCII hostname")
    if not re.fullmatch(r"/catalog/categories/[A-Za-z0-9_-]+/", config["catalog_path"]):
        raise ValueError("Configure an existing published catalog category for smoke checks")
    for key in ("state_dir", "lock_file", "app_key", "compose_file", "images_env", "media_dir", "backup_recipient_file"):
        if not Path(config[key]).is_absolute():
            raise ValueError(f"Absolute path required: {key}")
    for key in ("app_id", "installation_id"):
        if type(config[key]) is not int or config[key] <= 0:
            raise ValueError(f"Configure {key}")
    command = config.get("backup_upload_command", [])
    if command and (not isinstance(command, list) or not Path(command[0]).is_absolute()):
        raise ValueError("Backup upload command must be an absolute executable argv list")
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/fishsite/deployer.json")
    parser.add_argument("action", choices=("once", "status", "bootstrap"))
    parser.add_argument("--sha")
    args = parser.parse_args()
    os.umask(0o077)
    config = load_config(args.config)
    state_path = Path(config["state_dir"]) / "state.json"
    if args.action == "status":
        print(state_path.read_text() if state_path.exists() else '{"phase":"uninitialized"}')
        return 0
    with open(config["lock_file"], "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another deploy, backup or import owns the operations lock")
            return 0
        github = GitHub(config, app_token(config))
        runtime = Runtime(config)
        agent = Agent(state_path, github, runtime)
        try:
            # Existing recovery must proceed even if protection is later misconfigured.
            if agent.state["phase"] in {"uninitialized", "healthy"}:
                github.rules_ready()
            if args.action == "bootstrap":
                if not args.sha:
                    raise ValueError("bootstrap requires --sha")
                agent.bootstrap(args.sha)
            else:
                agent.tick()
            stamp = {key: agent.state.get(key) for key in ("phase", "main_sha", "current", "recovery_commit")}
            report_path = Path(config["state_dir"]) / "reported.json"
            if not report_path.exists() or json.loads(report_path.read_text()) != stamp:
                github.report(agent.state)
                atomic_json(report_path, stamp)
            print(json.dumps({"phase": agent.state["phase"], "running_sha": agent.state["current"]["sha"]}))
            return 0
        except Exception as error:
            # Do not print subprocess output, server data or credential-bearing URLs.
            print(f"Deployment stopped: {type(error).__name__}; inspect private state/logs", file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
