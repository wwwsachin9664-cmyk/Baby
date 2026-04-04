# CricStar Discord Bot — AI Context File

Paste this entire file into another AI chat so they understand your bot instantly.

---

## What is this bot?

**CricStar** is a Discord collectible card game bot (like PokéBall but for cricket).
Players catch, collect, trade, and upgrade cricket player cards.
Built with **discord.py 2.x**, **Django ORM**, and **PostgreSQL**.

Bot name: **CricStar#1541**
Entry point: `python run.py`
Django settings: `admin_panel.settings.cricstar`

---

## Tech Stack

- **Python 3.12** + discord.py 2.7.1
- **Django ORM** (for DB models — no Django web server running, just the ORM)
- **PostgreSQL** (via `DATABASE_URL` env var)
- **Pillow** (PIL) — generates card images
- **Nulshock Bold** font — used for all card text

---

## Folder Structure

```
cricstar/
  core/
    bot.py                        # Main bot class, loads packages
    image_generator/
      image_gen.py                # ALL card image generation (draw_card, draw_premade_card)
      src/                        # Font files (.otf/.ttf)
    utils/
      checks.py                   # is_superuser(), ADMIN_ROLE_ID
  packages/
    admin/cog.py                  # /cardmaker, /editcard, /setspawnimg commands
    upgrade/cog.py                # /cricket upgrade command
    cricketers/
      cog.py                      # spawn logic, /cricketers commands
      countryball.py              # get_random() — picks a random spawnable ball
    collection/cog.py             # /collection command
    players/cog.py                # viewing caught cards
    trade/cog.py                  # trading
    daily/cog.py                  # daily rewards
admin_panel/
  bd_models/
    models.py                     # Ball, BallInstance, Regime, etc.
    migrations/                   # Django migrations
  media/
    backgrounds/                  # Background images (e.g. base_background.jpg)
    foregrounds/                  # Foreground presets (named by player slug, no extension)
    premade_*.png                 # Generated card images stored here
```

---

## Key Database Models (admin_panel/bd_models/models.py)

### Ball (the card template / cricketer definition)
```python
ball.country          # Display name shown on card header (also used for DB lookup)
ball.health           # Bat score (shown on card as left stat)
ball.attack           # Ball/bowl score (shown on card as right stat)
ball.rarity           # Spawn probability (0.0–1.0 float; multiply by 100 for display %)
ball.wild_card        # ImageFieldFile — card image used when spawning (filename: premade_*.png)
ball.collection_card  # ImageFieldFile — card image shown in collection (same as wild_card for premade cards)
ball.capacity_name    # "Codename" shown on the card (e.g. "KING")
ball.capacity_description  # Description text on the card
ball.credits          # Artwork author name
ball.enabled          # If False, hidden from all lists
ball.spawnable        # If False, never spawns randomly (but still in collection)
ball.tradeable        # Whether the card can be traded
ball.emoji_id         # Discord emoji ID for this card
ball.regime           # FK to Regime (country/team grouping)
```

### BallInstance (a specific caught card owned by a user)
```python
instance.player       # FK to Player (Discord user)
instance.ball         # FK to Ball
instance.ball_id      # Ball primary key
instance.cricketer    # Property — returns Ball from in-memory cache (balls dict)
instance.draw_card()  # Returns BytesIO of the rendered card image
```

### In-memory cache
```python
from bd_models.models import balls  # dict[int, Ball] — loaded at bot startup
# Key = ball.id, Value = Ball object
# MUST update after any DB change: balls[ball.id] = ball
```

---

## Card Image Generation (image_gen.py)

### draw_premade_card() — creates new card PNG files
Called by `/cardmaker` and `/editcard` to generate the 1500×2000px card image.

Parameters:
```python
draw_premade_card(
    background_path,   # Path to background image
    foreground_path,   # Path to foreground (player action photo)
    player_name,       # Name shown on card header (top-left, Nulshock 120px)
    codename,          # "CODENAME: X" shown below frame
    description,       # Description text (Nulshock Bold 40px, below codename)
    rarity,            # Badge value shown top-right (e.g. 0.1)
    bat_score,         # Left stat number
    ball_score,        # Right stat number
    artwork_author,    # "Artwork: X" credit line
    logo_path=None,    # Optional logo shown in info panel top-right
)
```

