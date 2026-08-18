#!/usr/bin/env python
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "Django is not installed or the virtualenv is not active."
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
