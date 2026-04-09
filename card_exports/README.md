# Card Exports

This folder is auto-managed by the CricStar bot.

Every time a card is created with `/cardmaker`, its full data is saved here:
- `cards.json` — all card details (stats, names, descriptions, etc.)
- `images/` — the generated card PNG files
- `foregrounds/` — the player foreground images

**When this project is remixed/forked to another Replit account:**
The bot startup script automatically reads this folder and restores all cards
into the new database — no manual work needed.
