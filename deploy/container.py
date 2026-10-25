"""One-shot container operations, always under the schema-owner credentials."""
import hashlib
import json
import inspect
import os
from pathlib import Path
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fishsite.settings")
import django
django.setup()
from django.conf import settings
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from deploy.migration_policy import validate_added


def main():
    action = sys.argv[1]
    if action == "migrate":
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_user, rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = current_user")
            if cursor.fetchone() != ("fishsite_owner", False, False, False):
                raise RuntimeError("Migrations require the restricted schema-owner role")
            cursor.execute("SET lock_timeout = '10s'")
            cursor.execute("SET statement_timeout = '180s'")
        # Inspect the actual pending plan too, including migrations shipped by
        # upgraded dependencies. A repository diff cannot see those migrations.
        executor = MigrationExecutor(connection)
        for migration, backwards in executor.migration_plan(executor.loader.graph.leaf_nodes()):
            if backwards:
                raise RuntimeError("Automatic backwards migrations are forbidden")
            source = inspect.getsourcefile(type(migration))
            if not source:
                raise RuntimeError("Cannot verify pending migration source")
            validate_added(Path(source).read_text())
        call_command("migrate", interactive=False)
    elif action == "static":
        call_command("collectstatic", interactive=False, clear=True)
        root = Path(settings.STATIC_ROOT)
        assets = sorted(root.glob("css/*.css"))
        if not assets:
            raise RuntimeError("No stylesheet collected")
        asset = assets[0]
        (root / "release.json").write_text(json.dumps({
            "sha": os.environ["FISHSITE_RELEASE_SHA"],
            "asset": asset.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
        }) + "\n")
    else:
        raise ValueError("Unknown operation")


if __name__ == "__main__":
    main()
