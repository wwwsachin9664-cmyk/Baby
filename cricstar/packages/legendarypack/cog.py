"""
Legendary Pack system for CricStar.

ONLY the bot owner can give Legendary Packs — they cannot be earned from
catches, daily rewards, or any other automatic source.

Pack pool — cards whose badge_rarity matches one of these tiers only:
    0.01 → weight 15
    0.03 → weight 25
    0.05 → weight 35
    0.08 → weight 55

Commands:
    /ownergivelegendarypack  — bot-owner-only: give legendary packs to a user
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
from asgiref.sync import sync_to_async
from discord import app_commands
from discord.ext import commands
from django.db import transaction

from cricstar.core.utils.availability import is_ball_obtainable
from cricstar.core.utils.checks import BOT_OWNER_ID
from bd_models.models import Ball, BallInstance, Player, Special, balls as balls_cache, specials
from settings.models import settings

if TYPE_CHECKING:
    from cricstar.core.bot import CricStarBot

log = logging.getLogger("cricstar.packages.legendarypack")

DATA_FILE = Path(__file__).parent.parent.parent.parent / "data" / "legendary_pack.json"
MEDIA_DIR = Path("admin_panel/media")
ASSETS_DIR = Path(__file__).parent / "assets"
PACK_COVER = ASSETS_DIR / "legendary_pack_cover.png"
PACK_GIF = ASSETS_DIR / "legendary_pack_open.gif"

PACK_EMOJI = "🔴"
PACK_LABEL = f"{PACK_EMOJI} Legendary Pack"

OPEN_TIMEOUT = 35
ANIMATION_DURATION = 4.0

LEGENDARY_RARITY_WEIGHTS: dict[float, float] = {
    0.01: 15,
    0.03: 25,
    0.05: 35,
    0.08: 55,
}

ALLOWED_TIERS = set(LEGENDARY_RARITY_WEIGHTS.keys())
TIER_EPSILON = 1e-4


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
    if value is None:
        return None
    for tier in ALLOWED_TIERS:
        if abs(value - tier) < TIER_EPSILON:
            return tier
    return None


# ── Storage ───────────────────────────────────────────────────────────────────

def _default_data() -> dict:
    return {"packs": {}}


def load_data() -> dict:
    if DATA_FILE.exists():
        try:
            data = json.loads(DATA_FILE.read_text())
        except Exception:
            data = _default_data()
    else:
        data = _default_data()
    data.setdefault("packs", {})
    return data


def save_data(data: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, indent=2))


def add_legendary_pack(user_id: int, count: int = 1) -> int:
    data = load_data()
    uid = str(user_id)
    new_total = int(data["packs"].get(uid, 0)) + count
    data["packs"][uid] = max(new_total, 0)
    save_data(data)
    return data["packs"][uid]


def consume_legendary_pack(user_id: int) -> bool:
    data = load_data()
    uid = str(user_id)
    have = int(data["packs"].get(uid, 0))
    if have <= 0:
        return False
    data["packs"][uid] = have - 1
    save_data(data)
    return True


def get_legendary_pack_count(user_id: int) -> int:
    data = load_data()
    return int(data["packs"].get(str(user_id), 0))


# ── Card picking ──────────────────────────────────────────────────────────────

def pick_legendary_pack_card() -> tuple[Ball, float] | None:
    """
    Pick a single card from the Legendary Pack pool.
    Only cards with badge_rarity in {0.01, 0.03, 0.05, 0.08} are eligible.
    """
    tiers: dict[float, list[Ball]] = {t: [] for t in LEGENDARY_RARITY_WEIGHTS}
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

    weights = [LEGENDARY_RARITY_WEIGHTS[t] for t in available]
    chosen_tier = random.choices(available, weights=weights, k=1)[0]
    return random.choice(tiers[chosen_tier]), chosen_tier


def _get_random_special() -> Special | None:
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


async def grant_card_from_legendary_pack(
    user: discord.abc.User, guild_id: int | None
) -> tuple[BallInstance, Ball, Special | None, bool, float] | None:
    picked = pick_legendary_pack_card()
    if not picked:
        return None
    card, tier = picked

    bonus_attack = random.randint(-settings.max_attack_bonus, settings.max_attack_bonus)
    bonus_health = random.randint(-settings.max_health_bonus, settings.max_health_bonus)

    forced_id = card.capacity_logic.get("forced_special") if card.capacity_logic else None
    if forced_id:
        special = specials.get(int(forced_id))
    else:
        special = _get_random_special()

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


# ── Animated opening view ─────────────────────────────────────────────────────

def _cover_embed() -> discord.Embed:
    embed = discord.Embed(
        title=PACK_LABEL,
        description="Press the button below to open your **Legendary Pack**.",
        color=0xB22222,
    )
    embed.set_image(url="attachment://legendary_pack_cover.png")
    embed.set_footer(text="Legendary Pack")
    return embed


def _opening_embed() -> discord.Embed:
    embed = discord.Embed(
        title=f"{PACK_LABEL} — Opening…",
        description="Hold tight, your legendary card is being revealed!",
        color=0xFF4500,
    )
    embed.set_image(url="attachment://legendary_pack_open.gif")
    embed.set_footer(text="Legendary Pack • Opening…")
    return embed


class OpenLegendaryPackView(discord.ui.View):
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

    @discord.ui.button(
        label="Open Legendary Pack",
        style=discord.ButtonStyle.danger,
        emoji="🔴",
    )
    async def open_button(
        self,
        interaction: discord.Interaction["CricStarBot"],
        button: discord.ui.Button,
    ):
        if self.opened:
            await interaction.response.send_message("Already opened!", ephemeral=True)
            return
        self.opened = True

        if not consume_legendary_pack(interaction.user.id):
            await interaction.response.send_message(
                "You don't have a Legendary Pack to open.", ephemeral=True
            )
            self.stop()
            return

        button.disabled = True
        button.label = "Opening…"

        try:
            await interaction.response.defer()
        except discord.HTTPException:
            log.exception("Failed to defer legendary pack-open interaction")

        gif_file = discord.File(str(PACK_GIF), filename="legendary_pack_open.gif")
        try:
            await interaction.edit_original_response(
                embed=_opening_embed(),
                view=self,
                attachments=[gif_file],
            )
        except discord.HTTPException:
            log.exception("Failed to swap to legendary opening GIF")

        await asyncio.sleep(ANIMATION_DURATION)

        result = await grant_card_from_legendary_pack(interaction.user, interaction.guild_id)
        if result is None:
            embed = discord.Embed(
                title="The pack was empty!",
                description="No legendary cards are currently obtainable. The pack has been refunded.",
                color=0xED4245,
            )
            add_legendary_pack(interaction.user.id, 1)
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
            title=f"🔴  You pulled {card.country}!",
            description=(
                f"{interaction.user.mention} opened a **{PACK_LABEL}** and revealed a legendary card!\n\n"
                f"**Card:** {card.country}\n"
                f"**ID:** `#{instance.pk:0X}`\n"
                f"**Stats:** `{instance.attack_bonus:+}% ATK / {instance.health_bonus:+}% HP`\n"
                f"**Rarity:** `{tier:g}`"
                f"{special_text}{new_text}"
            ),
            color=0xB22222,
        )
        embed.set_footer(text="Legendary Pack • Opened")

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
            log.exception("Failed to edit legendary final reveal message")

        self.stop()


# ── Cog ───────────────────────────────────────────────────────────────────────

class LegendaryPackCog(commands.Cog):
    """Legendary Pack: owner-only gifted packs with rare card tiers."""

    def __init__(self, bot: "CricStarBot"):
        self.bot = bot

    @app_commands.command(
        name="ownergivelegendarypack",
        description="(Bot owner only) Give a Legendary Pack to a user.",
    )
    @app_commands.describe(
        user="The user to receive the legendary pack",
        amount="How many packs to give (default 1)",
    )
    async def ownergivelegendarypack(
        self,
        interaction: discord.Interaction["CricStarBot"],
        user: discord.User,
        amount: app_commands.Range[int, 1, 100] = 1,
    ):
        is_owner = (
            interaction.user.id == BOT_OWNER_ID
            or await interaction.client.is_owner(interaction.user)
        )
        if not is_owner:
            await interaction.response.send_message(
                "Only the bot owner can give Legendary Packs.", ephemeral=True
            )
            return

        new_total = add_legendary_pack(user.id, amount)
        await interaction.response.send_message(
            f"✅ Gave **{amount}** Legendary Pack(s) to {user.mention}. "
            f"They now have **{new_total}** legendary pack(s).",
            ephemeral=True,
        )
        try:
            await user.send(
                f"🔴 The bot owner gifted you **{amount}** Legendary Pack(s)! "
                f"Use `/pack open` and choose **Legendary Pack** to open it."
            )
        except discord.HTTPException:
            pass
