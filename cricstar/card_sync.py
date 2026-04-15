"""
card_sync.py — Permanent persistence layer for CricStar.

Exports and imports:
  - Cards (Ball model)        → card_exports/cards.json + images + foregrounds + spawns
  - Special events (Special)  → card_exports/events.json
  - User holdings (BallInstance + Player) → card_exports/holdings.json
  - Guild configs (GuildConfig)           → card_exports/guild_configs.json

Everything in card_exports/ is committed to the repo, so the full bot state
survives Replit remixes / forks with zero manual work.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("cricstar.card_sync")

BASE_DIR      = Path(__file__).resolve().parent.parent
EXPORTS_DIR   = BASE_DIR / "card_exports"
IMAGES_DIR    = EXPORTS_DIR / "images"
SPAWNS_DIR    = EXPORTS_DIR / "spawns"
FOREGROUNDS_DIR = EXPORTS_DIR / "foregrounds"
CARDS_JSON        = EXPORTS_DIR / "cards.json"
EVENTS_JSON       = EXPORTS_DIR / "events.json"
HOLDINGS_JSON     = EXPORTS_DIR / "holdings.json"
GUILD_CONFIGS_JSON = EXPORTS_DIR / "guild_configs.json"
MEDIA_DIR     = BASE_DIR / "admin_panel" / "media"


def _ensure_dirs():
    for d in (IMAGES_DIR, SPAWNS_DIR, FOREGROUNDS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path) -> dict | list:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}


def _load_list_json(path: Path) -> list:
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------

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
    capacity_logic: dict | None = None,
    wild_card_filename: str | None = None,
    event_name: str | None = None,
    unobtainable: bool = False,
) -> None:
    """Called after /cardmaker creates or /editcard modifies a card."""
    _ensure_dirs()

    records = _load_json(CARDS_JSON)

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
        "unobtainable": unobtainable,
        "catch_name": catch_name or "",
        "event_id": event_id,
        "event_name": event_name or "",
        "filename": filename,
        "wild_card_filename": wild_card_filename or filename,
        "capacity_logic": capacity_logic or {},
    }

    CARDS_JSON.write_text(json.dumps(records, indent=2, ensure_ascii=False))

    src_card = MEDIA_DIR / filename
    if src_card.exists():
        shutil.copy2(str(src_card), str(IMAGES_DIR / filename))
        log.info("card_sync: exported collection image %s", filename)

    wc_filename = wild_card_filename or filename
    if wc_filename != filename:
        src_wc = MEDIA_DIR / wc_filename
        if src_wc.exists():
            shutil.copy2(str(src_wc), str(SPAWNS_DIR / wc_filename))
            log.info("card_sync: exported spawn image %s", wc_filename)
    else:
        src_card2 = MEDIA_DIR / filename
        if src_card2.exists():
            shutil.copy2(str(src_card2), str(SPAWNS_DIR / filename))

    fg_src = MEDIA_DIR / "foregrounds" / slug
    if fg_src.exists():
        shutil.copy2(str(fg_src), str(FOREGROUNDS_DIR / slug))
        log.info("card_sync: exported foreground preset %s", slug)

    log.info("card_sync: saved export for %r", player_name)


def has_custom_spawn_image(slug: str) -> bool:
    """Return True if a custom spawn image exists in card_exports/spawns/ for this slug."""
    _ensure_dirs()
    for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        if (SPAWNS_DIR / f"spawn_{slug}{ext}").exists():
            return True
    return False


def export_spawn_image(player_name: str, slug: str, src_path: str) -> str:
    """
    Persist a spawn image to card_exports/spawns/ and update wild_card_filename in cards.json.
    Returns the filename used (e.g. 'spawn_virat_kohli.jpg').
    """
    _ensure_dirs()
    src = Path(src_path)
    ext = src.suffix or ".jpg"
    filename = f"spawn_{slug}{ext}"
    dest = SPAWNS_DIR / filename
    shutil.copy2(str(src), str(dest))
    log.info("card_sync: exported spawn image %s for %r", filename, player_name)

    records = _load_json(CARDS_JSON)
    if player_name in records:
        records[player_name]["wild_card_filename"] = filename
        CARDS_JSON.write_text(json.dumps(records, indent=2, ensure_ascii=False))

    return filename


def remove_spawn_image(player_name: str, slug: str) -> str | None:
    """
    Delete a custom spawn image from card_exports/spawns/ and reset wild_card_filename in
    cards.json back to the collection image filename. Returns the removed filename or None.
    """
    _ensure_dirs()
    removed_filename: str | None = None

    for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        target = SPAWNS_DIR / f"spawn_{slug}{ext}"
        if target.exists():
            target.unlink()
            removed_filename = target.name
            log.info("card_sync: removed spawn image %s for %r", target.name, player_name)
            break

    records = _load_json(CARDS_JSON)
    if player_name in records:
        records[player_name]["wild_card_filename"] = records[player_name].get("filename")
        CARDS_JSON.write_text(json.dumps(records, indent=2, ensure_ascii=False))

    return removed_filename


def list_spawn_images() -> list[str]:
    """Return all custom spawn filenames (spawn_*.ext) in card_exports/spawns/."""
    _ensure_dirs()
    return sorted(
        f.name for f in SPAWNS_DIR.iterdir()
        if f.is_file() and f.name.startswith("spawn_")
    )


def delete_card_export(player_name: str, filename: str, wild_card_filename: str | None, slug: str) -> list[str]:
    """
    Remove a card from card_exports/ entirely.
    Called by /removecard so deleted cards don't get re-imported on next restart.
    Returns a list of paths that were removed.
    """
    _ensure_dirs()
    removed: list[str] = []

    records = _load_json(CARDS_JSON)
    if player_name in records:
        del records[player_name]
        CARDS_JSON.write_text(json.dumps(records, indent=2, ensure_ascii=False))
        removed.append("cards.json entry")
        log.info("card_sync: removed %r from cards.json", player_name)

    wc_filename = wild_card_filename or filename
    for target in [IMAGES_DIR / filename, SPAWNS_DIR / wc_filename, FOREGROUNDS_DIR / slug]:
        if target.exists():
            target.unlink()
            removed.append(str(target.name))
            log.info("card_sync: deleted export file %s", target)

    if wc_filename != filename:
        extra = SPAWNS_DIR / filename
        if extra.exists():
            extra.unlink()
            removed.append(str(extra.name))

    holdings_payload = _load_json(HOLDINGS_JSON)
    if isinstance(holdings_payload, dict) and "holdings" in holdings_payload:
        original = holdings_payload["holdings"]
        filtered = [h for h in original if h.get("ball_player_name") != player_name]
        if len(filtered) < len(original):
            holdings_payload["holdings"] = filtered
            holdings_payload["count"] = len(filtered)
            holdings_payload["exported_at"] = datetime.now(timezone.utc).isoformat()
            HOLDINGS_JSON.write_text(json.dumps(holdings_payload, indent=2, ensure_ascii=False))
            removed.append(f"holdings.json ({len(original) - len(filtered)} entries)")
            log.info("card_sync: removed %d holding(s) for %r from holdings.json",
                     len(original) - len(filtered), player_name)

    return removed


def import_all_cards() -> int:
    records = _load_json(CARDS_JSON)
    if not records:
        return 0

    import django
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "admin_panel.settings.cricstar")
    try:
        django.setup()
    except RuntimeError:
        pass

    from bd_models.models import Ball, Regime, Special

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
        wc_filename = data.get("wild_card_filename") or filename

        img_src = IMAGES_DIR / filename
        img_dst = MEDIA_DIR / filename
        if img_src.exists() and not img_dst.exists():
            img_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(img_src), str(img_dst))
            log.info("card_sync: restored collection image %s", filename)

        if wc_filename != filename:
            wc_src = SPAWNS_DIR / wc_filename
            wc_dst = MEDIA_DIR / wc_filename
            if wc_src.exists() and not wc_dst.exists():
                wc_dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(wc_src), str(wc_dst))
                log.info("card_sync: restored spawn image %s", wc_filename)
        else:
            spawn_src = SPAWNS_DIR / filename
            if spawn_src.exists() and not img_dst.exists():
                img_dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(spawn_src), str(img_dst))

        slug = data["slug"]
        fg_src = FOREGROUNDS_DIR / slug
        fg_dst = MEDIA_DIR / "foregrounds" / slug
        if fg_src.exists() and not fg_dst.exists():
            fg_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(fg_src), str(fg_dst))
            log.info("card_sync: restored foreground %s", slug)

        capacity_logic: dict = data.get("capacity_logic") or {}
        if not capacity_logic:
            capacity_logic = {"badge_rarity": data["rarity"]}

        event_name = data.get("event_name", "")
        event_id = data.get("event_id")
        if event_name:
            try:
                special = Special.objects.get(name=event_name)
                capacity_logic["forced_special"] = special.id
            except Special.DoesNotExist:
                if event_id:
                    capacity_logic["forced_special"] = event_id
        elif event_id:
            capacity_logic["forced_special"] = event_id

        Ball.objects.create(
            country=player_name,
            health=data["bat_score"],
            attack=data["ball_score"],
            rarity=data["spawn_chance"],
            emoji_id=0,
            wild_card=wc_filename,
            collection_card=filename,
            credits=data["artwork_author"],
            capacity_name=data["codename"],
            capacity_description=data["description"],
            capacity_logic=capacity_logic,
            regime=regime,
            tradeable=data["tradeable"],
            spawnable=data["spawnable"],
            unobtainable=data.get("unobtainable", False),
            catch_names=data["catch_name"] or None,
        )
        log.info("card_sync: imported card %r", player_name)
        imported += 1

    return imported


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def export_event(special) -> None:
    """Called after /createevent creates a new special event."""
    records = _load_json(EVENTS_JSON)

    records[special.name] = {
        "name": special.name,
        "catch_phrase": special.catch_phrase or "",
        "rarity": special.rarity,
        "emoji": special.emoji or "",
        "tradeable": special.tradeable,
        "hidden": special.hidden,
        "credits": special.credits or "",
        "start_date": special.start_date.isoformat() if special.start_date else None,
        "end_date": special.end_date.isoformat() if special.end_date else None,
    }

    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    EVENTS_JSON.write_text(json.dumps(records, indent=2, ensure_ascii=False))
    log.info("card_sync: exported event %r", special.name)


def import_all_events() -> int:
    records = _load_json(EVENTS_JSON)
    if not records:
        return 0

    import django
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "admin_panel.settings.cricstar")
    try:
        django.setup()
    except RuntimeError:
        pass

    from bd_models.models import Special

    imported = 0
    for name, data in records.items():
        if Special.objects.filter(name=name).exists():
            log.debug("card_sync: event %r already exists, skipping", name)
            continue

        start_date = None
        end_date = None
        if data.get("start_date"):
            try:
                start_date = datetime.fromisoformat(data["start_date"])
            except Exception:
                pass
        if data.get("end_date"):
            try:
                end_date = datetime.fromisoformat(data["end_date"])
            except Exception:
                pass

        Special.objects.create(
            name=name,
            catch_phrase=data.get("catch_phrase") or None,
            rarity=data.get("rarity", 0.05),
            emoji=data.get("emoji") or None,
            tradeable=data.get("tradeable", True),
            hidden=data.get("hidden", False),
            credits=data.get("credits") or None,
            start_date=start_date,
            end_date=end_date,
        )
        log.info("card_sync: imported event %r", name)
        imported += 1

    return imported


# ---------------------------------------------------------------------------
# Holdings (BallInstance + Player)
# ---------------------------------------------------------------------------

def export_all_holdings() -> int:
    """
    Exports every non-deleted BallInstance to card_exports/holdings.json.
    Called periodically so Replit checkpoints always capture the latest state.
    Returns the number of instances exported.
    """
    import django
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "admin_panel.settings.cricstar")
    try:
        django.setup()
    except RuntimeError:
        pass

    from bd_models.models import BallInstance, Player

    rows = []
    qs = (
        BallInstance.all_objects
        .filter(deleted=False)
        .select_related("ball", "player", "special")
    )
    for inst in qs:
        rows.append({
            "player_discord_id": inst.player.discord_id,
            "ball_player_name": inst.ball.country,
            "special_name": inst.special.name if inst.special else None,
            "health_bonus": inst.health_bonus,
            "attack_bonus": inst.attack_bonus,
            "favorite": inst.favorite,
            "tradeable": inst.tradeable,
            "server_id": inst.server_id,
            "catch_date": inst.catch_date.isoformat() if inst.catch_date else None,
            "spawned_time": inst.spawned_time.isoformat() if inst.spawned_time else None,
            "extra_data": inst.extra_data or {},
        })

    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "count": len(rows),
        "holdings": rows,
    }
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    HOLDINGS_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    log.info("card_sync: exported %d holding(s) to holdings.json", len(rows))
    return len(rows)


def import_all_holdings() -> int:
    """
    Imports holdings from card_exports/holdings.json.
    Only runs when the Player table is empty (fresh remix scenario).
    Returns the number of instances imported.
    """
    import django
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "admin_panel.settings.cricstar")
    try:
        django.setup()
    except RuntimeError:
        pass

    from bd_models.models import Ball, BallInstance, Player, Special

    if Player.objects.exists():
        log.debug("card_sync: Player table is populated — skipping holdings import")
        return 0

    payload = _load_json(HOLDINGS_JSON)
    if not payload or not isinstance(payload, dict):
        return 0

    rows = payload.get("holdings", [])
    if not rows:
        return 0

    ball_cache: dict[str, Ball] = {b.country: b for b in Ball.objects.all()}
    special_cache: dict[str, Special] = {s.name: s for s in Special.objects.all()}

    imported = 0
    for row in rows:
        discord_id = row.get("player_discord_id")
        ball_name = row.get("ball_player_name")
        if not discord_id or not ball_name:
            continue

        ball = ball_cache.get(ball_name)
        if ball is None:
            log.warning("card_sync: holdings import — ball %r not found, skipping", ball_name)
            continue

        player, _ = Player.objects.get_or_create(discord_id=discord_id)

        special = None
        special_name = row.get("special_name")
        if special_name:
            special = special_cache.get(special_name)

        catch_date = None
        if row.get("catch_date"):
            try:
                catch_date = datetime.fromisoformat(row["catch_date"])
            except Exception:
                pass

        spawned_time = None
        if row.get("spawned_time"):
            try:
                spawned_time = datetime.fromisoformat(row["spawned_time"])
            except Exception:
                pass

        BallInstance.objects.create(
            player=player,
            ball=ball,
            special=special,
            health_bonus=row.get("health_bonus", 0),
            attack_bonus=row.get("attack_bonus", 0),
            favorite=row.get("favorite", False),
            tradeable=row.get("tradeable", True),
            server_id=row.get("server_id"),
            spawned_time=spawned_time,
            extra_data=row.get("extra_data") or {},
        )
        imported += 1

    log.info("card_sync: imported %d holding(s) from holdings.json", imported)
    return imported


# ---------------------------------------------------------------------------
# Guild Configs (GuildConfig)
# ---------------------------------------------------------------------------

def export_guild_config(guild_id: int, spawn_channel: int | None, enabled: bool) -> None:
    """
    Save a single server's spawn config to card_exports/guild_configs.json.
    Called whenever a server sets or changes its spawn channel.
    """
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    records: dict = {}
    if GUILD_CONFIGS_JSON.exists():
        try:
            records = json.loads(GUILD_CONFIGS_JSON.read_text())
        except Exception:
            records = {}

    records[str(guild_id)] = {
        "guild_id": guild_id,
        "spawn_channel": spawn_channel,
        "enabled": enabled,
    }
    GUILD_CONFIGS_JSON.write_text(json.dumps(records, indent=2, ensure_ascii=False))
    log.info("card_sync: saved guild config for guild_id=%s", guild_id)


def export_all_guild_configs() -> int:
    """
    Export every GuildConfig row to card_exports/guild_configs.json.
    Called at startup so the file is always up to date.
    Returns the number of configs exported.
    """
    import django
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "admin_panel.settings.cricstar")
    try:
        django.setup()
    except RuntimeError:
        pass

    from bd_models.models import GuildConfig

    records: dict = {}
    for cfg in GuildConfig.objects.all():
        records[str(cfg.guild_id)] = {
            "guild_id": cfg.guild_id,
            "spawn_channel": cfg.spawn_channel,
            "enabled": cfg.enabled,
        }

    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    GUILD_CONFIGS_JSON.write_text(json.dumps(records, indent=2, ensure_ascii=False))
    log.info("card_sync: exported %d guild config(s) to guild_configs.json", len(records))
    return len(records)


def import_all_guild_configs() -> int:
    """
    Restore guild configs from card_exports/guild_configs.json.
    Only creates missing rows — never overwrites existing ones so live changes are preserved.
    Returns the number of configs imported.
    """
    if not GUILD_CONFIGS_JSON.exists():
        return 0

    try:
        records = json.loads(GUILD_CONFIGS_JSON.read_text())
    except Exception:
        return 0

    if not records:
        return 0

    import django
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "admin_panel.settings.cricstar")
    try:
        django.setup()
    except RuntimeError:
        pass

    from bd_models.models import GuildConfig

    imported = 0
    for key, data in records.items():
        guild_id = data.get("guild_id")
        if not guild_id:
            continue
        _, created = GuildConfig.objects.get_or_create(
            guild_id=guild_id,
            defaults={
                "spawn_channel": data.get("spawn_channel"),
                "enabled": data.get("enabled", False),
            },
        )
        if created:
            log.info("card_sync: restored guild config for guild_id=%s", guild_id)
            imported += 1

    return imported
