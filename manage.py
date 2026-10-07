#!/usr/bin/env python
import os
import sys


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        selected = None
        for index, argument in enumerate(sys.argv[2:], 2):
            if argument.startswith("--settings="):
                selected = argument.split("=", 1)[1]
            elif argument == "--settings" and index + 1 < len(sys.argv):
                selected = sys.argv[index + 1]
        if selected is not None and selected != "fishsite.settings_test":
            raise SystemExit("Tests require --settings=fishsite.settings_test")
        os.environ["DJANGO_SETTINGS_MODULE"] = "fishsite.settings_test"
    else:
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fishsite.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()

