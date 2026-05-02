"""
Catcher Pack system for CricStar.

Each user has a personal catch counter. Every catch increments it; when the
counter reaches CATCH_GOAL (30) the user is automatically awarded one Catcher
Pack and the counter resets to 0.

If a user does not catch anything for INACTIVITY_RESET_SECONDS (48 hours),
their progress is reset back to 0 the next time it is checked.

Pack contents — only cards whose badge_rarity matches one of these tiers are
in the pool, picked by weight (RARITY_WEIGHTS).

Commands:
    /csopen            — open a pack (animated)
    /catchleaderboard  — top 10 by current catch progress
    /ownergivepack     — bot-owner-only: give packs to a user
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cricstar.core.utils.availability import is_ball_obtainable
from cricstar.core.utils.checks import BOT_OWNER_ID
from bd_models.models import Ball, BallInstance, Player, Special, balls as balls_cache, specials
from settings.models import settings

if TYPE_CHECKING:
    from cricstar.core.bot import CricStarBot

log = logging.getLogger("cricstar.packages.catcherpack")

DATA_FILE = Path(__file__).parent.parent.parent.parent / "data" / "catcher_pack.json"
PACKABLE_FILE = Path(__file__).parent.parent.parent.parent / "data" / "pack_disabled.json"
MEDIA_DIR = Path("admin_panel/media")
ASSETS_DIR = Path(__file__).parent / "assets"
PACK_COVER = ASSETS_DIR / "pack_cover.png"
PACK_GIF = ASSETS_DIR / "pack_open.gif"

# Custom Discord emoji shown in front of "Catcher Pack" everywhere
PACK_EMOJI = "<:catcherpack:1496344403537559703>"
PACK_LABEL = f"{PACK_EMOJI} Catcher Pack"

# How long the Open button stays usable
OPEN_TIMEOUT = 35
# How long the opening animation runs (matches the GIF length)
ANIMATION_DURATION = 4.0

# How many catches a user needs to earn one pack
CATCH_GOAL = 30
# If a user does not catch anything for this many seconds, their progress
# resets back to 0.
INACTIVITY_RESET_SECONDS = 48 * 60 * 60  # 48 hours

# Badge-rarity tier weights for the Catcher Pack.
# Only cards whose badge_rarity matches one of these keys are in the pool.
RARITY_WEIGHTS: dict[float, float] = {
    0.2: 15,
    0.3: 20,
    0.4: 25,
    0.5: 45,
    0.6: 60,
    0.7: 65,
    0.8: 70,
}

ALLOWED_TIERS = set(RARITY_WEIGHTS.keys())
TIER_EPSILON = 1e-3  # tolerance for float equality


def _badge_rarity(ball: Ball) -> float | None:
    logic = ball.capacity_logic or {}
    raw = logic.get("badge_rarity")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _matching_tier(value: float | None) -> float | None:
    """Return the allowed tier equal to `value` (within tolerance), or None."""
    if value is None:
        return None
    for tier in ALLOWED_TIERS:
        if abs(value - tier) < TIER_EPSILON:
            return tier
    return None


# ── Storage helpers ──────────────────────────────────────────────────────────

_lock = asyncio.Lock()


def _now_ts() -> float:
    return datetime.now(tz=timezone.utc).timestamp()


def _default_data() -> dict:
    return {"progress": {}, "last_catch": {}, "packs": {}}


def load_data() -> dict:
    if DATA_FILE.exists():
        try:
            data = json.loads(DATA_FILE.read_text())
        except Exception:
            data = _default_data()
    else:
        data = _default_data()
    data.setdefault("progress", {})
    data.setdefault("last_catch", {})
    data.setdefault("packs", {})

    # Migration: legacy keys from the old daily-winner system are no longer
    # used. Carry over "packs" only and drop the rest silently.
    for legacy_key in ("date", "catches", "last_winner"):
        if legacy_key in data:
            data.pop(legacy_key, None)

    return data


def save_data(data: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, indent=2))


def _expire_if_inactive(data: dict, uid: str, now: float) -> None:
    """If the user's last catch was more than 48h ago, reset their progress."""
    last = data["last_catch"].get(uid)
    if last is None:
        return
    try:
        last_f = float(last)
    except (TypeError, ValueError):
        return
    if now - last_f >= INACTIVITY_RESET_SECONDS:
        if data["progress"].get(uid):
            log.info(
                "Catcher Pack: resetting progress for %s after %.1fh inactivity",
                uid,
                (now - last_f) / 3600,
            )
        data["progress"][uid] = 0
        # Forget the timestamp so we don't log the reset again next tick.
        data["last_catch"].pop(uid, None)


