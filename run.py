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

DISCORD_TOKEN = (os.environ.get("DISCORD_BOT_TOKEN") or os.environ.get("DISCORD_TOKEN") or "").strip()

env = {**os.environ, "DJANGO_SETTINGS_MODULE": "admin_panel.settings.cricstar"}

_PYTHON_HEADER = f"""
import os, sys
sys.path.insert(0, r"{ADMIN_PANEL_DIR}")
sys.path.insert(0, r"{BASE_DIR}")
os.environ["DJANGO_SETTINGS_MODULE"] = "admin_panel.settings.cricstar"
import django
django.setup()
"""

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
init_script = _PYTHON_HEADER + f"""
from settings.models import Settings

DISCORD_TOKEN = (os.environ.get("DISCORD_BOT_TOKEN") or os.environ.get("DISCORD_TOKEN") or "").strip()
settings_obj, created = Settings.objects.get_or_create(pk=1)
changed = False
if DISCORD_TOKEN and settings_obj.bot_token != DISCORD_TOKEN:
    settings_obj.bot_token = DISCORD_TOKEN
    changed = True
if created:
    settings_obj.bot_name = "CricStar"
    settings_obj.collectible_name = "cricketer"
    settings_obj.plural_collectible_name = "cricketers"
    settings_obj.cricstar_slash_name = "cricstar"
    settings_obj.catch_button_label = "Catch me!"
    changed = True
if not settings_obj.bot_token:
    raise RuntimeError("DISCORD_BOT_TOKEN or DISCORD_TOKEN must be set before starting the bot.")
if changed:
    settings_obj.save()
    print("CricStar settings saved.")
print(f"Bot: {{settings_obj.bot_name}}")
print(f"Collectible: {{settings_obj.collectible_name}} / {{settings_obj.plural_collectible_name}}")
print(f"Slash command: /{{settings_obj.cricstar_slash_name}}")
"""
result = subprocess.run([sys.executable, "-c", init_script], env=env, cwd=BASE_DIR)
if result.returncode != 0:
    print("Settings initialization failed!")
    sys.exit(1)

# Step 3: Seed required special events (must run BEFORE card restore so events
#         exist when cards reference them by name).
print("Setting up special events...")
result = subprocess.run([sys.executable, MANAGE_PY, "setup_specials"], env=env, cwd=BASE_DIR)
if result.returncode != 0:
    print("setup_specials failed (non-critical, continuing).")

# Step 4: Restore any custom events created via /createevent
print("=" * 50)
print("Restoring custom events from card_exports/ (if any)...")
print("=" * 50)
event_restore_script = _PYTHON_HEADER + """
from cricstar.card_sync import import_all_events
count = import_all_events()
if count:
    print(f"Restored {count} custom event(s) from card_exports/events.json.")
else:
    print("No new events to restore.")
"""
result = subprocess.run([sys.executable, "-c", event_restore_script], env=env, cwd=BASE_DIR)
if result.returncode != 0:
    print("Event restore step failed (non-critical, continuing).")

# Step 5: Restore cards (runs after events so Special FKs resolve by name)
print("=" * 50)
print("Restoring cards from card_exports/ (if any)...")
print("=" * 50)
card_restore_script = _PYTHON_HEADER + """
from cricstar.card_sync import import_all_cards
count = import_all_cards()
if count:
    print(f"Restored {count} card(s) from card_exports/.")
else:
    print("No new cards to restore.")
"""
result = subprocess.run([sys.executable, "-c", card_restore_script], env=env, cwd=BASE_DIR)
if result.returncode != 0:
    print("Card restore step failed (non-critical, continuing).")

# Step 6: Restore user holdings (only if Player table is empty = fresh remix)
print("=" * 50)
print("Restoring user holdings from card_exports/ (if any)...")
print("=" * 50)
holdings_restore_script = _PYTHON_HEADER + """
from cricstar.card_sync import import_all_holdings
count = import_all_holdings()
if count:
    print(f"Restored {count} user holding(s) from card_exports/holdings.json.")
else:
    print("No holdings to restore (DB already populated or no export found).")
"""
result = subprocess.run([sys.executable, "-c", holdings_restore_script], env=env, cwd=BASE_DIR)
if result.returncode != 0:
    print("Holdings restore step failed (non-critical, continuing).")

# Step 7: Export current holdings so this checkpoint captures the latest state
print("Exporting current user holdings to card_exports/holdings.json...")
holdings_export_script = _PYTHON_HEADER + """
from cricstar.card_sync import export_all_holdings
count = export_all_holdings()
print(f"Exported {count} holding(s) to card_exports/holdings.json.")
"""
result = subprocess.run([sys.executable, "-c", holdings_export_script], env=env, cwd=BASE_DIR)
if result.returncode != 0:
    print("Holdings export step failed (non-critical, continuing).")

# Step 7b: Restore guild configs so servers don't need to re-configure after a remix
print("Restoring server configurations from card_exports/guild_configs.json...")
guild_config_import_script = _PYTHON_HEADER + """
from cricstar.card_sync import import_all_guild_configs
count = import_all_guild_configs()
if count:
    print(f"Restored {count} server configuration(s) from guild_configs.json.")
else:
    print("No new server configurations to restore (already up to date).")
"""
result = subprocess.run([sys.executable, "-c", guild_config_import_script], env=env, cwd=BASE_DIR)
if result.returncode != 0:
    print("Guild config restore step failed (non-critical, continuing).")

# Step 7c: Export current guild configs to keep the file up to date
print("Exporting current server configurations to card_exports/guild_configs.json...")
guild_config_export_script = _PYTHON_HEADER + """
from cricstar.card_sync import export_all_guild_configs
count = export_all_guild_configs()
print(f"Exported {count} server configuration(s) to guild_configs.json.")
"""
result = subprocess.run([sys.executable, "-c", guild_config_export_script], env=env, cwd=BASE_DIR)
if result.returncode != 0:
    print("Guild config export step failed (non-critical, continuing).")

# Step 8: Start the bot
print("=" * 50)
print("Starting CricStar bot...")
print("=" * 50)
os.execv(sys.executable, [sys.executable, MANAGE_PY, "startbot"])
