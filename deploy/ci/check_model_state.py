"""Run makemigrations --check inside the guarded disposable PostgreSQL DB."""
import os

os.environ["DJANGO_SETTINGS_MODULE"] = "fishsite.settings_test"
import django
django.setup()
from django.core.management import call_command
from fishsite.test_runner import IsolatedTestRunner


def main():
    # The maintenance DB 'postgres' is deliberately forbidden to application ORM.
    # Let the existing guarded runner create/drop test_fishsite, just as tests do.
    runner = IsolatedTestRunner(interactive=False)
    previous = runner.setup_databases()
    try:
        call_command("makemigrations", check=True, dry_run=True)
    finally:
        runner.teardown_databases(previous)


if __name__ == "__main__":
    main()
