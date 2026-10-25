"""Narrow GitHub API client: verified artifacts and App-authored forward reverts."""
import base64
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from deploy.migration_policy import check_changes, is_migration

API = "https://api.github.com"
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class APIError(RuntimeError):
    """Deliberately excludes response bodies, URLs and credentials."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(path, token, data=None, method=None, *, binary=False):
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("API path required")
    req = urllib.request.Request(API + path, method=method, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "fishsite-deployer",
        "Content-Type": "application/json",
    }, data=None if data is None else json.dumps(data).encode())
    try:
        response = urllib.request.build_opener(NoRedirect).open(req, timeout=30)
    except urllib.error.HTTPError as error:
        if binary and error.code == 302:
            location = error.headers["Location"]
            if urllib.parse.urlsplit(location).scheme != "https":
                raise APIError("Artifact redirect is not HTTPS") from None
            # Signed download URL: never forward the GitHub Authorization header.
            response = urllib.request.urlopen(location, timeout=30)
        else:
            raise APIError(f"GitHub HTTP {error.code}") from None
    with response:
        raw = response.read(2_000_001 if binary else 16_000_001)
    if len(raw) > (2_000_000 if binary else 16_000_000):
        raise APIError("GitHub response exceeds size limit")
    return raw if binary else json.loads(raw)


def app_token(config):
    def encode(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=")
    now = int(time.time())
    payload = encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()) + b"." + encode(
        json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(config["app_id"])}).encode())
    result = subprocess.run(["openssl", "dgst", "-sha256", "-sign", config["app_key"]],
                            input=payload, capture_output=True, check=True)
    jwt = (payload + b"." + encode(result.stdout)).decode()
    token = request(f'/app/installations/{int(config["installation_id"])}/access_tokens', jwt, {
        "repositories": [config["repository"].split("/")[1]],
        "permissions": {"contents": "write", "actions": "read", "statuses": "write",
                        "administration": "read", "workflows": "write"},
    })
    return token["token"]


def validate_release(value, config, run):
    expected = {
        "version": 1, "repository": config["repository"], "sha": run["head_sha"],
        "image": config["image"], "platform": config["platform"],
        "run_id": run["id"], "run_attempt": run["run_attempt"],
    }
    if any(value.get(k) != v for k, v in expected.items()) or set(value) != set(expected) | {"digest"}:
        raise ValueError("Release provenance does not match the successful workflow run")
    if not SHA.fullmatch(value["sha"]) or not DIGEST.fullmatch(value.get("digest", "")):
        raise ValueError("Malformed commit or image digest")
    return value


class GitHub:
    def __init__(self, config, token):
        self.config, self.token = config, token
        self.root = f'/repos/{config["repository"]}'

    def api(self, path, data=None, method=None, **kwargs):
        return request(self.root + path, self.token, data, method, **kwargs)

    def head(self):
        return self.api("/git/ref/heads/main")["object"]["sha"]

    def commit(self, sha):
        if not SHA.fullmatch(sha):
            raise ValueError("Full SHA required")
        return self.api(f"/git/commits/{sha}")

    def same_tree(self, left, right):
        return self.commit(left)["tree"]["sha"] == self.commit(right)["tree"]["sha"]

    def contains(self, ancestor, head):
        if ancestor == head:
            return True
        return self.api(f"/compare/{ancestor}...{head}")["status"] in {"ahead", "identical"}

    def release(self, sha):
        if not SHA.fullmatch(sha):
            raise ValueError("Full SHA required")
        runs = self.api(f"/actions/workflows/release.yml/runs?event=push&head_sha={sha}&per_page=100")["workflow_runs"]
        # A newer failed rerun invalidates an older successful attempt.
        runs = [r for r in runs if r["head_sha"] == sha and r["event"] == "push"
                and r["head_branch"] == "main" and r["head_repository"]["full_name"] == self.config["repository"]]
        if not runs:
            return None
        run = max(runs, key=lambda r: (r["id"], r["run_attempt"]))
        if run["status"] != "completed" or run["conclusion"] != "success":
            return None
        artifacts = self.api(f'/actions/runs/{run["id"]}/artifacts?per_page=100')["artifacts"]
        name = f'release-{sha}-{run["run_attempt"]}'
        artifacts = [a for a in artifacts if a["name"] == name and not a["expired"]]
        if len(artifacts) != 1 or artifacts[0]["size_in_bytes"] > 2_000_000:
            raise ValueError("Missing or ambiguous release manifest artifact")
        artifact = artifacts[0]
        raw = self.api(f'/actions/artifacts/{artifact["id"]}/zip', binary=True)
        if artifact.get("digest") != "sha256:" + hashlib.sha256(raw).hexdigest():
            raise ValueError("Artifact checksum mismatch")
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if archive.namelist() != ["release.json"] or archive.getinfo("release.json").file_size > 8192:
                raise ValueError("Unexpected release archive contents")
            manifest = json.loads(archive.read("release.json"))
        return validate_release(manifest, self.config, run)

    def read_file(self, sha, path):
        item = self.api(f'/contents/{urllib.parse.quote(path, safe="/")}?ref={sha}')
        if item.get("encoding") != "base64" or item.get("size", 0) > 200_000:
            raise ValueError("Unsupported migration file")
        return base64.b64decode(item["content"]).decode()

    def migrations(self, sha):
        tree = self.api(f"/git/trees/{sha}?recursive=1")
        if tree["truncated"]:
            raise ValueError("Cannot verify a truncated migration tree")
        return {item["path"]: hashlib.sha256(self.read_file(sha, item["path"]).encode()).hexdigest()
                for item in tree["tree"] if item["type"] == "blob" and is_migration(item["path"])}

    def compatible(self, base, head, ledger):
        comparison = self.api(f"/compare/{base}...{head}")
        files = comparison.get("files", [])
        if comparison["status"] != "ahead" or len(files) >= 300 or comparison["total_commits"] > 250:
            raise ValueError("Cannot completely verify release ancestry/diff")
        changes = []
        for item in files:
            if item["status"] == "renamed":
                changes.append(("removed", item["previous_filename"]))
                changes.append(("added", item["filename"]))
            else:
                changes.append((item["status"], item["filename"]))
        return check_changes(changes, lambda path: self.read_file(head, path), ledger)

    def create_revert(self, head, good):
        if not self.contains(good, head):
            raise ValueError("main is no longer descended from the known good release")
        tree = self.commit(good)["tree"]["sha"]
        return self.api("/git/commits", {
            "message": f"Revert failed deployment range through {head}\n\nFishSite-Restore: {good}",
            "tree": tree, "parents": [head],
        })["sha"]

    def advance(self, sha):
        return self.api("/git/refs/heads/main", {"sha": sha, "force": False}, "PATCH")

    def report(self, state):
        sha = state.get("main_sha") or state["current"]["sha"]
        healthy = state["phase"] == "healthy" and state["current"]["sha"] == sha
        description = f'{state["phase"]}; running {state["current"]["sha"][:12]}'
        self.api(f"/statuses/{sha}", {"state": "success" if healthy else "pending",
                                    "context": "production", "description": description})

    def rules_ready(self):
        rules = self.api("/rules/branches/main")
        by_type = {rule["type"]: rule for rule in rules}
        if not {"pull_request", "required_status_checks", "non_fast_forward", "deletion"} <= by_type.keys():
            raise ValueError("Required main protection is absent")
        review = by_type["pull_request"]["parameters"]
        checks = by_type["required_status_checks"]["parameters"]
        if (review["required_approving_review_count"] < 1 or not review["dismiss_stale_reviews_on_push"]
                or not review["require_last_push_approval"] or not checks["strict_required_status_checks_policy"]
                or not any(c["context"] == "CI required" for c in checks["required_status_checks"])):
            raise ValueError("main protection is weaker than the deployment contract")
        # Verify the one bypass is the dedicated rollback App, never a human/admin.
        rulesets = self.api("/rulesets?per_page=100")
        release_sets = [r for r in rulesets if r["name"] == "FishSite reviewed main"]
        if len(release_sets) != 1:
            raise ValueError("Managed main ruleset missing")
        detail = self.api(f'/rulesets/{release_sets[0]["id"]}')
        if detail["bypass_actors"] != [{"actor_id": self.config["app_id"], "actor_type": "Integration", "bypass_mode": "always"}]:
            raise ValueError("Unexpected main bypass actors")
