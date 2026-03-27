#!/bin/bash
cd /home/runner/workspace/admin_panel
export PYTHONPATH=/home/runner/workspace
export DJANGO_SETTINGS_MODULE=admin_panel.settings.cricstar
exec python3 manage.py runserver 0.0.0.0:8000