def record_catch(user_id: int) -> None:
    """
    Record one catch for the given user. Safe to call from sync code.

    - Resets the user's progress first if they have been inactive ≥48h.
    - Increments the user's catch progress.
    - When progress reaches CATCH_GOAL, awards 1 pack and resets to 0.
    """
    try:
        data = load_data()
        uid = str(user_id)
        now = _now_ts()

        _expire_if_inactive(data, uid, now)

        progress = int(data["progress"].get(uid, 0)) + 1
        data["last_catch"][uid] = now

        if progress >= CATCH_GOAL:
            data["packs"][uid] = int(data["packs"].get(uid, 0)) + 1
            data["progress"][uid] = 0
            log.info(
                "Catcher Pack: user %s completed %d catches and earned 1 pack",
                uid,
                CATCH_GOAL,
            )
        else:
            data["progress"][uid] = progress

        save_data(data)
    except Exception:
        log.exception("Failed to record catch for user %s", user_id)


def add_pack(user_id: int, count: int = 1) -> int:
    data = load_data()
    uid = str(user_id)
    new_total = int(data["packs"].get(uid, 0)) + count
    data["packs"][uid] = max(new_total, 0)
    save_data(data)
    return data["packs"][uid]


# ── Pack-eligibility (per-cricketer "packable" flag) ─────────────────────────
#
# A cricketer is packable by default. /packset can disable specific cricketers
# from appearing in any Catcher Pack. This file only tracks the EXPLICITLY
# disabled cards by ball ID; everything else is considered packable.

def load_disabled_ids() -> set[int]:
    if not PACKABLE_FILE.exists():
        return set()
    try:
        raw = json.loads(PACKABLE_FILE.read_text())
        return {int(x) for x in raw.get("disabled", [])}
    except Exception:
        return set()


def save_disabled_ids(ids: set[int]) -> None:
    PACKABLE_FILE.parent.mkdir(parents=True, exist_ok=True)
    PACKABLE_FILE.write_text(json.dumps({"disabled": sorted(ids)}, indent=2))


def set_packable(ball_id: int, packable: bool) -> bool:
    """
    Set whether a specific cricketer is allowed in Catcher Packs.
    Returns True if the state actually changed.
    """
    disabled = load_disabled_ids()
    if packable:
        if ball_id in disabled:
            disabled.discard(ball_id)
            save_disabled_ids(disabled)
            return True
        return False
    else:
        if ball_id not in disabled:
            disabled.add(ball_id)
            save_disabled_ids(disabled)
            return True
        return False


def is_packable(ball_id: int) -> bool:
    return ball_id not in load_disabled_ids()


def consume_pack(user_id: int) -> bool:
    data = load_data()
    uid = str(user_id)
    have = int(data["packs"].get(uid, 0))
    if have <= 0:
        return False
    data["packs"][uid] = have - 1
    save_data(data)
    return True


# ── Pack contents ────────────────────────────────────────────────────────────

