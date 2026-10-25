"""Durable release state machine. All calls run under the host's shared flock."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Agent:
    def __init__(self, path, github, runtime):
        self.path, self.github, self.runtime = Path(path), github, runtime
        self.operation = "repository admission"
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {
            "version": 1, "phase": "uninitialized", "current": None, "previous": None,
            "candidate": None, "migration_ledger": {}, "workers": [],
        }
        if self.state.get("version") != 1:
            raise ValueError("Unknown deployment state format")

    def save(self, phase, **values):
        self.state.update(values, phase=phase, updated_at=datetime.now(timezone.utc).isoformat())
        atomic_json(self.path, self.state)

    def bootstrap(self, sha):
        if self.state["phase"] != "uninitialized":
            raise ValueError("Already initialized")
        release = self.github.release(sha)
        if not release or self.github.head() != sha:
            raise ValueError("Bootstrap requires a successful release for current main")
        # Adoption is allowed only after inspecting and probing the *running* digest.
        workers = self.runtime.workers()
        self.runtime.health(release, workers)
        ledger = self.github.migrations(sha)
        self.save("healthy", current=release, workers=workers, migration_ledger=ledger)

    def tick(self):
        phase = self.state["phase"]
        if phase == "uninitialized":
            raise ValueError("Bootstrap baseline is required before automatic deployment")
        if phase != "healthy":
            return self.recover()
        self.operation = "release admission"
        head = self.github.head()
        self.save("healthy", main_sha=head)
        if head == self.state["current"]["sha"]:
            return
        release = self.github.release(head)
        if release is None:  # Failed, pending or missing CI is never deployable.
            return
        self.operation = "migration compatibility"
        ledger = self.github.compatible(self.state["current"]["sha"], head, self.state["migration_ledger"])
        self.operation = "image preparation"
        self.runtime.prepare(release)
        if self.github.head() != head:
            return  # A newer merge superseded this candidate before any writes.
        workers = self.runtime.workers()
        self.save("deploying", candidate=release, workers=workers,
                  migration_ledger=self.state["migration_ledger"] | ledger,
                  recovery_commit=None, recovery_parent=None)
        self.operation = "deployment"
        try:
            self.runtime.quiesce(workers)
            backup = self.runtime.backup(self.state["current"])
            self.save("migrating", backup=backup)
            self.runtime.migrate(release)
            self.runtime.static(release)
            self.save("switching")
            self.runtime.activate(release, workers)
            self.runtime.health(release, workers)
        except Exception as error:
            self.save("recovering", error=type(error).__name__)
            self.recover()
            return
        self.save("healthy", previous=self.state["current"], current=release, candidate=None, error=None)

    def recover(self):
        """Idempotent after a crash at any side effect; never restore a database."""
        old, workers = self.state["current"], self.state["workers"]
        if not old:
            raise ValueError("No known good release: operator recovery required")
        self.operation = "local recovery"
        restored_phases = {"reverted_runtime", "reverting_main", "awaiting_recovery_ci"}
        # Always re-probe before touching Git, even if a prior attempt restored it.
        if self.state["phase"] in restored_phases:
            try:
                self.runtime.health(old, workers)
            except Exception:
                self.save("recovering")
        if self.state["phase"] not in restored_phases:
            self.save("recovering")
            self.runtime.quiesce(workers)
            self.runtime.activate(old, workers)
            self.runtime.health(old, workers)
            self.save("reverted_runtime", running_sha=old["sha"])
        self.operation = "GitHub recovery"
        head = self.github.head()
        commit = self.state.get("recovery_commit")
        if not commit or (head != self.state.get("recovery_parent") and not self.github.contains(commit, head)):
            # Create the commit first, persist its identity, then move the ref.
            commit = self.github.create_revert(head, old["sha"])
            self.save("reverting_main", recovery_commit=commit, recovery_parent=head)
        if head == self.state.get("recovery_parent"):
            self.github.advance(commit)  # force=false; races fail closed and retry.
        elif not self.github.contains(commit, head):
            raise ValueError("main changed while recovering; retry under the lock")
        self.save("awaiting_recovery_ci", main_sha=self.github.head())
        release = self.github.release(commit, recovery=old["sha"])
        if not release:
            return
        if not self.github.same_tree(commit, old["sha"]):
            raise ValueError("Recovery commit changed the known good tree")
        # Recovery is exactly the old source tree. Its schema is already compatible;
        # do not reverse migrations or reapply deleted migration files.
        self.operation = "recovery release"
        self.runtime.prepare(release)
        self.runtime.static(release)
        self.runtime.quiesce(workers)
        self.runtime.activate(release, workers)
        self.runtime.health(release, workers)
        self.save("healthy", previous=old, current=release, candidate=None, error=None,
                  running_sha=release["sha"])
