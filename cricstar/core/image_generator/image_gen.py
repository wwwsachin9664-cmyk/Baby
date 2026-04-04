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
    