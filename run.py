#!/usr/bin/env python3
"""
CricStar Discord Bot - Startup Script
"""
import os
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ADMIN_PANEL_DIR = os.path.join(BASE_DIR, "admin_panel")
MANAGE_PY = os.path.join(ADMIN_PANEL_DIR, "manage.py")

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "admin_panel.settings.cricstar")

DISCORD_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "") or os.environ.get("DISCORD_TOKEN", "")

env = {**os.environ, "DJANGO_SETTINGS_MODULE": "admin_panel.settings.cricstar"}

# Step 1: Run database migrations
print("=" * 50)
print("Running database migrations...")
print("=" * 50)
result = subprocess.run(
    [sys.executable, MANAGE_PY, "migrate", "--run-syncdb"],
    env=env,
    cwd=BASE_DIR,
)
if result.returncode != 0:
    print("Migration failed!")
    sys.exit(1)
print("Migrations complete.")

# Step 2: Initialize CricStar settings in the database
print("Initializing CricStar settings...")
init_script = f"""
import os, sys
sys.path.insert(0, r"{ADMIN_PANEL_DIR}")
sys.path.insert(0, r"{BASE_DIR}")
os.environ["DJANGO_SETTINGS_MODULE"] = "admin_panel.settings.cricstar"
import django
django.setup()
from settings.models import Settings

DISCORD_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "") or os.environ.get("DISCORD_TOKEN", "")
settings_obj, created = Settings.objects.get_or_create(pk=1)
changed = False
if created or not settings_obj.bot_token:
    settings_obj.bot_token = DISCORD_TOKEN
    settings_obj.bot_name = "CricStar"
    settings_obj.collectible_name = "cricketer"
    settings_obj.plural_collectible_name = "cricketers"
    settings_obj.cricstar_slash_name = "cricstar"
    settings_obj.catch_button_label = "Catch me!"
    changed = True
elif DISCORD_TOKEN and settings_obj.bot_token != DISCORD_TOKEN:
    settings_obj.bot_token = DISCORD_TOKEN
    changed = True
if changed:
    settings_obj.save()
    print("CricStar settings saved.")
print(f"Bot: {{settings_obj.bot_name}}")
print(f"Collectible: {{settings_obj.collectible_name}} / {{settings_obj.plural_collectible_name}}")
print(f"Slash command: /{{settings_obj.cricstar_slash_name}}")
"""
result = subprocess.run(
    [sys.executable, "-c", init_script],
    env=env,
    cwd=BASE_DIR,
)
if result.returncode != 0:
    print("Settings initialization failed!")
    sys.exit(1)

# Step 3: Start the bot
print("=" * 50)
print("Starting CricStar bot...")
print("=" * 50)
os.execv(sys.executable, [sys.executable, MANAGE_PY, "startbot"])
