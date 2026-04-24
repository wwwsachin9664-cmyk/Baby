import json
import logging
from pathlib import Path
from typing import Union

from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True

log = logging.getLogger("cricstar.foreground_border")

OVERRIDES_PATH = Path("data/foreground_border_overrides.json")

DEFAULT_BORDER = 5
STEP = 2
MIN_BORDER = 0
MAX_BORDER = 60


def _load_raw() -> dict:
    if not OVERRIDES_PATH.exists():
        return {}
    try:
        with OVERRIDES_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception as e:
        log.warning("Failed to load %s: %s", OVERRIDES_PATH, e)
    return {}


def _save_raw(data: dict) -> None:
    OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OVERRIDES_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(OVERRIDES_PATH)


def get_override(ball_id: int) -> Union[int, str, None]:
    """Return stored override for a ball: int px, "auto", or None if not set."""
    raw = _load_raw()
    val = raw.get(str(ball_id))
    if val is None:
        return None
    if isinstance(val, str) and val.lower() == "auto":
        return "auto"
    try:
        ival = int(val)
        return max(MIN_BORDER, min(MAX_BORDER, ival))
    except (TypeError, ValueError):
        return None


def set_override(ball_id: int, value: Union[int, str]) -> None:
    raw = _load_raw()
    if isinstance(value, str) and value.lower() == "auto":
        raw[str(ball_id)] = "auto"
    else:
        ival = max(MIN_BORDER, min(MAX_BORDER, int(value)))
        raw[str(ball_id)] = ival
    _save_raw(raw)


def bump_override(ball_id: int, delta: int) -> int:
    """Adjust the stored border by `delta` px. Returns the new value."""
    current = get_override(ball_id)
    if current is None or current == "auto":
        base = DEFAULT_BORDER
    else:
        base = int(current)
    new_val = max(MIN_BORDER, min(MAX_BORDER, base + delta))
    set_override(ball_id, new_val)
    return new_val


def compute_auto_border(foreground_path) -> int:
    """Pick a sensible border thickness from the foreground image's smaller side."""
    try:
        with Image.open(str(foreground_path)) as img:
            w, h = img.size
    except Exception as e:
        log.warning("compute_auto_border: cannot read %s: %s", foreground_path, e)
        return DEFAULT_BORDER
    smaller = min(w, h)
    border = round(smaller / 200)
    return max(2, min(border, MAX_BORDER))


def resolve_border(ball_id: int, foreground_path) -> int:
    """Return the actual border thickness in pixels for this player."""
    val = get_override(ball_id)
    if val is None:
        return DEFAULT_BORDER
    if val == "auto":
        return compute_auto_border(foreground_path)
    return int(val)


def describe_override(ball_id: int) -> str:
    """Human-readable description of what's stored for this player."""
    val = get_override(ball_id)
    if val is None:
        return f"default ({DEFAULT_BORDER}px)"
    if val == "auto":
        return "auto"
    return f"{int(val)}px"
