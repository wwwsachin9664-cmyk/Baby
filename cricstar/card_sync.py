"""
card_sync.py — Auto-export and auto-import of /cardmaker cards.

Every card created via /cardmaker is saved to card_exports/ so that
when this project is remixed/forked to another Replit account, the bot
startup automatically restores all cards into the new database.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger("cricstar.card_sync")

BASE_DIR = Path(__file__).resolve().parent.parent
EXPORTS_DIR = BASE_DIR / "card_exports"
IMAGES_DIR = EXPORTS_DIR / "images"
FOREGROUNDS_DIR = EXPORTS_DIR / "foregrounds"
CARDS_JSON = EXPORTS_DIR / "cards.json"
MEDIA_DIR = BASE_DIR / "admin_panel" / "media"


def _ensure_dirs():
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    FOREGROUNDS_DIR.mkdir(parents=True, exist_ok=True)


def export_card(
    player_name: str,
    card_name: str,
    slug: str,
    codename: str,
    description: str,
    bat_score: int,
    ball_score: int,
    rarity: float,
    spawn_chance: float,
    artwork_author: str,
    tradeable: bool,
    spawnable: bool,
    catch_name: str | None,
    event_id: int | None,
    filename: str,
) -> None:
    """
    Called after a card is successfully created via /cardmaker.
    Saves the card data to cards.json and copies the image files.
    """
    _ensure_dirs()

    records = _load_records()

    records[player_name] = {
        "player_name": player_name,
        "card_name": card_name,
        "slug": slug,
        "codename": codename,
        "description": description,
        "bat_score": bat_score,
        "ball_score": ball_score,
        "rarity": rarity,
        "spawn_chance": spawn_chance,
        "artwork_author": artwork_author,
        "tradeable": tradeable,
        "spawnable": spawnable,
        "catch_name": catch_name or "",
        "event_id": event_id,
        "filename": filename,
    }

    CARDS_JSON.write_text(json.dumps(records, indent=2, ensure_ascii=False))

    src_card = MEDIA_DIR / filename
    if src_card.exists():
        shutil.copy2(str(src_card), str(IMAGES_DIR / filename))
        log.info("card_sync: exported card image %s", filename)

    fg_src = MEDIA_DIR / "foregrounds" / slug
    if fg_src.exists():
        shutil.copy2(str(fg_src), str(FOREGROUNDS_DIR / slug))
        log.info("card_sync: exported foreground preset %s", slug)

    log.info("card_sync: saved export for %r", player_name)


def _load_records() -> dict:
    if CARDS_JSON.exists():
        try:
            return json.loads(CARDS_JSON.read_text())
        except Exception:
            pass
    return {}


def import_all_cards() -> int:
    """
    Called at bot startup.  Reads card_exports/cards.json and creates any
    cards that are missing from the database.  Returns the number of cards
    imported.
    """
    records = _load_records()
    if not records:
        return 0

    import django
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "admin_panel.settings.cricstar")
    try:
        django.setup()
    except RuntimeError:
        pass

    from bd_models.models import Ball, Regime
    from django.db import connection

    imported = 0
    regime = Regime.objects.first()
    if regime is None:
        log.warning("card_sync: no Regime found — cannot import cards")
        return 0

    for player_name, data in records.items():
        if Ball.objects.filter(country=player_name).exists():
            log.debug("card_sync: %r already exists, skipping", player_name)
            continue

        filename = data["filename"]

        img_src = IMAGES_DIR / filename
        img_dst = MEDIA_DIR / filename
        if img_src.exists() and not img_dst.exists():
            img_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(img_src), str(img_dst))
            log.info("card_sync: restored image %s", filename)

        slug = data["slug"]
        fg_src = FOREGROUNDS_DIR / slug
        fg_dst = MEDIA_DIR / "foregrounds" / slug
        if fg_src.exists() and not fg_dst.exists():
            fg_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(fg_src), str(fg_dst))
            log.info("card_sync: restored foreground %s", slug)

        capacity_logic: dict = {"badge_rarity": data["rarity"]}
        event_id = data.get("event_id")
        if event_id:
            capacity_logic["forced_special"] = event_id

        Ball.objects.create(
            country=player_name,
            health=data["bat_score"],
            attack=data["ball_score"],
            rarity=data["spawn_chance"],
            emoji_id=0,
            wild_card=filename,
            collection_card=filename,
            credits=data["artwork_author"],
            capacity_name=data["codename"],
            capacity_description=data["description"],
            capacity_logic=capacity_logic,
            regime=regime,
            tradeable=data["tradeable"],
            spawnable=data["spawnable"],
            catch_names=data["catch_name"] or None,
        )
        log.info("card_sync: imported card %r", player_name)
        imported += 1

    return imported
