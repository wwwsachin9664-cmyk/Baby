#!/usr/bin/env python3
"""
CricStar Discord Bot - Startup Script
"""
import os
import signal
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ADMIN_PANEL_DIR = os.path.join(BASE_DIR, "admin_panel")
MANAGE_PY = os.path.join(ADMIN_PANEL_DIR, "manage.py")

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "admin_panel.settings.cricstar")

DISCORD_TOKEN = (os.environ.get("DISCORD_BOT_TOKEN") or os.environ.get("DISCORD_TOKEN") or "").strip()

# ---------------------------------------------------------------------------
# PID lock — ensure only one bot process is alive at a time
# ---------------------------------------------------------------------------
# When Replit restarts or remixes the project, the old bot process may keep
# running for a while alongside the new one.  Both processes are connected to
# Discord and receive every message, which causes double card spawns.
#
# We write the current process PID to a lock file at startup.  If a previous
# PID file exists, we forcibly terminate that process before continuing.
# Because run.py ends with os.execv (replacing itself with manage.py startbot,
# same PID), the lock file correctly tracks the live bot process.
# ---------------------------------------------------------------------------

PID_FILE = os.path.join(BASE_DIR, ".bot.pid")


def _find_all_bot_pids() -> list:
    """
    Find ALL running manage.py startbot processes that are NOT the current process.
    This catches ghost processes even when the PID file is stale or missing.
    """
    my_pid = os.getpid()
    found = []
    try:
        result = subprocess.run(
            ["ps", "aux"],
            capture_output=True, text=True
        )
        for line in result.stdout.splitlines():
            if "manage.py" in line and "startbot" in line and "grep" not in line:
                parts = line.split()
                try:
                    pid = int(parts[1])
                    if pid != my_pid:
                        found.append(pid)
                except (IndexError, ValueError):
                    pass
    except Exception:
        pass
    return found


def _kill_pid(pid: int) -> None:
    """Send SIGTERM then SIGKILL to a single PID."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return  # Already dead

    print(f"[PID lock] Killing old bot process (PID {pid}) with SIGTERM...")
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return

    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        time.sleep(0.5)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            print(f"[PID lock] PID {pid} exited cleanly.")
            return

    print(f"[PID lock] PID {pid} still alive — sending SIGKILL...")
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    time.sleep(1)
    print(f"[PID lock] PID {pid} force-killed.")


def _kill_old_process() -> None:
    """
    Kill ALL other bot processes — both from the PID file AND by scanning
    all processes for manage.py startbot.  This ensures ghost processes
    left over from previous restarts are always eliminated, even when the
    PID file is stale, missing, or was never written.
    """
    my_pid = os.getpid()
    pids_to_kill = set()

    # 1. PID file approach (fast path)
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE) as f:
                old_pid = int(f.read().strip())
            if old_pid != my_pid:
                pids_to_kill.add(old_pid)
        except (ValueError, OSError):
            pass

    # 2. Full process scan (catches ghost processes the PID file misses)
    for pid in _find_all_bot_pids():
        pids_to_kill.add(pid)

    if not pids_to_kill:
        print("[PID lock] No old bot processes found.")
        return

    for pid in pids_to_kill:
        _kill_pid(pid)


def _write_pid() -> None:
    """Write the current PID to the lock file."""
    try:
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
    except OSError as exc:
        print(f"[PID lock] Warning: could not write PID file: {exc}")


_kill_old_process()
_write_pid()

# ---------------------------------------------------------------------------

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

# Step 1b: Clear stale trade locks left over from the previous bot session.
# Trade state is stored only in memory (the Trade cog's dict).  When the bot
# restarts or crashes, that state is lost but BallInstance.locked timestamps
# remain set in the DB.  Any locked card with no active trade will silently
# block the user from adding it to a new trade.  Clearing all locks here is
# safe because there are no active trades at startup.
print("Clearing stale trade locks from previous session...")
unlock_script = _PYTHON_HEADER + """
from bd_models.models import BallInstance
count = BallInstance.objects.filter(locked__isnull=False).update(locked=None)
print(f"Cleared {count} stale trade lock(s).")
"""
result = subprocess.run([sys.executable, "-c", unlock_script], env=env, cwd=BASE_DIR)
if result.returncode != 0:
    print("Lock cleanup failed (non-critical, continuing).")

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

# Step 8: Open health-check port if running inside Replit deployment.
# The deployment system requires the artifact's declared port to be open
# before it considers the process healthy.  The Discord bot itself is not
# a web server, so we spin up a minimal HTTP responder in a background
# subprocess.  os.execv (below) replaces this process, but the subprocess
# keeps running independently.
_deploy_port = int(os.environ.get("PORT", 0))
if _deploy_port:
    _health_code = (
        "import socket\n"
        "s = socket.socket()\n"
        "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        f"s.bind(('0.0.0.0', {_deploy_port}))\n"
        "s.listen(10)\n"
        "while True:\n"
        "    try:\n"
        "        c, _ = s.accept()\n"
        "        c.send(b'HTTP/1.1 200 OK\\r\\nContent-Length: 2\\r\\n\\r\\nOK')\n"
        "        c.close()\n"
        "    except Exception:\n"
        "        pass\n"
    )
    subprocess.Popen(
        [sys.executable, "-c", _health_code],
        env=env,
        cwd=BASE_DIR,
    )
    print(f"Health check server started on port {_deploy_port}.")

# Step 9: Start the bot
# NOTE: os.execv replaces this process image with manage.py startbot,
# keeping the SAME PID — so the PID lock file written above remains valid
# and correctly tracks the live bot process.
print("=" * 50)
print("Starting CricStar bot...")
print("=" * 50)
os.execv(sys.executable, [sys.executable, MANAGE_PY, "startbot"])
