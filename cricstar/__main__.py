import os
import sys

from django.core.management import execute_from_command_line


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "admin_panel.settings.cricstar")
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "admin_panel"))
    sys.argv.insert(1, "startbot")
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