def pick_pack_card() -> tuple[Ball, float] | None:
    """
    Pick a single card from the Catcher Pack pool.

    Only cards whose badge_rarity exactly matches one of the allowed tiers
    (0.2/0.3/0.4/0.5/0.6/0.7/0.8) are eligible. Tiers are weighted by
    RARITY_WEIGHTS.

    Returns (ball, tier) or None if no cards qualify.
    """
    disabled_ids = load_disabled_ids()
    tiers: dict[float, list[Ball]] = {t: [] for t in RARITY_WEIGHTS}
    for ball in balls_cache.values():
        if not ball.enabled:
            continue
        if ball.pk in disabled_ids:
            continue
        if not is_ball_obtainable(ball, specials.get):
            continue
        tier = _matching_tier(_badge_rarity(ball))
        if tier is None:
            continue
        tiers[tier].append(ball)

    available = [t for t, lst in tiers.items() if lst]
    if not available:
        return None

    weights = [RARITY_WEIGHTS[t] for t in available]
    chosen_tier = random.choices(available, weights=weights, k=1)[0]
    return random.choice(tiers[chosen_tier]), chosen_tier


def get_random_special() -> Special | None:
    from django.utils import timezone as dj_tz
    now = dj_tz.now()
    population = [
        x for x in specials.values()
        if x.start_date is not None
        and x.start_date <= now
        and (x.end_date is None or x.end_date >= now)
    ]
    if not population:
        return None
    common_weight = max(0.0, 1 - sum(x.rarity for x in population))
    weights = [x.rarity for x in population] + [common_weight]
    return random.choices(population + [None], weights=weights, k=1)[0]


async def grant_card_from_pack(user: discord.abc.User, guild_id: int | None) -> tuple[BallInstance, Ball, Special | None, bool, float] | None:
    picked = pick_pack_card()
    if not picked:
        return None
    card, tier = picked

    bonus_attack = random.randint(-settings.max_attack_bonus, settings.max_attack_bonus)
    bonus_health = random.randint(-settings.max_health_bonus, settings.max_health_bonus)

    forced_id = card.capacity_logic.get("forced_special") if card.capacity_logic else None
    if forced_id:
        special = specials.get(int(forced_id))
    else:
        special = get_random_special()

    player, _ = await Player.objects.aget_or_create(discord_id=user.id)
    is_new = not await BallInstance.objects.filter(player=player, ball=card).aexists()

    from django.utils import timezone as dj_tz
    instance = await BallInstance.objects.acreate(
        ball=card,
        player=player,
        special=special,
        attack_bonus=bonus_attack,
        health_bonus=bonus_health,
        server_id=guild_id,
        catch_date=dj_tz.now(),
    )
    return instance, card, special, is_new, tier


# ── Animated pack opening view ───────────────────────────────────────────────

def _cover_embed() -> discord.Embed:
    embed = discord.Embed(
        title=PACK_LABEL,
        description="Press the button below to open your pack.",
        color=0xE6B800,
    )
    embed.set_image(url="attachment://pack_cover.png")
    embed.set_footer(text="Catcher Pack")
    return embed


def _opening_embed() -> discord.Embed:
    embed = discord.Embed(
        title=f"{PACK_LABEL} — Opening…",
        description="Hold tight, your card is being revealed!",
        color=0xFEE75C,
    )
    embed.set_image(url="attachment://pack_open.gif")
    embed.set_footer(text="Catcher Pack • Opening…")
    return embed


