"""
Emoji registry for CricStar.

Emojis are stored in admin_panel/media/emojis.json as a list of objects:
  [{"id": "1234567890", "name": "dhoni", "player": "Virat Kohli", "list": true, "bet": true}, ...]

- "id"     : Discord emoji ID (string of digits) or a raw unicode character
- "name"   : Discord emoji name (e.g. "dhoni") — used to render <:dhoni:ID>
- "player" : optional player name — if set, this emoji is shown next to that player
- "list"   : if true, the emoji may appear randomly in /list output
- "bet"    : if true, the emoji may appear randomly next to cards in bet display
"""

from __future__ import annotations

import json
import logging
import random
import re
from pathlib import Path

log = logging.getLogger(__name__)

EMOJIS_PATH = Path("admin_panel/media/emojis.json")


def player_name_to_emoji_name(player_name: str) -> str:
    """
    Convert a player name to a valid Discord emoji name.
    Discord emoji names may only contain letters, digits, and underscores (max 32 chars).
    Example: 'MS DHONI' -> 'MS_DHONI', 'Virat Kohli (IPL2026)' -> 'Virat_Kohli_IPL2026'
    """
    name = re.sub(r"[^\w]", "_", player_name.strip())
    name = re.sub(r"_+", "_", name)
    name = name.strip("_")
    return name[:32] if name else "e"


def _load() -> list[dict]:
    if not EMOJIS_PATH.exists():
        return []
    try:
        return json.loads(EMOJIS_PATH.read_text())
    except Exception:
        log.exception("Failed to load emojis.json")
        return []


def _save(data: list[dict]) -> None:
    EMOJIS_PATH.parent.mkdir(parents=True, exist_ok=True)
    EMOJIS_PATH.write_text(json.dumps(data, indent=2))


def list_emojis() -> list[dict]:
    """Return all registered emojis."""
    return _load()


def add_emoji(
    emoji_id: str,
    *,
    show_in_list: bool,
    show_in_bet: bool,
    player_name: str = "",
    emoji_name: str = "",
) -> None:
    """Add or update an emoji entry."""
    resolved_name = emoji_name.strip()
    if not resolved_name and player_name.strip():
        resolved_name = player_name_to_emoji_name(player_name.strip())

    data = _load()
    for entry in data:
        if entry["id"] == emoji_id:
            entry["list"] = show_in_list
            entry["bet"] = show_in_bet
            entry["player"] = player_name.strip()
            if resolved_name:
                entry["name"] = resolved_name
            _save(data)
            return
    record: dict = {
        "id": emoji_id,
        "list": show_in_list,
        "bet": show_in_bet,
        "player": player_name.strip(),
    }
    if resolved_name:
        record["name"] = resolved_name
    data.append(record)
    _save(data)


def remove_emoji(emoji_id: str) -> bool:
    """Remove an emoji by ID. Returns True if it was found and removed."""
    data = _load()
    new_data = [e for e in data if e["id"] != emoji_id]
    if len(new_data) == len(data):
        return False
    _save(new_data)
    return True


def _resolve_name(entry: dict) -> str:
    """
    Get the Discord emoji name for an entry.
    Uses the stored 'name' field, or falls back to deriving it from 'player'.
    """
    stored = entry.get("name", "").strip()
    if stored:
        return stored
    player = entry.get("player", "").strip()
    if player:
        return player_name_to_emoji_name(player)
    return "e"


def _fmt_id(eid: str, ename: str = "") -> str:
    """Format a raw emoji ID or unicode char into a Discord-renderable string."""
    if eid.isdigit():
        name = ename.strip() if ename.strip() else "e"
        return f"<:{name}:{eid}> "
    return f"{eid} "


def get_player_emoji(player_name: str) -> str:
    """
    Return the formatted emoji string for a specific player, or empty string.
    Lookup is case-insensitive.
    """
    name_lower = player_name.strip().lower()
    if not name_lower:
        return ""
    for entry in _load():
        p = entry.get("player", "").strip().lower()
        if p and p == name_lower:
            eid = entry["id"]
            ename = _resolve_name(entry)
            return _fmt_id(eid, ename)
    return ""


def get_player_emoji_id(player_name: str) -> str | None:
    """
    Return the raw emoji ID string for a player's linked emoji, or None.
    Returns the numeric ID string if it is a custom Discord emoji, else the
    unicode character, else None.
    """
    name_lower = player_name.strip().lower()
    if not name_lower:
        return None
    for entry in _load():
        p = entry.get("player", "").strip().lower()
        if p and p == name_lower:
            return entry["id"]
    return None


def get_random_bet_emoji() -> str:
    """Return a random bet emoji string, or empty string if none configured."""
    bet_emojis = [e for e in _load() if e.get("bet")]
    if not bet_emojis:
        return ""
    entry = random.choice(bet_emojis)
    return _fmt_id(entry["id"], _resolve_name(entry))


def get_random_list_emoji() -> str:
    """Return a random list emoji string, or empty string if none configured."""
    list_emojis_data = [e for e in _load() if e.get("list")]
    if not list_emojis_data:
        return ""
    entry = random.choice(list_emojis_data)
    return _fmt_id(entry["id"], _resolve_name(entry))


def format_emoji(entry: dict) -> str:
    """Format an emoji entry for display in Discord (no trailing space)."""
    eid = entry["id"]
    if eid.isdigit():
        name = _resolve_name(entry)
        return f"<:{name}:{eid}>"
    return eid


def get_player_emoji_map() -> dict[str, str]:
    """
    Return a dict mapping player_name_lower -> full formatted emoji string
    for all player-linked emojis. The value is ready to embed directly in text.
    Example: {"virat kohli": "<:Virat_Kohli:123456>"}
    """
    result: dict[str, str] = {}
    for e in _load():
        player = e.get("player", "").strip()
        if not player:
            continue
        eid = e["id"]
        ename = _resolve_name(e)
        result[player.lower()] = _fmt_id(eid, ename).strip()
    return result


def parse_emoji_input(raw: str) -> tuple[str, str]:
    """
    Accept a Discord emoji in any format and return (emoji_name, emoji_id_or_char).
    Handles: '<:name:123456>', '<a:name:123456>', '123456', or a raw unicode emoji.
    Returns (name, id) where name may be empty for raw IDs / unicode chars.
    """
    raw = raw.strip()
    m = re.match(r"<a?:([\w~]+):(\d+)>", raw)
    if m:
        return m.group(1), m.group(2)
    return "", raw


def backfill_emoji_names() -> int:
    """
    Add the 'name' field to any existing emoji entries that lack it,
    deriving it from the 'player' field. Returns count of updated entries.
    """
    data = _load()
    updated = 0
    for entry in data:
        if not entry.get("name"):
            player = entry.get("player", "").strip()
            if player:
                entry["name"] = player_name_to_emoji_name(player)
                updated += 1
    if updated:
        _save(data)
    return updated
