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
NEON_COLORS_PATH = MEDIA_DIR / "neon_colors.json"

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


def load_neon_colors() -> dict:
    """Load the per-player neon color map from disk."""
    try:
        if NEON_COLORS_PATH.exists():
            return json.loads(NEON_COLORS_PATH.read_text())
    except Exception:
        pass
    return {}


def save_neon_color(country: str, color: "tuple | None") -> None:
    """Persist a neon color for a player, or remove it if color is None."""
    colors = load_neon_colors()
    if color is None:
        colors.pop(country, None)
    else:
        colors[country] = list(color)
    NEON_COLORS_PATH.write_text(json.dumps(colors, indent=2))


def get_neon_color(country: str) -> "tuple | None":
    """Return the stored RGB tuple for a player, or None if not set."""
    c = load_neon_colors().get(country)
    if c and len(c) >= 3:
        return (int(c[0]), int(c[1]), int(c[2]))
    return None


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
    neon = get_neon_color(ball.country)
    if neon:
        apply_neon_glow(image, CORNERS[0][0], CORNERS[0][1], artwork_size[0], artwork_size[1], color=neon)
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
    neon_color: "tuple | None" = None,
) -> "tuple[Image.Image, dict[str, Any]]":
    """
    Generate a cricket trading card.

    Layout (1428 × 2000 — portrait):
    ┌─────────────────────────────────────────┐
    │  PLAYER NAME (white, left)  RARITY (gold, right)  │  ← 130 px top bar
    │ ┌─────────────────────────────────────┐ │
    │ │       foreground player image       │ │  ← 710 px image frame
    │ └─────────────────────────────────────┘ │
    │  CODENAME: …   (white, 70px)            │
    │  description…  (white, 76px)            │
    │                                         │
    │  Created by… / Artwork: …   [bat] 200  [ball] 200  │
    └─────────────────────────────────────────┘
    """

    # ── Canvas & layout constants ─────────────────────────────────────────────
    CARD_W   = 1428
    CARD_H   = 2000
    MARGIN   = 32                          # edge → content margin

    TOP_BAR_H  = 130                       # height of name / rarity strip
    FRAME_X    = MARGIN
    FRAME_Y    = TOP_BAR_H + 137          # +65 px
    FRAME_W    = CARD_W - 2 * MARGIN      # 1364 px
    FRAME_H    = 716                       # frame height
    FRAME_BOTTOM = FRAME_Y + FRAME_H

    INFO_Y     = FRAME_BOTTOM + 36        # where text panel begins
    CODENAME_Y = INFO_Y + 72             # -10 px
    DESC_Y     = CODENAME_Y + 104       # -15 px

    BAR_H  = 180                          # bottom stats bar height
    BAR_Y  = CARD_H - BAR_H              # 1820

    # ── Fonts (loaded locally so sizes are independent) ───────────────────────
    nulshock = str(SOURCES_PATH / "Nulshock-Bold.otf")
    fontspring = str(SOURCES_PATH / "Fontspring-DEMO-alergia_remix-bold-iF66c45d3230ef9_1775467503470.otf")
    arial = str(SOURCES_PATH / "arial.ttf")

    def _font(path: str, size: int, fallback_path: str | None = None) -> ImageFont.FreeTypeFont:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            if fallback_path:
                return ImageFont.truetype(fallback_path, size)
            return ImageFont.load_default()

    bar_font     = _font(nulshock, 122)                          # name + rarity
    codename_fnt = _font(nulshock, 50)                           # CODENAME: …
    try:
        desc_fnt = _font(fontspring, 55)                         # description body
    except Exception:
        desc_fnt = _font(nulshock, 55)
    stat_fnt     = _font(nulshock, 107)                          # stat numbers (200 / 200)
    cred_fnt     = _font(arial, 42, nulshock)                    # credits small text

    # ── 1. Background: open + resize to card dimensions ───────────────────────
    bg = Image.open(str(background_path)).convert("RGBA")
    bg = ImageOps.fit(bg, (CARD_W, CARD_H), Image.LANCZOS)
    draw = ImageDraw.Draw(bg)

    # ── 2. Top bar: player name (left, white) + rarity (right, gold) ─────────
    rarity_str = str(rarity)
    name_text  = player_name.upper()

    NAME_RARITY_Y = TOP_BAR_H // 2 + 90   # -20 px

    # Rarity — right-aligned, gold
    draw.text(
        (CARD_W - MARGIN, NAME_RARITY_Y),
        rarity_str,
        font=bar_font,
        fill=(255, 184, 0, 255),
        anchor="rm",
        stroke_width=5,
        stroke_fill=(0, 0, 0, 240),
    )

    # Player name — left-aligned, white; auto-shrink if it would overlap rarity
    rarity_w = int(bar_font.getlength(rarity_str)) + MARGIN + 20
    avail_w  = CARD_W - 2 * MARGIN - rarity_w
    if bar_font.getlength(name_text) <= avail_w:
        name_fnt = bar_font
    else:
        scale    = avail_w / bar_font.getlength(name_text)
        name_fnt = _font(nulshock, max(48, int(122 * scale)))
    draw.text(
        (MARGIN, NAME_RARITY_Y),
        name_text,
        font=name_fnt,
        fill=(255, 255, 255, 255),
        anchor="lm",
        stroke_width=5,
        stroke_fill=(0, 0, 0, 240),
    )

    # ── 3. Image frame — white border outline + foreground image ─────────────
    BORDER = 5
    # White rectangle border drawn BEFORE the image so it sits behind
    draw.rectangle(
        [(FRAME_X - BORDER, FRAME_Y - BORDER),
         (FRAME_X + FRAME_W + BORDER, FRAME_BOTTOM + BORDER)],
        outline=(255, 255, 255, 255),
        width=BORDER,
    )

    fg = Image.open(str(foreground_path)).convert("RGBA")
    fg_fitted = ImageOps.fit(fg, (FRAME_W, FRAME_H), Image.LANCZOS)
    bg.paste(fg_fitted, (FRAME_X, FRAME_Y), mask=fg_fitted)
    fg.close()
    fg_fitted.close()

    # Re-acquire draw handle after paste operations
    draw = ImageDraw.Draw(bg)

    # ── 4a. Logo — right-aligned, vertically centred with codename line ───────
    if logo_path and os.path.exists(str(logo_path)):
        try:
            logo      = Image.open(str(logo_path)).convert("RGBA")
            logo_size = 145
            logo_fit  = ImageOps.fit(logo, (logo_size, logo_size))
            logo_x    = CARD_W - logo_size - MARGIN
            logo_y    = CODENAME_Y - 20             # 20 px higher than codename
            bg.paste(logo_fit, (logo_x, logo_y), mask=logo_fit)
            logo.close()
            logo_fit.close()
            draw = ImageDraw.Draw(bg)
        except Exception:
            pass

    # ── 4b. Codename — white bold text ────────────────────────────────────────
    draw.text(
        (MARGIN, CODENAME_Y),
        f"CODENAME: {codename.upper()}",
        font=codename_fnt,
        fill=(255, 255, 255, 255),
        stroke_width=3,
        stroke_fill=(0, 0, 0, 255),
    )

    # ── 4c. Description — slightly larger white text, word-wrapped ────────────
    # Leave room on the right for the logo (145px wide + margin gap)
    logo_size = 145
    text_avail_w = FRAME_W - logo_size - 20   # ~1199 px
    wrap_width = int(text_avail_w / (desc_fnt.size * 0.58))
    desc_lines: list[str] = []
    for raw_line in description.splitlines():
        desc_lines.extend(textwrap.wrap(raw_line, width=max(10, wrap_width)) or [""])

    LINE_H = int(desc_fnt.size * 1.28)   # line spacing
    for i, line in enumerate(desc_lines[:8]):
        draw.text(
            (MARGIN, DESC_Y + LINE_H * i),
            line,
            font=desc_fnt,
            fill=(255, 255, 255, 255),
            stroke_width=2,
            stroke_fill=(0, 0, 0, 255),
        )

    # ── 5. Bottom stats bar (no background fill — background shows through) ─────
    draw = ImageDraw.Draw(bg)

    STAT_CY = BAR_Y + BAR_H // 2 - 18   # +10 px higher

    # ── Helper: paste a PNG icon scaled to target height, return actual width ─
    def _paste_icon(img_path: str, target_h: int, x: int) -> int:
        """Load img_path, scale to target_h, paste at (x, centred in bar).
        Returns the rendered width so the caller can position text after it."""
        try:
            ico = Image.open(img_path).convert("RGBA")
            w0, h0 = ico.size
            target_w = int(target_h * w0 / h0)
            ico = ico.resize((target_w, target_h), Image.LANCZOS)
            iy = STAT_CY - target_h // 2
            bg.paste(ico, (x, iy), mask=ico)
            ico.close()
            return target_w
        except Exception:
            return 0

    ICON_H   = 120   # icon height inside the bar (180px bar has 30px padding each side)
    GAP      = 14    # gap between icon and number text

    bat_icon_path  = str(SOURCES_PATH / "bat_icon.png")
    ball_icon_path = str(SOURCES_PATH / "ball_icon.png")

    # ── Bat stat ──────────────────────────────────────────────────────────────
    BAT_X = 530
    bat_icon_w = _paste_icon(bat_icon_path, ICON_H, BAT_X)
    draw = ImageDraw.Draw(bg)   # re-acquire after paste

    if bat_icon_w == 0:
        # Fallback: drawn bat shape
        ICON_SZ = 70
        bat_iy = STAT_CY - ICON_SZ // 2
        blade_w, blade_h = ICON_SZ - 10, ICON_SZ - 18
        hx = BAT_X + (ICON_SZ - 10) // 2 - 5
        draw.rounded_rectangle(
            [(BAT_X, bat_iy), (BAT_X + blade_w, bat_iy + blade_h)],
            radius=12, fill=(210, 90, 60, 240), outline=(237, 120, 100, 255), width=3,
        )
        draw.rounded_rectangle(
            [(hx, bat_iy + blade_h - 2), (hx + 10, bat_iy + blade_h + 22)],
            radius=4, fill=(160, 55, 35, 230), outline=(220, 100, 75, 200), width=2,
        )
        bat_icon_w = ICON_SZ

    draw.text(
        (BAT_X + bat_icon_w + GAP, STAT_CY),
        str(bat_score),
        font=stat_fnt,
        fill=(237, 115, 101, 255),
        anchor="lm",
        stroke_width=3,
        stroke_fill=(0, 0, 0, 255),
    )

    # ── Ball stat ─────────────────────────────────────────────────────────────
    BALL_X = 980
    ball_icon_w = _paste_icon(ball_icon_path, ICON_H, BALL_X)
    draw = ImageDraw.Draw(bg)

    if ball_icon_w == 0:
        # Fallback: drawn ball circle
        ICON_SZ = 70
        ball_iy = STAT_CY - ICON_SZ // 2
        ball_cx = BALL_X + ICON_SZ // 2
        ball_cy = STAT_CY
        ball_r  = ICON_SZ // 2
        draw.ellipse(
            [(BALL_X, ball_iy), (BALL_X + ICON_SZ, ball_iy + ICON_SZ)],
            fill=(210, 210, 215, 240), outline=(180, 180, 185, 255), width=3,
        )
        draw.arc([(ball_cx - ball_r + 8, ball_cy - ball_r + 4), (ball_cx + 6, ball_cy + ball_r - 4)],
                 start=200, end=340, fill=(160, 160, 165, 230), width=4)
        draw.arc([(ball_cx - 6, ball_cy - ball_r + 4), (ball_cx + ball_r - 8, ball_cy + ball_r - 4)],
                 start=20, end=160, fill=(160, 160, 165, 230), width=4)
        ball_icon_w = ICON_SZ

    draw.text(
        (BALL_X + ball_icon_w + GAP, STAT_CY),
        str(ball_score),
        font=stat_fnt,
        fill=(252, 194, 76, 255),
        anchor="lm",
        stroke_width=3,
        stroke_fill=(0, 0, 0, 255),
    )

    # ── Credits — two lines, bottom-left (strong stroke for any background) ────
    cred_y = BAR_Y + 18
    for cred_line in ("Created by El Laggron", f"Artwork: {artwork_author}"):
        draw.text(
            (MARGIN, cred_y),
            cred_line,
            font=cred_fnt,
            fill=(230, 230, 240, 255),
            stroke_width=3,
            stroke_fill=(0, 0, 0, 255),
        )
        cred_y += int(cred_fnt.size * 1.3)

    return bg, {"format": "PNG"}