class OpenPackView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=OPEN_TIMEOUT)
        self.user_id = user_id
        self.opened = False
        self.message: discord.Message | discord.InteractionMessage | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This pack isn't yours to open!", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        if self.opened:
            return
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Open Pack", style=discord.ButtonStyle.success, emoji="🎴")
    async def open_button(self, interaction: discord.Interaction["CricStarBot"], button: discord.ui.Button):
        if self.opened:
            await interaction.response.send_message("Already opened!", ephemeral=True)
            return
        self.opened = True

        # Make sure the user still owns a pack
        if not consume_pack(interaction.user.id):
            await interaction.response.send_message(
                "You don't have a Catcher Pack to open.", ephemeral=True
            )
            self.stop()
            return

        button.disabled = True
        button.label = "Opening…"

        # Defer FIRST so Discord doesn't timeout the interaction while we upload
        # the (large) GIF. We have 15 minutes after deferring.
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            log.exception("Failed to defer pack-open interaction")

        # Swap the cover image for the opening GIF via the deferred edit.
        gif_file = discord.File(str(PACK_GIF), filename="pack_open.gif")
        try:
            await interaction.edit_original_response(
                embed=_opening_embed(),
                view=self,
                attachments=[gif_file],
            )
        except discord.HTTPException:
            log.exception("Failed to swap to opening GIF")

        # Animation: hold the GIF on screen for its duration
        await asyncio.sleep(ANIMATION_DURATION)

        # Grant the card
        result = await grant_card_from_pack(interaction.user, interaction.guild_id)
        if result is None:
            embed = discord.Embed(
                title="The pack was empty!",
                description="No eligible cards are currently obtainable. The pack has been refunded.",
                color=0xED4245,
            )
            add_pack(interaction.user.id, 1)
            try:
                await interaction.edit_original_response(embed=embed, view=None, attachments=[])
            except discord.HTTPException:
                pass
            self.stop()
            return

        instance, card, special, is_new, tier = result

        special_text = ""
        if special and getattr(special, "catch_phrase", ""):
            special_text = f"\n✨ *{special.catch_phrase}*"
        new_text = "\n🆕 **New cricketer added to your collection!**" if is_new else ""

        embed = discord.Embed(
            title=f"🏏  You pulled {card.country}!",
            description=(
                f"{interaction.user.mention} opened a **{PACK_LABEL}** and revealed a card!\n\n"
                f"**Card:** {card.country}\n"
                f"**ID:** `#{instance.pk:0X}`\n"
                f"**Stats:** `{instance.attack_bonus:+}% ATK / {instance.health_bonus:+}% HP`\n"
                f"**Rarity:** `{tier:g}`"
                f"{special_text}{new_text}"
            ),
            color=0x57F287,
        )
        embed.set_footer(text="Catcher Pack • Opened")

        attachments: list[discord.File] = []
        if card.collection_card:
            card_path = MEDIA_DIR / str(card.collection_card)
            if card_path.exists():
                attachments.append(discord.File(str(card_path), filename=card_path.name))
                embed.set_image(url=f"attachment://{card_path.name}")

        try:
            await interaction.edit_original_response(
                embed=embed, view=None, attachments=attachments
            )
        except discord.HTTPException:
            log.exception("Failed to edit final reveal message")

        self.stop()


# ── Cog ──────────────────────────────────────────────────────────────────────

