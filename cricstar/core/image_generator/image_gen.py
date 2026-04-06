import json
import os
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

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
try:
    capacity_description_font = ImageFont.truetype(str(SOURCES_PATH / "Fontspring-DEMO-alergia_remix-bold-iF66c45d3230ef9_1775467503470.otf"), 65)
except Exception:
    capacity_description_font = ImageFont.truetype(str(SOURCES_PATH / "OpenSans-Semibold.ttf"), 75)
stats_font = ImageFont.truetype(str(SOURCES_PATH / "Bobby Jones Soft.otf"), 130)
credits_font = ImageFont.truetype(str(SOURCES_PATH / "arial.ttf"), 40)

try:
    nulshock_title_font = ImageFont.truetype(str(SOURCES_PATH / "Nulshock-Bold.otf"), 150)
    nulshock_codename_font = ImageFont.truetype(str(SOURCES_PATH / "Nulshock-Bold.otf"), 76)
    nulshock_rarity_font = ImageFont.truetype(str(SOURCES_PATH / "Nulshock-Bold.otf"), 58)
    nulshock_stats_font = ImageFont.truetype(str(SOURCES_PATH / "Nulshock-Bold.otf"), 130)
    nulshock_desc_font = ImageFont.truetype(str(SOURCES_PATH / "Nulshock-Bold.otf"), 40)
except Exception:
    nulshock_title_font = title_font
    nulshock_codename_font = capacity_name_font
    nulshock_rarity_font = stats_font
    nulshock_stats_font = stats_font
    nulshock_desc_font = capacity_description_font

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


def apply_neon_glow(
    card: Image.Image,
    x: int,
    y: int,
    w: int,
    h: int,
    color: tuple = (0, 210, 255),
    pad: int = 45,
    passes: int = 3,
    blur_radius: int = 22,
) -> None:
    """Paste a soft neon glow halo behind a rectangular region on `card` (in-place).

    A filled rectangle the size of the region is drawn on a transparent canvas,
    then blurred multiple times to produce a smooth coloured halo.  The result is
    alpha-composited onto `card` *before* the foreground image is pasted, so the
    glow appears to emanate from behind the artwork.
    """
    canvas_w = w + pad * 2
    canvas_h = h + pad * 2
    glow = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    core = Image.new("RGBA", (w, h), (*color, 255))
    glow.paste(core, (pad, pad))
    core.close()
    for _ in range(passes):
        glow = glow.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    # Boost intensity by alpha-compositing a second copy on top
    boosted = Image.alpha_composite(glow, glow)
    glow.close()
    paste_x = x - pad
    paste_y = y - pad
    # Clip to card boundaries
    cx = max(paste_x, 0)
    cy = max(paste_y, 0)
    ox = cx - paste_x
    oy = cy - paste_y
    region = boosted.crop((ox, oy, ox + card.width - cx, oy + card.height - cy))
    card.paste(region, (cx, cy), mask=region)
    boosted.close()
    region.close()