def patch_card_stats(
    card_path: str,
    background_path: str,
    bat_score: int,
    ball_score: int,
    artwork_author: str,
) -> None:
    """
    Open an existing card PNG and repaint ONLY the bottom stats bar
    (bat icon, bat number, ball icon, ball number, credits).
    Everything else (rarity, name, foreground, background effects) is untouched.
    """
    CARD_W, CARD_H = 1428, 2000
    MARGIN = 32
    BAR_H  = 180
    BAR_Y  = CARD_H - BAR_H          # 1820
    STAT_CY = BAR_Y + BAR_H // 2 - 18  # same as draw_premade_card

    nulshock   = str(SOURCES_PATH / "Nulshock-Bold.otf")
    arial      = str(SOURCES_PATH / "arial.ttf")
    stat_fnt   = ImageFont.truetype(nulshock, 107)
    cred_fnt   = ImageFont.truetype(arial, 42)

    bat_icon_path  = str(SOURCES_PATH / "bat_icon.png")
    ball_icon_path = str(SOURCES_PATH / "ball_icon.png")

    # Open existing card
    card = Image.open(card_path).convert("RGBA")

    # Restore the clean background at the stats bar area (erases old numbers)
    bg = Image.open(background_path).convert("RGBA")
    bg = ImageOps.fit(bg, (CARD_W, CARD_H), Image.LANCZOS)
    bg_bar_strip = bg.crop((0, BAR_Y, CARD_W, CARD_H))
    card.paste(bg_bar_strip, (0, BAR_Y))
    bg.close()
    bg_bar_strip.close()

    draw = ImageDraw.Draw(card)

    ICON_H = 120
    GAP    = 14

    def _paste_icon_patch(img_path: str, target_h: int, x: int) -> int:
        try:
            ico = Image.open(img_path).convert("RGBA")
            w0, h0 = ico.size
            target_w = int(target_h * w0 / h0)
            ico = ico.resize((target_w, target_h), Image.LANCZOS)
            iy  = STAT_CY - target_h // 2
            card.paste(ico, (x, iy), mask=ico)
            ico.close()
            return target_w
        except Exception:
            return 0

    # Bat
    BAT_X      = 530
    bat_icon_w = _paste_icon_patch(bat_icon_path, ICON_H, BAT_X)
    draw       = ImageDraw.Draw(card)
    if bat_icon_w == 0:
        bat_icon_w = 70
    draw.text(
        (BAT_X + bat_icon_w + GAP, STAT_CY),
        str(bat_score),
        font=stat_fnt,
        fill=(237, 115, 101, 255),
        anchor="lm",
        stroke_width=3,
        stroke_fill=(0, 0, 0, 255),
    )

    # Ball
    BALL_X      = 980
    ball_icon_w = _paste_icon_patch(ball_icon_path, ICON_H, BALL_X)
    draw        = ImageDraw.Draw(card)
    if ball_icon_w == 0:
        ball_icon_w = 70
    draw.text(
        (BALL_X + ball_icon_w + GAP, STAT_CY),
        str(ball_score),
        font=stat_fnt,
        fill=(252, 194, 76, 255),
        anchor="lm",
        stroke_width=3,
        stroke_fill=(0, 0, 0, 255),
    )

    # Credits
    cred_y = BAR_Y + 18
    for cred_line in ("Created by El Laggron", f"Artwork: {artwork_author}"):
        draw.text(
            (MARGIN, cred_y),
            cred_line,
            font=cred_fnt,
            fill=(230, 230, 240, 255),
            stroke_width=3,
            stroke_fill=(0, 0, 0, 255),
        )
        cred_y += int(cred_fnt.size * 1.3)

    card.save(card_path, format="PNG")
    card.close()