class CatcherPackCog(commands.Cog):
    """Catcher Pack: 30 catches earn one pack, reset after 48h of inactivity."""

    def __init__(self, bot: "CricStarBot"):
        self.bot = bot
        self.inactivity_sweep.start()

    def cog_unload(self):
        self.inactivity_sweep.cancel()

    # ── Background task: sweep stale progress every hour ────────────────────
    #
    # record_catch() already resets a user's progress on their next catch if
    # they've been inactive for 48h, but this loop ensures the leaderboard
    # reflects expirations even when the user doesn't catch anything new.

    @tasks.loop(hours=1)
    async def inactivity_sweep(self):
        try:
            async with _lock:
                data = load_data()
                now = _now_ts()
                changed = False
                for uid in list(data["progress"].keys()):
                    before = int(data["progress"].get(uid, 0))
                    _expire_if_inactive(data, uid, now)
                    if int(data["progress"].get(uid, 0)) != before:
                        changed = True
                if changed:
                    save_data(data)
        except Exception:
            log.exception("Catcher Pack inactivity sweep failed")

    @inactivity_sweep.before_loop
    async def _before_sweep(self):
        await self.bot.wait_until_ready()

    # ── /pack open ───────────────────────────────────────────────────────────

    pack_group = app_commands.Group(name="pack", description="Pack commands.")

    @pack_group.command(name="open", description="Open a pack from your inventory.")
    @app_commands.describe(packs="Which pack to open")
    @app_commands.choices(packs=[
        app_commands.Choice(name="Catcher Pack", value="catcher"),
        app_commands.Choice(name="Legendary Pack", value="legendary"),
    ])
    async def pack_open(self, interaction: discord.Interaction["CricStarBot"], packs: app_commands.Choice[str]):
        if packs.value == "legendary":
            await self._open_legendary_pack(interaction)
            return

        # ── Catcher Pack flow ─────────────────────────────────────────────
        await interaction.response.defer(thinking=True)

        data = load_data()
        have = int(data["packs"].get(str(interaction.user.id), 0))
        if have <= 0:
            await interaction.followup.send(
                "You don't own a Catcher Pack.", ephemeral=True
            )
            return

        embed = _cover_embed()
        embed.add_field(
            name="Owner",
            value=f"{interaction.user.mention} • {have} pack(s)",
            inline=False,
        )

        cover_file = discord.File(str(PACK_COVER), filename="pack_cover.png")
        view = OpenPackView(interaction.user.id)

        message = await interaction.followup.send(
            embed=embed, view=view, file=cover_file, wait=True
        )
        view.message = message

    async def _open_legendary_pack(self, interaction: discord.Interaction["CricStarBot"]):
        from cricstar.packages.legendarypack.cog import (
            OpenLegendaryPackView,
            get_legendary_pack_count,
            _cover_embed as legendary_cover_embed,
            PACK_COVER as LEGENDARY_PACK_COVER,
        )

        await interaction.response.defer(thinking=True)

        have = get_legendary_pack_count(interaction.user.id)
        if have <= 0:
            await interaction.followup.send(
                "You don't own a Legendary Pack.", ephemeral=True
            )
            return

        embed = legendary_cover_embed()
        embed.add_field(
            name="Owner",
            value=f"{interaction.user.mention} • {have} legendary pack(s)",
            inline=False,
        )

        cover_file = discord.File(str(LEGENDARY_PACK_COVER), filename="legendary_pack_cover.png")
        view = OpenLegendaryPackView(interaction.user.id)

        message = await interaction.followup.send(
            embed=embed, view=view, file=cover_file, wait=True
        )
        view.message = message

    # ── /catchleaderboard ────────────────────────────────────────────────────

    @app_commands.command(
        name="catchleaderboard",
        description=f"Show the top 10 catchers by progress toward a Catcher Pack ({CATCH_GOAL} catches).",
    )
    async def catchleaderboard(self, interaction: discord.Interaction["CricStarBot"]):
        # Sweep stale progress on demand so the leaderboard is always fresh
        async with _lock:
            data = load_data()
            now = _now_ts()
            changed = False
            for uid in list(data["progress"].keys()):
                before = int(data["progress"].get(uid, 0))
                _expire_if_inactive(data, uid, now)
                if int(data["progress"].get(uid, 0)) != before:
                    changed = True
            if changed:
                save_data(data)

        progress: dict[str, int] = {
            uid: int(c) for uid, c in data.get("progress", {}).items() if int(c) > 0
        }
        if not progress:
            await interaction.response.send_message(
                "No catches recorded yet. Be the first!",
                ephemeral=True,
            )
            return

        ranked = sorted(progress.items(), key=lambda kv: kv[1], reverse=True)[:10]
        medals = ["🥇", "🥈", "🥉"] + ["🏏"] * 7
        lines: list[str] = []
        for i, (uid, count) in enumerate(ranked):
            try:
                uid_int = int(uid)
            except ValueError:
                continue
            user = self.bot.get_user(uid_int)
            name = user.mention if user else f"<@{uid_int}>"
            lines.append(
                f"{medals[i]} **#{i+1}** — {name} • `{count}/{CATCH_GOAL}` catches"
            )

        embed = discord.Embed(
            title="🏏 Catch Leaderboard",
            description="\n".join(lines),
            color=0x5865F2,
        )
        embed.set_footer(
            text=f"{CATCH_GOAL} catches = 1 Catcher Pack • progress resets after 48h of inactivity"
        )
        await interaction.response.send_message(embed=embed)

    # ── /ownergivepack ───────────────────────────────────────────────────────

    @app_commands.command(
        name="ownergivepack",
        description="(Bot owner only) Give a Catcher Pack to a user.",
    )
    @app_commands.describe(user="The user to receive the pack", amount="How many packs to give (default 1)")
    async def ownergivepack(
        self,
        interaction: discord.Interaction["CricStarBot"],
        user: discord.User,
        amount: app_commands.Range[int, 1, 100] = 1,
    ):
        _GIVEPACK_ALLOWED = {BOT_OWNER_ID, 1325178465816936523}
        is_owner = (
            interaction.user.id in _GIVEPACK_ALLOWED
            or await interaction.client.is_owner(interaction.user)
        )
        if not is_owner:
            await interaction.response.send_message(
                "Only the bot owner can use this command.", ephemeral=True
            )
            return

        new_total = add_pack(user.id, amount)
        await interaction.response.send_message(
            f"✅ Gave **{amount}** Catcher Pack(s) to {user.mention}. "
            f"They now have **{new_total}** pack(s).",
            ephemeral=True,
        )
        try:
            await user.send(
                f"🎁 The bot owner gifted you **{amount}** Catcher Pack(s)! "
                f"Use `/pack open` to open."
            )
        except discord.HTTPException:
            pass

    # ── /packset ────────────────────────────────────────────────────────────
    #
    # Toggle whether a specific cricketer can appear in Catcher Packs.
    # This setting ONLY affects pack openings; it has no effect on spawns,
    # daily rewards, or weekly rewards.

    async def _packset_player_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        current_lower = current.lower()
        choices: list[app_commands.Choice[str]] = []
        # Sort by name for predictable autocomplete ordering
        for ball in sorted(balls_cache.values(), key=lambda b: b.country.lower()):
            if not getattr(ball, "country", None):
                continue
            if current_lower and current_lower not in ball.country.lower():
                continue
            choices.append(app_commands.Choice(name=ball.country, value=ball.country))
            if len(choices) >= 25:
                break
        return choices

    @app_commands.command(
        name="packset",
        description="(Bot owner only) Allow or disallow a cricketer in Catcher Packs.",
    )
    @app_commands.describe(
        player="The cricketer to configure",
        packable="True = can appear in packs, False = cannot appear in packs",
    )
    @app_commands.autocomplete(player=_packset_player_autocomplete)
    async def packset(
        self,
        interaction: discord.Interaction["CricStarBot"],
        player: str,
        packable: bool,
    ):
        is_owner = (
            interaction.user.id == BOT_OWNER_ID
            or await interaction.client.is_owner(interaction.user)
        )
        if not is_owner:
            await interaction.response.send_message(
                "Only the bot owner can use this command.", ephemeral=True
            )
            return

        # Resolve the cricketer by exact (case-insensitive) name
        target: Ball | None = None
        for ball in balls_cache.values():
            if ball.country and ball.country.lower() == player.lower():
                target = ball
                break

        if target is None:
            await interaction.response.send_message(
                f"No cricketer named **{player}** was found.", ephemeral=True
            )
            return

        changed = set_packable(target.pk, packable)
        state = "✅ packable" if packable else "🚫 NOT packable"
        if changed:
            msg = f"{state} — **{target.country}** updated."
        else:
            msg = f"{state} — **{target.country}** was already in that state."
        await interaction.response.send_message(msg, ephemeral=True)
