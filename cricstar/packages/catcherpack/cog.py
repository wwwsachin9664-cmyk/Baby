"""
Daily Catcher Pack system for CricStar.

Tracks how many cricketers each user catches per UTC day. The user with the most
catches at the end of the day automatically receives one "Daily Catcher Pack" in
their inventory. Packs can be opened with /csopen, the leaderboard is shown with
/catchleaderboard, and bot owners can grant a pack with /ownergivepack.

Pack contents — only cards whose badge rarity matches one of these tiers are in
the pool, picked by weight:
    0.2 -> 5,  0.3 -> 10, 0.4 -> 20, 0.5 -> 25,
    0.6 -> 45, 0.7 -> 70, 0.8 -> 90
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import date, datetime, time, timezone
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
MEDIA_DIR = Path("admin_panel/media")
ASSETS_DIR = Path(__file__).parent / "assets"
PACK_COVER = ASSETS_DIR / "pack_cover.png"
PACK_GIF = ASSETS_DIR / "pack_open.gif"

# Custom Discord emoji shown in front of "Daily Catcher Pack" everywhere
PACK_EMOJI = "<:catcherpack:1496344403537559703>"
PACK_LABEL = f"{PACK_EMOJI} Daily Catcher Pack"

# How long the Open button stays usable
OPEN_TIMEOUT = 35
# How long the opening animation runs (matches the GIF length)
ANIMATION_DURATION = 4.0

# Badge-rarity tier weights for the Daily Catcher Pack.
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

MIDNIGHT_UTC = time(hour=0, minute=0, tzinfo=timezone.utc)


# ── Storage helpers ──────────────────────────────────────────────────────────

_lock = asyncio.Lock()


def _today_str() -> str:
    return datetime.now(tz=timezone.utc).date().isoformat()


def _default_data() -> dict:
    return {"date": _today_str(), "catches": {}, "packs": {}, "last_winner": None}


def load_data() -> dict:
    if DATA_FILE.exists():
        try:
            data = json.loads(DATA_FILE.read_text())
        except Exception:
            data = _default_data()
    else:
        data = _default_data()
    data.setdefault("date", _today_str())
    data.setdefault("catches", {})
    data.setdefault("packs", {})
    data.setdefault("last_winner", None)
    return data


def save_data(data: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, indent=2))


def record_catch(user_id: int) -> None:
    """Record one catch for the given user for today. Safe to call from sync code."""
    try:
        data = load_data()
        today = _today_str()
        if data.get("date") != today:
            # day changed mid-flight; the daily task will handle awarding,
            # but keep counts fresh here too.
            data["date"] = today
            data["catches"] = {}
        uid = str(user_id)
        data["catches"][uid] = int(data["catches"].get(uid, 0)) + 1
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
    Pick a single card from the Daily Catcher Pack pool.

    Only cards whose badge_rarity exactly matches one of the allowed tiers
    (0.2/0.3/0.4/0.5/0.6/0.7/0.8) are eligible. Tiers are weighted by
    RARITY_WEIGHTS.

    Returns (ball, tier) or None if no cards qualify.
    """
    tiers: dict[float, list[Ball]] = {t: [] for t in RARITY_WEIGHTS}
    for ball in balls_cache.values():
        if not ball.enabled:
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
        description="A pack reserved for the day's top catcher. Press the button to open it.",
        color=0xE6B800,
    )
    embed.set_image(url="attachment://pack_cover.png")
    embed.set_footer(text="Daily Catcher Pack")
    return embed


