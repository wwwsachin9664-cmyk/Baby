#!/bin/bash
# Run this BEFORE pushing to GitHub.
# It dumps the live database into db_backup.sql so all user data is included.

set -e

echo "==> Dumping database..."
pg_dump $DATABASE_URL --no-owner --no-acl -F p -f db_backup.sql
echo "    Done. $(wc -l < db_backup.sql) lines written to db_backup.sql"

echo "==> Staging all changes..."
git add -A

echo ""
read -p "Commit message (press Enter for default): " MSG
MSG="${MSG:-chore: backup $(date '+%Y-%m-%d %H:%M')}"

git commit -m "$MSG" || echo "(nothing new to commit)"

echo ""
read -p "Push to GitHub now? (y/n): " PUSH
if [[ "$PUSH" == "y" || "$PUSH" == "Y" ]]; then
    git push
    echo "==> Pushed successfully."
else
    echo "==> Skipped push. Run 'git push' when ready."
fi