def draw_card(ball_instance: "BallInstance") -> tuple[Image.Image, dict[str, Any]]:
    ball = ball_instance.cricketer

    # Load event config
    event = load_event_config()

    # Pre-made card bypass: if collection_card filename starts with "premade_",
    # just resize it to card dimensions and return it directly — no text overlay.
    # BUT agar event active hai aur foreground_overlay hai toh overlay lagao.
    card_name_field = str(ball.collection_card.name) if ball.collection_card else ""
    if card_name_field.startswith("premade_"):
        # Open via direct filesystem path so we always read the latest file on
        # disk (bypasses any Django storage caching that could return stale data).
        premade_path = MEDIA_DIR / card_name_field
        premade = Image.open(str(premade_path)).convert("RGBA")
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
    apply_neon_glow(image, CORNERS[0][0], CORNERS[0][1], artwork_size[0], artwork_size[1])
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
    """
    Generate a trading-card-style cricket card with Nulshock Bold font.

    Layout (1500 × 2000):
    ┌────────────────────────────────────────┐
    │  PLAYER NAME (left)    RARITY (right)  │  ← 115 px
    │ ┌──────────────────────────────────┐   │
    │ │  landscape player image frame    │   │  ← 760 px
    │ └──────────────────────────────────┘   │
    │  CODENAME: …                           │
    │  description text                      │
    │                                        │
    │  200 BAT              209 BALL          │
    │  Created by El Laggron • Artwork: …    │
    └────────────────────────────────────────┘
    """

    # ── Layout constants ──────────────────────────────────────────────────────
    MARGIN = 28                          # card edge → text/info margin
    TOP_BAR_H = 160                      # name + rarity strip (tall enough for 120px font)
    FRAME_Y = TOP_BAR_H                  # frame starts immediately below name bar (no gap)
    FRAME_H = 780                        # taller landscape frame — fills more of the card
    FRAME_W = WIDTH                      # full card width — no side margins on the image
    FRAME_X = 0                          # foreground image starts at left edge
    FRAME_BOTTOM = FRAME_Y + FRAME_H    # ≈ 940
    INFO_Y = FRAME_BOTTOM + 12          # info panel starts here
    # Bottom bar constants are defined inline in section 5

    # Smaller Nulshock font for the top name/rarity bar
    try:
        bar_font = ImageFont.truetype(str(SOURCES_PATH / "Nulshock-Bold.otf"), 118)
    except Exception:
        bar_font = nulshock_title_font

    # ── 1. Background (full card) ─────────────────────────────────────────────
    bg = Image.open(str(background_path)).convert("RGBA")
    bg = ImageOps.fit(bg, (WIDTH, HEIGHT), Image.LANCZOS)

    draw = ImageDraw.Draw(bg)

    # ── 2. Name + rarity drawn directly on background (no dark overlay) ───────
    rarity_str = str(rarity)

    # Rarity — plain gold text, right-aligned, with strong black outline so it
    # is readable on any background without a strip behind it
    draw.text(
        (WIDTH - MARGIN, TOP_BAR_H // 2),
        rarity_str,
        font=bar_font,
        fill=(255, 210, 0, 255),
        anchor="rm",
        stroke_width=6,
        stroke_fill=(0, 0, 0, 230),
    )

    # Player name — left-aligned, white, dynamically shrunk to fit beside rarity
    name_text = player_name.upper()
    rarity_w = int(bar_font.getlength(rarity_str)) + MARGIN + 24
    avail_name_w = WIDTH - 2 * MARGIN - rarity_w
    if bar_font.getlength(name_text) <= avail_name_w:
        name_font = bar_font
    else:
        scale = avail_name_w / bar_font.getlength(name_text)
        scaled_size = max(48, int(118 * scale))
        try:
            name_font = ImageFont.truetype(str(SOURCES_PATH / "Nulshock-Bold.otf"), scaled_size)
        except Exception:
            name_font = bar_font
    draw.text(
        (MARGIN, TOP_BAR_H // 2),
        name_text,
        font=name_font,
        fill=(255, 255, 255, 255),
        anchor="lm",
        stroke_width=6,
        stroke_fill=(0, 0, 0, 230),
    )

    # ── 3. Player image: landscape frame ─────────────────────────────────────
    # Paste foreground image inside the frame
    fg = Image.open(str(foreground_path)).convert("RGBA")
    # Fit inside the landscape frame — minimal cropping for landscape images
    fg_fitted = ImageOps.fit(fg.convert("RGBA"), (FRAME_W, FRAME_H), Image.LANCZOS)
    # Neon glow behind the foreground frame
    apply_neon_glow(bg, FRAME_X, FRAME_Y, FRAME_W, FRAME_H)
    # Use alpha channel as mask so transparent PNGs composite correctly;
    # for opaque JPEGs the alpha is all-255 so this works in both cases.
    bg.paste(fg_fitted, (FRAME_X, FRAME_Y), mask=fg_fitted)
    fg.close()
    fg_fitted.close()

    # ── 4. Info section (drawn directly on background — NO dark overlay) ────────
    draw = ImageDraw.Draw(bg)

    # ── 4a. Logo (top-right of info panel, optional) ──────────────────────────
    logo_x = WIDTH - MARGIN
    if logo_path and os.path.exists(str(logo_path)):
        try:
            logo = Image.open(str(logo_path)).convert("RGBA")
            logo_size = 130
            logo_fitted = ImageOps.fit(logo, (logo_size, logo_size))
            logo_fitted = logo_fitted.convert("RGBA")
            bg.paste(logo_fitted, (WIDTH - logo_size - MARGIN, INFO_Y + 16), mask=logo_fitted)
            logo_x = WIDTH - logo_size - MARGIN - 10
            logo.close()
            logo_fitted.close()
        except Exception:
            pass
        draw = ImageDraw.Draw(bg)

    # ── 4b. Codename ──────────────────────────────────────────────────────────
    codename_y = INFO_Y + 22
    draw.text(
        (MARGIN, codename_y),
        f"CODENAME: {codename.upper()}",
        font=nulshock_codename_font,
        fill=(255, 210, 0, 255),
        stroke_width=4,
        stroke_fill=(0, 0, 0, 255),
    )

    # ── 4c. Description ───────────────────────────────────────────────────────
    # 170px below codename (60px up from the previous 230px gap)
    desc_y = codename_y + 170
    try:
        desc_font = ImageFont.truetype(str(SOURCES_PATH / "Fontspring-DEMO-alergia_remix-bold-iF66c45d3230ef9_1775467503470.otf"), 65)
    except Exception:
        desc_font = nulshock_desc_font

    desc_lines: list[str] = []
    words = description.split()
    for j in range(0, len(words), 5):
        desc_lines.append(" ".join(words[j:j + 5]))

    for i, line in enumerate(desc_lines[:10]):
        draw.text(
            (MARGIN, desc_y + 80 * i),
            line,
            font=desc_font,
            fill=(255, 255, 255, 255),
            stroke_width=1,
            stroke_fill=(0, 0, 0, 255),
        )

    # ── 5. Bottom stats + credits bar (Dembele style) ─────────────────────────
    # Solid darker strip at the very bottom of the card
    BAR_H = 120
    BAR_Y = HEIGHT - BAR_H
    bottom_bar = Image.new("RGBA", (WIDTH, BAR_H), (6, 5, 18, 255))
    bg.paste(bottom_bar, (0, BAR_Y), mask=bottom_bar)
    bottom_bar.close()
    draw = ImageDraw.Draw(bg)

    # Thin separator line above the bar
    draw.rectangle([(0, BAR_Y), (WIDTH, BAR_Y + 3)], fill=(120, 90, 220, 180))

    # Stat numbers font — smaller than before, matching Dembele proportions
    try:
        stat_font = ImageFont.truetype(str(SOURCES_PATH / "Nulshock-Bold.otf"), 88)
    except Exception:
        stat_font = nulshock_codename_font

    ICON = 52          # icon square size
    STAT_Y = BAR_Y + (BAR_H - ICON) // 2   # vertically centred in bar

    # ── BAT stat: icon + number, left half of stats area ─────────────────────
    # Bat positions (Dembele: ❤️342 sits around 40% from left)
    bat_ix = 470
    bat_iy = STAT_Y
    # Bat blade
    draw.rounded_rectangle(
        [(bat_ix, bat_iy), (bat_ix + ICON, bat_iy + ICON - 14)],
        radius=10,
        fill=(200, 80, 60, 235),
        outline=(237, 115, 101, 255),
        width=3,
    )
    # Bat handle
    hx = bat_ix + ICON // 2 - 4
    draw.rounded_rectangle(
        [(hx, bat_iy + ICON - 16), (hx + 8, bat_iy + ICON + 8)],
        radius=3,
        fill=(160, 60, 40, 230),
        outline=(237, 115, 101, 200),
        width=2,
    )
    draw.text(
        (bat_ix + ICON + 10, STAT_Y - 4),
        str(bat_score),
        font=stat_font,
        fill=(237, 115, 101, 255),
        stroke_width=2,
        stroke_fill=(0, 0, 0, 255),
    )

    # ── BALL stat: icon + number, right half of stats area ───────────────────
    ball_ix = 960
    ball_iy = STAT_Y + 2
    ball_cx = ball_ix + ICON // 2
    ball_cy = ball_iy + ICON // 2
    ball_r = ICON // 2
    draw.ellipse(
        [(ball_ix, ball_iy), (ball_ix + ICON, ball_iy + ICON)],
        fill=(180, 30, 30, 235),
        outline=(252, 194, 76, 255),
        width=3,
    )
    draw.arc(
        [(ball_cx - ball_r + 5, ball_cy - ball_r + 3), (ball_cx + 7, ball_cy + ball_r - 3)],
        start=200, end=340, fill=(255, 255, 255, 200), width=3,
    )
    draw.arc(
        [(ball_cx - 7, ball_cy - ball_r + 3), (ball_cx + ball_r - 5, ball_cy + ball_r - 3)],
        start=20, end=160, fill=(255, 255, 255, 200), width=3,
    )
    draw.text(
        (ball_ix + ICON + 10, STAT_Y - 2),
        str(ball_score),
        font=stat_font,
        fill=(252, 194, 76, 255),
        stroke_width=2,
        stroke_fill=(0, 0, 0, 255),
    )

    # ── Credits — two lines, bottom-left (Dembele style) ─────────────────────
    draw.text(
        (MARGIN, BAR_Y + 14),
        "Created by El Laggron",
        font=credits_font,
        fill=(190, 190, 210, 255),
    )
    draw.text(
        (MARGIN, BAR_Y + 60),
        f"Artwork: {artwork_author}",
        font=credits_font,
        fill=(190, 190, 210, 255),
    )

    return bg, {"format": "PNG"}
    