def _opening_embed() -> discord.Embed:
    embed = discord.Embed(
        title=f"{PACK_LABEL} — Opening…",
        description="Hold tight, your card is being revealed!",
        color=0xFEE75C,
    )
    embed.set_image(url="attachment://pack_open.gif")
    embed.set_footer(text="Daily Catcher Pack • Opening…")
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
                "You don't have a Daily Catcher Pack to open.", ephemeral=True
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
                f"{interaction.user.mention} opened the **{PACK_LABEL}** and revealed a card!\n\n"
                f"**Card:** {card.country}\n"
                f"**ID:** `#{instance.pk:0X}`\n"
                f"**Stats:** `{instance.attack_bonus:+}% ATK / {instance.health_bonus:+}% HP`\n"
                f"**Rarity:** `{tier:g}`"
                f"{special_text}{new_text}"
            ),
            color=0x57F287,
        )
        embed.set_footer(text="Daily Catcher Pack • Opened")

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
    """Catcher Pack: daily catch competition and pack openings."""

    def __init__(self, bot: "CricStarBot"):
        self.bot = bot
        self.daily_award.start()
        self.catchup_task.start()

    def cog_unload(self):
        self.daily_award.cancel()
        self.catchup_task.cancel()

    # ── Background task: award winner at UTC midnight ────────────────────────

    @tasks.loop(time=MIDNIGHT_UTC)
    async def daily_award(self):
        await self._award_winner()

    @daily_award.before_loop
    async def _before_daily_award(self):
        await self.bot.wait_until_ready()

    # ── Catch-up task: handles missed midnights if the bot was offline ──────
    #
    # If the bot was down when UTC midnight passed, the scheduled task won't
    # fire for that day. On startup (and every hour as a safety net) we check
    # whether the stored counting date is older than today. If it is, we award
    # the winner for that older day's catches immediately, then reset.

    @tasks.loop(hours=1)
    async def catchup_task(self):
        data = load_data()
        stored_date = data.get("date")
        today = _today_str()
        if stored_date and stored_date != today and data.get("catches"):
            log.info(
                "Catcher Pack catch-up: awarding missed day %s (bot was offline at midnight).",
                stored_date,
            )
            await self._award_winner()
        elif stored_date and stored_date != today:
            # No catches recorded, just roll the date forward
            data["date"] = today
            data["catches"] = {}
            save_data(data)

    @catchup_task.before_loop
    async def _before_catchup(self):
        await self.bot.wait_until_ready()

    async def _award_winner(self):
        async with _lock:
            data = load_data()
            yesterday_catches = data.get("catches", {})
            if not yesterday_catches:
                # Roll the date and exit silently
                data["date"] = _today_str()
                save_data(data)
                return

            winner_id, count = max(yesterday_catches.items(), key=lambda kv: kv[1])
            try:
                winner_id_int = int(winner_id)
            except ValueError:
                return

            data["packs"][winner_id] = int(data["packs"].get(winner_id, 0)) + 1
            data["last_winner"] = {
                "user_id": winner_id_int,
                "catches": int(count),
                "date": data.get("date"),
            }
            data["date"] = _today_str()
            data["catches"] = {}
            save_data(data)

        # Try to DM the winner
        try:
            user = self.bot.get_user(winner_id_int) or await self.bot.fetch_user(winner_id_int)
            if user:
                try:
                    await user.send(
                        f"🏆 You topped yesterday's catch leaderboard with **{count}** catches!\n"
                        f"A **Daily Catcher Pack** has been added to your inventory.\n"
                        f"Use `/csopen` to open it."
                    )
                except discord.HTTPException:
                    pass
        except Exception:
            log.exception("Failed to notify catcher pack winner %s", winner_id_int)

        log.info("Awarded Catcher Pack to %s (%s catches)", winner_id_int, count)

    # ── /csopen ──────────────────────────────────────────────────────────────

    @app_commands.command(name="csopen", description="Open a Daily Catcher Pack from your inventory.")
    async def csopen(self, interaction: discord.Interaction["CricStarBot"]):
        data = load_data()
        have = int(data["packs"].get(str(interaction.user.id), 0))
        if have <= 0:
            # Silent ephemeral acknowledgement so only the user sees nothing in chat
            await interaction.response.send_message(
                "You don't own a Daily Catcher Pack.", ephemeral=True
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

        await interaction.response.send_message(embed=embed, view=view, file=cover_file)
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            view.message = None

    # ── /catchleaderboard ────────────────────────────────────────────────────

    @app_commands.command(
        name="catchleaderboard",
        description="Show today's top 10 catchers.",
    )
    async def catchleaderboard(self, interaction: discord.Interaction["CricStarBot"]):
        data = load_data()
        catches: dict[str, int] = data.get("catches", {})
        if not catches:
            await interaction.response.send_message(
                "No catches have been recorded today yet. Be the first!",
                ephemeral=True,
            )
            return

        ranked = sorted(catches.items(), key=lambda kv: kv[1], reverse=True)[:10]

        medals = ["🥇", "🥈", "🥉"] + ["🏏"] * 7
        lines = []
        for i, (uid, count) in enumerate(ranked):
            try:
                uid_int = int(uid)
            except ValueError:
                continue
            user = self.bot.get_user(uid_int)
            name = user.mention if user else f"<@{uid_int}>"
            lines.append(f"{medals[i]} **#{i+1}** — {name} • `{count}` catch{'es' if count != 1 else ''}")

        embed = discord.Embed(
            title="🏏 Catch Leaderboard — Today",
            description="\n".join(lines),
            color=0x5865F2,
        )
        last = data.get("last_winner")
        if last:
            embed.add_field(
                name="Yesterday's Daily Catcher Pack winner",
                value=f"<@{last['user_id']}> with `{last['catches']}` catches",
                inline=False,
            )
        embed.set_footer(text="Top catcher at UTC midnight wins a Daily Catcher Pack.")
        await interaction.response.send_message(embed=embed)

    # ── /ownergivepack ───────────────────────────────────────────────────────

    @app_commands.command(
        name="ownergivepack",
        description="(Bot owner only) Give a Daily Catcher Pack to a user.",
    )
    @app_commands.describe(user="The user to receive the pack", amount="How many packs to give (default 1)")
    async def ownergivepack(
        self,
        interaction: discord.Interaction["CricStarBot"],
        user: discord.User,
        amount: app_commands.Range[int, 1, 100] = 1,
    ):
        is_owner = interaction.user.id == BOT_OWNER_ID or await interaction.client.is_owner(interaction.user)
        if not is_owner:
            await interaction.response.send_message(
                "Only the bot owner can use this command.", ephemeral=True
            )
            return

        new_total = add_pack(user.id, amount)
        await interaction.response.send_message(
            f"✅ Gave **{amount}** Daily Catcher Pack(s) to {user.mention}. "
            f"They now have **{new_total}** pack(s).",
            ephemeral=True,
        )
        try:
            await user.send(
                f"🎁 The bot owner gifted you **{amount}** Daily Catcher Pack(s)! "
                f"Use `/csopen` to open."
            )
        except discord.HTTPException:
            pass

    # ── Owner-only manual trigger for the daily award (handy for testing) ───

    @app_commands.command(
        name="ownerforceaward",
        description="(Bot owner only) Force the Daily Catcher Pack award now.",
    )
    async def ownerforceaward(self, interaction: discord.Interaction["CricStarBot"]):
        is_owner = interaction.user.id == BOT_OWNER_ID or await interaction.client.is_owner(interaction.user)
        if not is_owner:
            await interaction.response.send_message(
                "Only the bot owner can use this command.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        await self._award_winner()
        await interaction.followup.send("Done.", ephemeral=True)
