from __future__ import annotations

from typing import Callable

from django.utils import timezone


def is_special_active(special, now=None) -> bool:
    current = now or timezone.now()
    if special.start_date and special.start_date > current:
        return False
    if special.end_date and special.end_date < current:
        return False
    return True


def is_ball_obtainable(ball, special_lookup: Callable[[int], object | None] | None = None, now=None) -> bool:
    if getattr(ball, "unobtainable", False):
        return False

    logic = ball.capacity_logic or {}
    forced_id = logic.get("forced_special")
    if not forced_id:
        return True

    lookup = special_lookup or (lambda _id: None)
    try:
        special = lookup(int(forced_id))
    except (TypeError, ValueError):
        return not logic.get("only_spawn_in_event")

    if not special:
        return not logic.get("only_spawn_in_event")

    has_window = bool(getattr(special, "start_date", None) or getattr(special, "end_date", None))
    if logic.get("only_spawn_in_event") or has_window:
        return is_special_active(special, now=now)

    return True