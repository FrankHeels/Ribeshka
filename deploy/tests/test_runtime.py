import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from deploy.agent.runtime import Runtime


class StaticPermissionsTests(unittest.TestCase):
    def test_private_agent_umask_does_not_make_public_static_unreadable_by_nginx(self):
        release = {"image": "ghcr.io/frankheels/ribeshka", "digest": "sha256:" + "a" * 64, "sha": "b" * 40}
        reference = release["image"] + "@" + release["digest"]
        info = {"Config": {"User": "app", "Labels": {"org.opencontainers.image.revision": release["sha"]}},
                "Os": "linux", "Architecture": "amd64", "RepoDigests": [reference]}
        with tempfile.TemporaryDirectory() as folder:
            runtime = Runtime({"state_dir": str(Path(folder) / "state"), "hostname": "example.com", "platform": "linux/amd64"})
            old_umask = os.umask(0o077)
            try:
                with patch.object(runtime, "run", side_effect=["", json.dumps([info]), "999", "999"]), \
                        patch.object(runtime, "compose"), patch("deploy.agent.runtime.os.chown"):
                    runtime.prepare(release)
            finally:
                os.umask(old_umask)
            self.assertEqual(stat.S_IMODE(runtime.static_dir(release).stat().st_mode), 0o755)
