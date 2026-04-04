import json
import os
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image, ImageDraw, ImageFont, ImageOps

from settings.models import settings

if TYPE_CHECKING:
    from bd_models.models import BallInstance


SOURCES_PATH = Path(os.path.dirname(os.path.abspath(__file__)), "./src")
MEDIA_DIR = Path("./admin_panel/media")

# ===== EVENT CONFIG =====
#
# File location: admin_panel/media/event_config.json
#
# Event START karne ke liye:
# {
#   "active": true,
#   "event_background": "ramadan_bg.png",
#   "foreground_overlay": "ramadan_overlay.png"
# }
#
# Event BAND karne ke liye:
# {
#   "active": false,
#   "event_background": "",
#   "foreground_overlay": ""
# }
#
EVENT_CONFIG_PATH = MEDIA_DIR / "event_config.json"

WIDTH = 1500
HEIGHT = 2000

RECTANGLE_WIDTH = WIDTH - 40
RECTANGLE_HEIGHT = (HEIGHT // 5) * 2

CORNERS = ((34, 261), (1393, 992))
artwork_size = [b - a for a, b in zip(*CORNERS)]

# ===== TIP =====
#
# If you want to quickly test the image generation, there is a CLI tool to quickly generate
# test images locally, without the bot or the admin panel running:
#
# With Docker: "docker compose run admin-panel django-admin preview > image.png"
# Without: "DJANGO_SETTINGS_MODULE=admin_panel.settings python3 -m django preview"
#
# This will either create a file named "image.png" or directly display it using your system's
# image viewer. There are options available to specify the ball or the special background,
# use the "--help" flag to view all options.

title_font = ImageFont.truetype(str(SOURCES_PATH / "ArsenicaTrial-Extrabold.ttf"), 170)
capacity_name_font = ImageFont.truetype(str(SOURCES_PATH / "Bobby Jones Soft.otf"), 110)
capacity_description_font = ImageFont.truetype(str(SOURCES_PATH / "OpenSans-Semibold.ttf"), 75)
stats_font = ImageFont.truetype(str(SOURCES_PATH / "Bobby Jones Soft.otf"), 130)
credits_font = ImageFont.truetype(str(SOURCES_PATH / "arial.ttf"), 40)

try:
    nulshock_title_font = ImageFont.truetype(str(SOURCES_PATH / "Nulshock-Bold.otf"), 150)
    nulshock_codename_font = ImageFont.truetype(str(SOURCES_PATH / "Nulshock-Bold.otf"), 76)
    nulshock_rarity_font = ImageFont.truetype(str(SOURCES_PATH / "Nulshock-Bold.otf"), 58)
    nulshock_stats_font = ImageFont.truetype(str(SOURCES_PATH / "Nulshock-Bold.otf"), 130)
except Exception:
    nulshock_title_font = title_font
    nulshock_codename_font = capacity_name_font
    nulshock_rarity_font = stats_font
    nulshock_stats_font = stats_font

credits_color_cache = {}


def load_event_config() -> dict:
    """Event config load karta hai. Agar active nahi hai toh empty dict return karta hai."""
    try:
        if EVENT_CONFIG_PATH.exists():
            data = json.loads(EVENT_CONFIG_PATH.read_text())
            if data.get("active", False):
                return data
    except Exception:
        pass
    return {}


def get_credit_color(image: Image.Image, region: tuple) -> tuple:
    image = image.crop(region)
    brightness = sum(image.convert("L").getdata()) / image.width / image.height  # type: ignore
    return (0, 0, 0, 255) if brightness > 100 else (255, 255, 255, 255)


def draw_card(ball_instance: "BallInstance") -> tuple[Image.Image, dict[str, Any]]:
    ball = ball_instance.cricketer

    # Load event config
    event = load_event_config()

    # Pre-made card bypass: if collection_card filename starts with "premade_",
    # just resize it to card dimensions and return it directly — no text overlay.
    # BUT agar event active hai aur foreground_overlay hai toh overlay lagao.
    card_name_field = str(ball.collection_card.name) if ball.collection_card else ""
    if card_name_field.startswith("premade_"):
        premade = Image.open(ball.collection_card).convert("RGBA")
        # Scale up to fill the full card size (1500×2000), cropping if needed
        card = ImageOps.fit(premade, (WIDTH, HEIGHT), Image.LANCZOS)
        premade.close()

        # ── Event overlay premade cards pe bhi lagao ──────────────────────────
        if event.get("foreground_overlay"):
            overlay_path = MEDIA_DIR / event["foreground_overlay"]
            if overlay_path.exists():
                overlay = Image.open(overlay_path).convert("RGBA")
                if overlay.size != (WIDTH, HEIGHT):
                    overlay = overlay.resize((WIDTH, HEIGHT), Image.LANCZOS)
                card.paste(overlay, (0, 0), mask=overlay)
                overlay.close()

        return card, {"format": "WEBP"}

    ball_health = (237, 115, 101, 255)
    ball_credits = ball.credits
    special_credits = ""
    card_name = ball.cached_regime.name

    # ── Background choose karo ────────────────────────────────────────────────
    # Priority: Special card > Event background > Normal regime
    if special_image := ball_instance.special_card:
        card_name = getattr(ball_instance.specialcard, "name", card_name)
        image = Image.open(special_image)
        if ball_instance.specialcard and ball_instance.specialcard.credits:
            special_credits += f" • Special Author: {ball_instance.specialcard.credits}"

    elif event.get("event_background"):
        # Global event active hai — sabke cards pe event background lagao
        event_bg_path = MEDIA_DIR / event["event_background"]
        if event_bg_path.exists():
            image = Image.open(event_bg_path)
        else:
            image = Image.open(ball.cached_regime.background)

    else:
        image = Image.open(ball.cached_regime.background)

    image = image.convert("RGBA")
    icon = Image.open(ball.cached_economy.icon).convert("RGBA") if ball.cached_economy else None

    draw = ImageDraw.Draw(image)
    draw.text((50, 20), ball.short_name or ball.country, font=title_font, stroke_width=2, stroke_fill=(0, 0, 0, 255))

    cap_name = textwrap.wrap(f"Ability: {ball.capacity_name}", width=26)

    for i, line in enumerate(cap_name):
        draw.text(
            (100, 1050 + 100 * i),
            line,
            font=capacity_name_font,
            fill=(230, 230, 230, 255),
            stroke_width=2,
            stroke_fill=(0, 0, 0, 255),
        )

    capacity_description_lines = (
        wrapped_line
        for newline in ball.capacity_description.splitlines()
        for wrapped_line in textwrap.wrap(newline, 32)
    )

    for i, line in enumerate(capacity_description_lines):
        draw.text(
            (60, 1100 + 100 * len(cap_name) + 80 * i),
            line,
            font=capacity_description_font,
            stroke_width=1,
            stroke_fill=(0, 0, 0, 255),
        )

    draw.text(
        (320, 1670),
        str(ball_instance.health),
        font=stats_font,
        fill=ball_health,
        stroke_width=1,
        stroke_fill=(0, 0, 0, 255),
    )
    draw.text(
        (1120, 1670),
        str(ball_instance.attack),
        font=stats_font,
        fill=(252, 194, 76, 255),
        stroke_width=1,
        stroke_fill=(0, 0, 0, 255),
        anchor="ra",
    )
    if settings.show_rarity:
        draw.text((1200, 50), str(ball.rarity), font=stats_font, stroke_width=2, stroke_fill=(0, 0, 0, 255))
    if card_name in credits_color_cache:
        credits_color = credits_color_cache[card_name]
    else:
        credits_color = get_credit_color(image, (0, int(image.height * 0.8), image.width, image.height))
        credits_color_cache[card_name] = credits_color
    draw.text(
        (30, 1870),
        # Modifying the line below is breaking the licence as you are removing credits
        # If you don't want to receive a DMCA, just don't
        f"Created by El Laggron{special_credits}\nArtwork author: {ball_credits}",
        font=credits_font,
        fill=credits_color,
        stroke_width=0,
        stroke_fill=(255, 255, 255, 255),
    )

    artwork = Image.open(ball.collection_card).convert("RGBA")
    image.paste(ImageOps.fit(artwork, artwork_size), CORNERS[0])  # type: ignore

    if icon:
        icon = ImageOps.fit(icon, (192, 192))
        image.paste(icon, (1200, 30), mask=icon)
        icon.close()
    artwork.close()

    # ── Foreground overlay — sabse upar lagta hai ─────────────────────────────
    if event.get("foreground_overlay"):
        overlay_path = MEDIA_DIR / event["foreground_overlay"]
        if overlay_path.exists():
            overlay = Image.open(overlay_path).convert("RGBA")
            if overlay.size != (WIDTH, HEIGHT):
                overlay = overlay.resize((WIDTH, HEIGHT), Image.LANCZOS)
            image.paste(overlay, (0, 0), mask=overlay)
            overlay.close()

    return image, {"format": "WEBP"}


def draw_premade_card(
    background_path: "str | Path",
    foreground_path: "str | Path",
    player_name: str,
    codename: str,
    description: str,
    rarity: float,
    bat_score: int,
    ball_score: int,
    artwork_author: str,
    logo_path: "str | Path | None" = None,
) -> "tuple[Image.Image, dict[str, Any]]":
    """Generate a Dembele-style cricket card with Nulshock Bold font."""

    PANEL_Y = int(HEIGHT * 0.61)

    # Background (full card)
    bg = Image.open(str(background_path)).convert("RGBA")
    bg = ImageOps.fit(bg, (WIDTH, HEIGHT), Image.LANCZOS)

    # Foreground player image (top 61%, pasted over background)
    fg = Image.open(str(foreground_path)).convert("RGBA")
    fg_fitted = ImageOps.fit(fg, (WIDTH, PANEL_Y), Image.LANCZOS)
    bg.paste(fg_fitted, (0, 0), mask=fg_fitted)
    fg.close()
    fg_fitted.close()

    draw = ImageDraw.Draw(bg)

    # Player name (top-left, over the foreground)
    draw.text(
        (45, 18),
        player_name.upper(),
        font=nulshock_title_font,
        fill=(255, 255, 255, 255),
        stroke_width=4,
        stroke_fill=(0, 0, 0, 220),
    )

    # Rarity badge (top-right, orange circle)
    rarity_str = str(rarity)
    badge_cx, badge_cy = WIDTH - 115, 112
    badge_r = 96
    draw.ellipse(
        [(badge_cx - badge_r, badge_cy - badge_r), (badge_cx + badge_r, badge_cy + badge_r)],
        fill=(200, 100, 0, 220),
        outline=(255, 180, 0, 255),
        width=6,
    )
    draw.text(
        (badge_cx, badge_cy),
        rarity_str,
        font=nulshock_rarity_font,
        fill=(255, 255, 255, 255),
        anchor="mm",
        stroke_width=1,
        stroke_fill=(0, 0, 0, 255),
    )

    # Dark info panel (bottom 39%)
    panel_overlay = Image.new("RGBA", (WIDTH, HEIGHT - PANEL_Y), (12, 12, 32, 225))
    bg.paste(panel_overlay, (0, PANEL_Y), mask=panel_overlay)
    panel_overlay.close()

    # Gold separator line
    draw = ImageDraw.Draw(bg)
    draw.rectangle([(0, PANEL_Y), (WIDTH, PANEL_Y + 6)], fill=(200, 150, 0, 210))

    # Logo (top-right of panel, optional)
    if logo_path and os.path.exists(str(logo_path)):
        try:
            logo = Image.open(str(logo_path)).convert("RGBA")
            logo_size = 140
            logo_fitted = ImageOps.fit(logo, (logo_size, logo_size))
            bg.paste(logo_fitted, (WIDTH - logo_size - 22, PANEL_Y + 22), mask=logo_fitted)
            logo.close()
            logo_fitted.close()
        except Exception:
            pass

    # Codename header
    codename_y = PANEL_Y + 28
    draw.text(
        (42, codename_y),
        f"CODENAME: {codename.upper()}",
        font=nulshock_codename_font,
        fill=(255, 200, 50, 255),
        stroke_width=1,
        stroke_fill=(0, 0, 0, 255),
    )

    # Description text (wrapped)
    desc_y = codename_y + 108
    desc_lines: list[str] = []
    for raw_line in description.splitlines():
        desc_lines.extend(textwrap.wrap(raw_line, width=38))

    for i, line in enumerate(desc_lines[:7]):
        draw.text(
            (42, desc_y + 72 * i),
            line,
            font=capacity_description_font,
            fill=(210, 210, 210, 255),
        )

    # Stats area
    stats_y = HEIGHT - 310
    icon_size = 68

    # --- Cricket bat icon (left side, pink/red theme) ---
    bat_ix, bat_iy = 38, stats_y - 10
    # Bat blade: wide rounded rectangle
    draw.rounded_rectangle(
        [(bat_ix, bat_iy), (bat_ix + icon_size, bat_iy + icon_size - 18)],
        radius=14,
        fill=(200, 80, 60, 230),
        outline=(237, 115, 101, 255),
        width=3,
    )
    # Bat handle: thin vertical strip extending below blade
    handle_x = bat_ix + icon_size // 2 - 6
    draw.rounded_rectangle(
        [(handle_x, bat_iy + icon_size - 20), (handle_x + 12, bat_iy + icon_size + 12)],
        radius=4,
        fill=(160, 60, 40, 230),
        outline=(237, 115, 101, 200),
        width=2,
    )

    # Bat score number
    draw.text(
        (bat_ix + icon_size + 14, stats_y),
        str(bat_score),
        font=nulshock_stats_font,
        fill=(237, 115, 101, 255),
        stroke_width=2,
        stroke_fill=(0, 0, 0, 255),
    )
    draw.text(
        (bat_ix, stats_y + 140),
        "BAT",
        font=nulshock_codename_font,
        fill=(237, 115, 101, 200),
    )

    # --- Cricket ball icon (right side, gold theme) ---
    ball_ix = WIDTH - icon_size - 38
    ball_iy = stats_y - 2
    ball_cx = ball_ix + icon_size // 2
    ball_cy = ball_iy + icon_size // 2
    ball_r = icon_size // 2
    # Ball body
    draw.ellipse(
        [(ball_ix, ball_iy), (ball_ix + icon_size, ball_iy + icon_size)],
        fill=(180, 30, 30, 230),
        outline=(252, 194, 76, 255),
        width=3,
    )
    # Seam: vertical arc on left half
    draw.arc(
        [(ball_cx - ball_r + 6, ball_cy - ball_r + 4), (ball_cx + 8, ball_cy + ball_r - 4)],
        start=200,
        end=340,
        fill=(255, 255, 255, 200),
        width=3,
    )
    # Seam: vertical arc on right half (mirrored)
    draw.arc(
        [(ball_cx - 8, ball_cy - ball_r + 4), (ball_cx + ball_r - 6, ball_cy + ball_r - 4)],
        start=20,
        end=160,
        fill=(255, 255, 255, 200),
        width=3,
    )

    # Ball score number
    draw.text(
        (ball_ix - 14, stats_y),
        str(ball_score),
        font=nulshock_stats_font,
        fill=(252, 194, 76, 255),
        stroke_width=2,
        stroke_fill=(0, 0, 0, 255),
        anchor="ra",
    )
    draw.text(
        (WIDTH - 38, stats_y + 140),
        "BALL",
        font=nulshock_codename_font,
        fill=(252, 194, 76, 200),
        anchor="ra",
    )

    # Credits (bottom-left, always "Created by El Laggron")
    draw.text(
        (32, HEIGHT - 82),
        f"Created by El Laggron  \u2022  Artwork: {artwork_author}",
        font=credits_font,
        fill=(170, 170, 170, 255),
    )

    return bg, {"format": "PNG"}
