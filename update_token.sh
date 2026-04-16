#!/bin/bash
# update_token.sh — Replace the Discord bot token easily

echo "======================================="
echo "   CricStar Bot — Token Updater"
echo "======================================="
echo ""
echo "Paste your new Discord bot token below."
echo "(Input is hidden for security)"
echo ""
read -rs -p "New token: " NEW_TOKEN
echo ""

if [ -z "$NEW_TOKEN" ]; then
    echo "No token entered. Aborted."
    exit 1
fi

echo ""
echo "Saving token to database..."

python3 - <<EOF
import os, sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "admin_panel.settings.cricstar")

sys.path.insert(0, "$(pwd)/admin_panel")
sys.path.insert(0, "$(pwd)")

import django
django.setup()

from settings.models import Settings

obj, _ = Settings.objects.get_or_create(pk=1)
obj.bot_token = "$NEW_TOKEN"
obj.save()
print("Token saved to database successfully.")
EOF

if [ $? -ne 0 ]; then
    echo ""
    echo "Failed to save token to database."
    exit 1
fi

echo ""
echo "======================================="
echo " Done! Restart the bot to apply it."
echo " Run: kill \$(pgrep -f 'manage.py startbot') && python3 run.py"
echo "======================================="