Card layout (1500×2000):
```
┌────────────────────────────────────────┐
│  PLAYER NAME (left, gold)  0.1 (right) │  ← 160px, no dark overlay
│ ┌──────────────────────────────────┐   │
│ │  landscape player image frame    │   │  ← 720px, gold border
│ └──────────────────────────────────┘   │
│  Gold separator line                   │
│  CODENAME: X  (gold, Nulshock 76px)    │
│                                        │
│  [Description text, Nulshock 40px]     │  ← starts at codename_y + 170
│                                        │
│  ██████████████████████████████████   │  ← dark bottom bar (120px)
│  🏏 300        🔴 300                  │
│  Created by El Laggron  Artwork: X     │
└────────────────────────────────────────┘
```

No dark overlay in the background area — background image shows through everywhere.
All text has black stroke outlines for readability.

### draw_card() — renders a BallInstance card for display
- If `ball.collection_card` filename starts with `"premade_"`, opens the PNG directly from disk
- Otherwise, generates full card from scratch with regime background

---

## Admin Commands (superuser only)

### /cardmaker
Creates a new cricketer card and adds it to the DB.
```
/cardmaker player_name:"Virat(T20)" display_name:"Virat T20" codename:"KING"
           description:"..." bat_score:300 ball_score:300 rarity:0.1
           spawn_chance:20 artwork_author:"Sachin" background:base_background
           foreground:[URL] logo_url:[URL] event:none tradeable:True spawnable:True
```
- `player_name` = internal DB identifier (used for slug/file naming)
- `display_name` = what shows on card header (optional; falls back to player_name)
- `ball.country` = stores display_name (or player_name if no display_name)
- File saved as `admin_panel/media/premade_{slug}.png`
- Foreground saved as preset at `admin_panel/media/foregrounds/{slug}` for reuse

### /editcard
Edits an existing card. Only supply fields you want to change.
```
/editcard player_name:"Virat T20" display_name:"New Name" bat_score:400 description:"..."
```
- Always regenerates the card image if it's already a premade card
- Updates `ball.country` if display_name is provided
- Updates `balls[ball.id]` in-memory cache after save

### /setspawnimg
Sets a custom spawn image (wild_card) for a player without regenerating the full premade card.

---

## /cricket upgrade Command (upgrade/cog.py)

- **Open to ALL users** (no permission check)
- **Global 15-hour cooldown** — one upgrade per bot-wide cooldown window
- Picks any cricketer via autocomplete
- Randomly increases bat and ball scores by 0–5 each
- Regenerates premade card if one exists
- Output: plain message like `#118 Virat T20 increased its Bat by +3! Bowl by +2!`
- Cooldown stored in memory (`_last_upgrade_time`) — resets on bot restart

---

## Spawn System (countryball.py)

```python
# get_random() filters:
# - ball.enabled == True
# - ball.spawnable == True
# Returns a random Ball weighted by ball.rarity
```

---

## Important Constants / IDs

```python
ADMIN_ROLE_ID = 1476180448446124055   # Admin role (used in old checks, kept for reference)
BOT_ID = 1439125179073691729
OWNER = "papa_320"
UPGRADE_COOLDOWN_HOURS = 15
```

---

## File Naming Conventions

```
admin_panel/media/premade_{slug}.png      # Full card image (wild_card + collection_card)
admin_panel/media/backgrounds/{name}.jpg  # Preset backgrounds
admin_panel/media/foregrounds/{slug}      # Preset foregrounds (no extension)
admin_panel/media/event_config.json       # Event config (active/inactive)
```

Slug formula: `re.sub(r"[^a-z0-9]+", "_", player_name.lower().strip()).strip("_")`

---

## Common Gotchas

1. **Always update `balls[ball.id] = ball` after saving** — the in-memory cache is what all commands read.
2. `ball.health` = bat score, `ball.attack` = ball/bowl score (confusing names from original code).
3. Premade card images are opened via direct filesystem path (`MEDIA_DIR / filename`), not Django storage.
4. `spawnable=False` prevents random spawning but card still appears in collections.
5. `enabled=False` hides the card from all collection lists.
6. The bot is restarted by running `python run.py` — migrations run automatically on startup.
7. Global upgrade cooldown resets when bot restarts (in-memory only).
