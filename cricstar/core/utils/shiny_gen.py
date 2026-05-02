"""
Shared utility for generating a shiny card image for a Ball.

Used by both /rankup upgrade and /admin cricketer give (when Shiny special is assigned).
Returns a dict to merge into BallInstance.extra_data, e.g. {"shiny_card": "premade_x_shiny.png"}.
Returns an empty dict if the shiny background is not configured or the foreground preset is missing.
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bd_models.models import Ball

log = logging.getLogger("cricstar.core.utils.shiny_gen")

SHINY_BG_PATH = Path("admin_panel/media/shiny_bg.png")
FOREGROUNDS_DIR = Path("admin_panel/media/foregrounds")


def _find_foreground(slug: str) -> Path | None:
    for ext in ("", ".png", ".jpg", ".jpeg", ".webp"):
        candidate = FOREGROUNDS_DIR / f"{slug}{ext}"
        if candidate.exists():
            return candidate
    return None


async def generate_shiny_extra(ball: Ball, loop) -> dict:
    """
    Generate the shiny card image for `ball` and return
    {"shiny_card": "<filename>"}, or {} if generation is not possible.

    `loop` should be asyncio.get_event_loop() or bot.loop.
    """
    if not SHINY_BG_PATH.exists():
        log.debug("shiny_gen: no shiny_bg.png, skipping for %s", ball.country)
        return {}

    slug = re.sub(r"[^a-z0-9]+", "_", ball.country.lower().strip()).strip("_")
    fg_src = _find_foreground(slug)
    if fg_src is None:
        log.debug("shiny_gen: no foreground preset for %s (slug=%s)", ball.country, slug)
        return {}

    shiny_filename = f"premade_{slug}_shiny.png"
    shiny_card_path = Path("admin_panel/media") / shiny_filename

    # Reuse existing file if it already exists (avoids redundant regeneration)
    if shiny_card_path.exists():
        log.debug("shiny_gen: reusing existing shiny card %s", shiny_filename)
        return {"shiny_card": shiny_filename}

    logic = dict(ball.capacity_logic or {})
    _card_name = logic.get("display_name") or ball.country
    _codename = ball.capacity_name or ""
    _description = ball.capacity_description or ""
    _rarity = logic.get("badge_rarity", ball.rarity)
    _fg_border = logic.get("foreground_border", True)
    _credit_stroke = logic.get("credit_stroke", True)
    _credit_font = logic.get("credit_font", "default")

    from cricstar.core.image_generator.image_gen import draw_premade_card, get_neon_color
    from cricstar.core.foreground_border_overrides import resolve_border as _resolve_border

    _border_size = _resolve_border(ball.pk, str(fg_src))
    _fg_src_str = str(fg_src)
    _bg_str = str(SHINY_BG_PATH)

    def _gen() -> tuple:
        return draw_premade_card(
            _bg_str, _fg_src_str,
            _card_name, _codename, _description,
            _rarity, ball.health, ball.attack,
            ball.credits or "", None,
            neon_color=get_neon_color(ball.country),
            foreground_border=_fg_border,
            credit_stroke=_credit_stroke,
            credit_font=_credit_font,
            border_size=_border_size,
        )

    try:
        with ThreadPoolExecutor() as pool:
            shiny_img, shiny_kwargs = await loop.run_in_executor(pool, _gen)
        shiny_img.save(str(shiny_card_path), **shiny_kwargs)
        shiny_img.close()
        log.info("shiny_gen: generated shiny card %s", shiny_filename)
        return {"shiny_card": shiny_filename}
    except Exception as e:
        log.warning("shiny_gen: could not generate shiny card for %s: %s", ball.country, e)
        return {}
