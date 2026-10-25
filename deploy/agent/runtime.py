"""Docker adapter for the existing VPS: stable DB/media, per-release static."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import time
import urllib.request
import uuid

from .github import NoRedirect
from .state import atomic_json

WORKERS = {"worker", "moysklad-worker"}


def checksum(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class OperationFailed(RuntimeError):
    pass


class Runtime:
    def __init__(self, config):
        self.config = config
        self.root = Path(config["state_dir"])
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.environment = {key: os.environ[key] for key in ("PATH", "HOME", "DOCKER_CONFIG") if key in os.environ}
        self.environment.update(FISHSITE_HOST=config["hostname"])
        self.active = None

    def run(self, args, *, env=None, timeout=300, output=None):
        # Logs stay on the host, private. The journal gets operation names only.
        with (self.root / "operations.log").open("ab") as log:
            os.chmod(log.name, 0o600)
            result = subprocess.run(args, env=env or self.environment, stdout=output or subprocess.PIPE,
                                    stderr=log, timeout=timeout)
        if result.returncode:
            raise OperationFailed(f"{args[0]} exited {result.returncode}; see private operations.log")
        return result.stdout.decode().strip() if result.stdout is not None else ""

    def static_dir(self, release):
        return self.root / "releases" / release["sha"] / "static"

    def compose(self, release, *args, **kwargs):
        env = self.environment | {
            "FISHSITE_IMAGE": release["image"] + "@" + release["digest"],
            "FISHSITE_RELEASE_SHA": release["sha"],
            "FISHSITE_STATIC_DIR": str(self.static_dir(release)),
        }
        return self.run(["docker", "compose", "--project-name", "fishsite", "--env-file", self.config["images_env"],
                         "-f", self.config["compose_file"], "--profile", "notifications", "--profile", "moysklad",
                         "--profile", "migration", *args], env=env, **kwargs)

    def workers(self):
        raw = self.run(["docker", "ps", "--filter", "label=com.docker.compose.project=fishsite", "--format",
                        '{{.Label "com.docker.compose.service"}}'])
        return sorted(set(raw.splitlines()) & WORKERS)

    def prepare(self, release):
        reference = release["image"] + "@" + release["digest"]
        self.run(["docker", "pull", "--platform", self.config["platform"], reference], timeout=600)
        info = json.loads(self.run(["docker", "image", "inspect", reference]))[0]
        if (info["Config"].get("Labels", {}).get("org.opencontainers.image.revision") != release["sha"]
                or f'{info["Os"]}/{info["Architecture"]}' != self.config["platform"]
                or info["Config"]["User"] != "app" or reference not in info["RepoDigests"]):
            raise ValueError("Pulled image identity, architecture or user differs from the release")
        target = self.static_dir(release)
        target.mkdir(parents=True, exist_ok=True, mode=0o755)
        # No credentials/network/mounts are given to this unprivileged probe.
        uid = int(self.run(["docker", "run", "--rm", "--network", "none", reference, "id", "-u"]))
        gid = int(self.run(["docker", "run", "--rm", "--network", "none", reference, "id", "-g"]))
        if uid <= 0 or gid <= 0:
            raise ValueError("Application image must not run as root")
        os.chown(target, uid, gid)
        # The host agent's private umask is 077; Nginx uses a different UID and
        # needs to traverse this bind-mount root to serve public static files.
        target.chmod(0o755)
        self.compose(release, "config", "--quiet")

    def quiesce(self, workers):
        if not set(workers) <= WORKERS:
            raise ValueError("Unexpected worker service")
        # A SIGKILL of the host agent can leave a Docker run container alive.
        subprocess.run(["docker", "rm", "-f", "fishsite-deployment-job"], env=self.environment,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        # Stop ingress first, then drain writers. Names are resolved by Compose
        # labels so a crashed operation does not depend on the last env file.
        for services in (("nginx",), ("web", *workers)):
            for service in services:
                ids = self.run(["docker", "ps", "-q", "--filter", "label=com.docker.compose.project=fishsite",
                                "--filter", f"label=com.docker.compose.service={service}"]).splitlines()
                if ids:
                    self.run(["docker", "stop", "--time", "120", *ids], timeout=150)

    def backup(self, release):
        started = datetime.now(timezone.utc).isoformat()
        backup_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        folder = self.root / "backups" / backup_id
        folder.mkdir(parents=True, mode=0o700)
        ids = self.run(["docker", "ps", "-q", "--filter", "label=com.docker.compose.project=fishsite",
                        "--filter", "label=com.docker.compose.service=db"]).splitlines()
        if len(ids) != 1:
            raise ValueError("Expected exactly one running production database")
        database = ids[0]
        pg = ["docker", "exec", database]
        dump = folder / "database.dump"
        with dump.open("wb") as stream:
            self.run([*pg, "pg_dump", "--username=fishsite_owner", "--dbname=fishsite", "--format=custom"], output=stream)
        # Structural verification does not mutate any database.
        with dump.open("rb") as source:
            result = subprocess.run([*pg[:2], "-i", database, "pg_restore", "--list"], stdin=source,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        if result.returncode:
            raise ValueError("Backup dump is unreadable")
        migrations = self.run([*pg, "psql", "-XAt", "-U", "fishsite_owner", "-d", "fishsite", "-c",
                               "SELECT app || ':' || name FROM django_migrations ORDER BY app,name"])
        (folder / "migrations.txt").write_text(migrations + "\n")
        media = Path(self.config["media_dir"])
        if not media.is_dir() or any(p.is_symlink() for p in media.rglob("*")):
            raise ValueError("Media must be an existing directory without symlinks")
        media_hashes = {p.relative_to(media).as_posix(): checksum(p) for p in sorted(media.rglob("*")) if p.is_file()}
        with tarfile.open(folder / "media.tar.gz", "w:gz") as archive:
            archive.add(media, arcname="media")
        files = {p.name: {"bytes": p.stat().st_size, "sha256": checksum(p)} for p in folder.iterdir()}
        manifest = {"version": 1, "backup_id": backup_id, "started_at": started,
                    "finished_at": datetime.now(timezone.utc).isoformat(), "release": release,
                    "postgres_client": self.run([*pg, "pg_dump", "--version"]),
                    "postgres_server": self.run([*pg, "psql", "-XAt", "-U", "fishsite_owner", "-d", "fishsite", "-c", "SHOW server_version"]),
                    "files": files, "media": media_hashes}
        atomic_json(folder / "manifest.json", manifest)
        for name, expected in files.items():
            if checksum(folder / name) != expected["sha256"]:
                raise ValueError("Backup checksum changed")
        bundle = folder / "pair.tar"
        with tarfile.open(bundle, "w") as archive:
            for name in (*files, "manifest.json"):
                archive.add(folder / name, arcname=name)
        encrypted = folder / "pair.tar.age"
        self.run(["age", "-R", self.config["backup_recipient_file"], "-o", str(encrypted), str(bundle)])
        receipt = {"id": backup_id, "file": str(encrypted), "sha256": checksum(encrypted)}
        atomic_json(folder / "receipt.json", receipt)
        # A separately configured uploader must upload AND read back/verify.
        # No shell interpolation; failure aborts this release before migration.
        if self.config.get("backup_upload_command"):
            self.run([*self.config["backup_upload_command"], str(encrypted), receipt["sha256"]], timeout=600)
        # Keep the encrypted pair and checksums; do not accumulate plaintext dumps.
        for name in (*files, "pair.tar"):
            (folder / name).unlink()
        return receipt

    def operation(self, release, action):
        name = "fishsite-deployment-job"
        try:
            self.compose(release, "run", "--rm", "--no-deps", "-T", "--name", name,
                         "migration", "python", "-m", "deploy.container", action, timeout=600)
        finally:
            # Also after timeout: a migration container must never outlive recovery.
            subprocess.run(["docker", "rm", "-f", name], env=self.environment,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)

    def migrate(self, release):
        self.operation(release, "migrate")

    def static(self, release):
        self.operation(release, "static")

    def activate(self, release, workers):
        self.compose(release, "up", "-d", "--no-deps", "--force-recreate", "--wait", "--wait-timeout", "120", "web", *workers)
        # Recreate Nginx after web: resolve its current IP and remount static/config.
        self.compose(release, "up", "-d", "--no-deps", "--force-recreate", "nginx")
        self.compose(release, "exec", "-T", "nginx", "nginx", "-t")

    def response(self, path):
        url = "https://" + self.config["hostname"] + path
        with urllib.request.build_opener(NoRedirect).open(
            urllib.request.Request(url, headers={"Cache-Control": "no-cache", "User-Agent": "fishsite-release-check"}), timeout=15
        ) as response:
            if response.status != 200:
                raise ValueError("Release probe did not return exact HTTP 200")
            return response.read(4_000_000)

    def health(self, release, workers):
        reference = release["image"] + "@" + release["digest"]
        for service in ("web", *workers, "nginx", "db"):
            ids = self.compose(release, "ps", "-q", service).splitlines()
            if len(ids) != 1:
                raise ValueError(f"Missing running service: {service}")
            info = json.loads(self.run(["docker", "inspect", ids[0]]))[0]
            if not info["State"]["Running"] or info["State"].get("Health", {}).get("Status", "healthy") != "healthy":
                raise ValueError(f"Service is unhealthy: {service}")
            if service in {"web", *workers} and info["Config"]["Image"] != reference:
                raise ValueError("Service is still using a different image")
        # A short soak catches workers which exit immediately after startup.
        time.sleep(2)
        for service in workers:
            ids = self.compose(release, "ps", "-q", service).splitlines()
            if len(ids) != 1:
                raise ValueError("Worker exited during release checks")
            state = json.loads(self.run(["docker", "inspect", ids[0]]))[0]
            if state["RestartCount"] or not state["State"]["Running"]:
                raise ValueError("Worker restarted during release checks")
        ready = json.loads(self.response("/healthz/"))
        if ready != {"status": "ok", "sha": release["sha"]}:
            raise ValueError("HTTPS is serving a different release or unhealthy database")
        for path in ("/", self.config["catalog_path"]):
            if b"</html>" not in self.response(path).lower():
                raise ValueError("Page smoke check did not return HTML")
        static = json.loads(self.response("/static/release.json"))
        local = json.loads((self.static_dir(release) / "release.json").read_text())
        if static != local or static["sha"] != release["sha"]:
            raise ValueError("Static files are from a different release")
        if hashlib.sha256(self.response("/static/" + static["asset"])).hexdigest() != static["sha256"]:
            raise ValueError("Served stylesheet checksum differs from the collected file")
