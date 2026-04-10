"""
Emoji registry for CricStar.

Emojis are stored in admin_panel/media/emojis.json as a list of objects:
  [{"id": "1234567890", "player": "Virat Kohli", "list": true, "bet": true}, ...]

- "id"     : Discord emoji ID (string of digits) or a raw unicode character
- "player" : optional player name — if set, this emoji is shown next to that player
- "list"   : if true, the emoji may appear randomly in /list output
- "bet"    : if true, the emoji may appear randomly next to cards in bet display
"""

from __future__ import annotations

import json
import logging
import random
from pathlib import Path

log = logging.getLogger(__name__)

EMOJIS_PATH = Path("admin_panel/media/emojis.json")


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
) -> None:
    """Add or update an emoji entry."""
    data = _load()
    for entry in data:
        if entry["id"] == emoji_id:
            entry["list"] = show_in_list
            entry["bet"] = show_in_bet
            entry["player"] = player_name.strip()
            _save(data)
            return
    data.append({
        "id": emoji_id,
        "list": show_in_list,
        "bet": show_in_bet,
        "player": player_name.strip(),
    })
    _save(data)


def remove_emoji(emoji_id: str) -> bool:
    """Remove an emoji by ID. Returns True if it was found and removed."""
    data = _load()
    new_data = [e for e in data if e["id"] != emoji_id]
    if len(new_data) == len(data):
        return False
    _save(new_data)
    return True


def get_player_emoji(player_name: str) -> str:
    """
    Return the formatted emoji string for a specific player, or empty string.
    Lookup is case-insensitive.
    Only returns unicode / text emojis safely. For Discord custom emoji IDs use
    get_player_emoji_id() instead.
    """
    name_lower = player_name.strip().lower()
    if not name_lower:
        return ""
    for entry in _load():
        p = entry.get("player", "").strip().lower()
        if p and p == name_lower:
            eid = entry["id"]
            if eid.isdigit():
                return f"<:e:{eid}> "
            return f"{eid} "
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
    return _fmt_id(random.choice(bet_emojis)["id"])


def get_random_list_emoji() -> str:
    """Return a random list emoji string, or empty string if none configured."""
    list_emojis = [e for e in _load() if e.get("list")]
    if not list_emojis:
        return ""
    return _fmt_id(random.choice(list_emojis)["id"])


def _fmt_id(eid: str) -> str:
    """Format a raw emoji ID or unicode char into a Discord-renderable string."""
    if eid.isdigit():
        return f"<:e:{eid}> "
    return f"{eid} "


def format_emoji(entry: dict) -> str:
    """Format an emoji entry for display in Discord (no trailing space)."""
    eid = entry["id"]
    if eid.isdigit():
        return f"<:e:{eid}>"
    return eid


def get_player_emoji_map() -> dict[str, str]:
    """Return a dict mapping player_name_lower -> emoji_id_str for all player-linked emojis."""
    return {
        e["player"].strip().lower(): e["id"]
        for e in _load()
        if e.get("player", "").strip()
    }


def parse_emoji_input(raw: str) -> str:
    """
    Accept a Discord emoji in any format and return the bare ID or unicode char.
    Handles: '<:name:123456>', '<a:name:123456>', '123456', or a raw unicode emoji.
    """
    import re
    raw = raw.strip()
    m = re.match(r"<a?:[\w~]+:(\d+)>", raw)
    if m:
        return m.group(1)
    return raw
