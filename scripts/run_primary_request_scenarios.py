#!/usr/bin/env python3
"""Retired: test scenarios must never run against the primary database."""
import sys


def main():
    print(
        "Primary database scenarios are disabled. Use: "
        ".venv/bin/python manage.py test tests "
        "--settings=fishsite.settings_test --noinput",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
