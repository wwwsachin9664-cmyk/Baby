import asyncio
import json
import logging
import os
import random
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bd_models.models import Ball, BallInstance, Player, Special, balls as balls_cache, specials
from settings.models import settings

if TYPE_CHECKING:
    from cricstar.core.bot import CricStarBot

log = logging.getLogger("cricstar.packages.daily")

CLAIMS_FILE = Path(__file__).parent.parent.parent.parent / "data" / "claims.json"
MEDIA_DIR = Path(__file__).parent.parent.parent.parent / "admin_panel" / "media"

DAILY_TIERS = [
    (1.0,  5.0,  30),
    (5.0,  10.0, 60),
    (10.0, 20.0, 80),
]

SUSPENSE_STAGES = [
    (0x1a1a2e, "🎴  Shuffling the deck...",              "```\n▓░░░░░░░░░░░░░░  10%\n```"),
    (0x16213e, "🌟  The cricket gods are deciding...",   "```\n▓▓▓▓▓░░░░░░░░░░  35%\n```"),
    (0x0f3460, "⚡  Something incoming for you...",      "```\n▓▓▓▓▓▓▓▓▓░░░░░░  60%\n```"),
    (0x1b0044, "🏏  Finalising your destiny...",         "```\n▓▓▓▓▓▓▓▓▓▓▓▓░░░  85%\n```"),
    (0x2d0057, "🎯  Almost revealed...",                 "```\n▓▓▓▓▓▓▓▓▓▓▓▓▓▓░  95%\n```"),
]

RARITY_STAR_MAP = [
    (0.01,  "💎 MYTHIC"),
    (0.05,  "⭐⭐⭐⭐⭐ LEGENDARY"),
    (0.1,   "⭐⭐⭐⭐ EPIC"),
    (0.5,   "⭐⭐⭐ RARE"),
    (1.0,   "⭐⭐ UNCOMMON"),
    (5.0,   "⭐ COMMON"),
    (float("inf"), "✦ BASIC"),
]


def load_claims() -> dict:
    if CLAIMS_FILE.exists():
        try:
            return json.loads(CLAIMS_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_claims(data: dict):
    CLAIMS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CLAIMS_FILE.write_text(json.dumps(data, indent=2))


def get_rarity_label(rarity: float) -> str:
    for threshold, label in RARITY_STAR_MAP:
        if rarity <= threshold:
            return label
    return "✦ BASIC"


def pick_daily_card() -> Ball | None:
    all_balls = [b for b in balls_cache.values() if b.enabled and 1.0 <= b.rarity <= 20.0]
    if not all_balls:
        return None

    tier_pools = []
    tier_weights = []
    for lo, hi, weight in DAILY_TIERS:
        pool = [b for b in all_balls if lo <= b.rarity < hi]
        if pool:
            tier_pools.append(pool)
            tier_weights.append(weight)

    if not tier_pools:
        return random.choice(all_balls)

    chosen_pool = random.choices(tier_pools, weights=tier_weights, k=1)[0]
    return random.choice(chosen_pool)


def get_random_special() -> Special | None:
    from django.utils import timezone as dj_tz
    population = [
        x for x in specials.values()
        if (x.start_date or datetime.min.replace(tzinfo=dj_tz.get_current_timezone()))
        <= dj_tz.now()
        <= (x.end_date or datetime.max.replace(tzinfo=dj_tz.get_current_timezone()))
    ]
    if not population:
        return None
    common_weight = max(0.0, 1 - sum(x.rarity for x in population))
    weights = [x.rarity for x in population] + [common_weight]
    return random.choices(population + [None], weights=weights, k=1)[0]


class DailyCog(commands.Cog):
    def __init__(self, bot: "CricStarBot"):
        self.bot = bot

    @app_commands.command(name="csdaily", description="Claim your daily cricketer card!")
    @app_commands.guild_only()
    async def csdaily(self, interaction: discord.Interaction["CricStarBot"]):
        user_id = str(interaction.user.id)
        today = date.today().isoformat()

        claims = load_claims()
        daily_claims = claims.get("daily", {})

        if daily_claims.get(user_id) == today:
            next_midnight = datetime.combine(
                date.today(), datetime.min.time()
            ).replace(tzinfo=timezone.utc)
            next_midnight = next_midnight.replace(
                hour=0, minute=0, second=0
            )
            wait_embed = discord.Embed(
                title="⏳ Already Claimed!",
                description=(
                    f"You've already claimed your daily card today, {interaction.user.mention}!\n\n"
                    f"Come back **tomorrow** for your next card.\n"
                    f"Keep collecting and climbing the ranks! 🏏"
                ),
                color=0xff4444,
            )
            wait_embed.set_footer(text="CricStar Daily • Resets at midnight UTC")
            await interaction.response.send_message(embed=wait_embed, ephemeral=True)
            return

        await interaction.response.defer()

        card = pick_daily_card()
        if not card:
            await interaction.followup.send(
                "No cards available right now. Try again later!", ephemeral=True
            )
            return

        stage_embed = discord.Embed(
            title=SUSPENSE_STAGES[0][1],
            description=SUSPENSE_STAGES[0][2],
            color=SUSPENSE_STAGES[0][0],
        )
        stage_embed.set_footer(text="🎴 CricStar Daily Card")
        msg = await interaction.followup.send(embed=stage_embed)

        delays = [1.2, 1.4, 1.4, 1.2, 1.2]
        for i, (color, title, desc) in enumerate(SUSPENSE_STAGES[1:], start=1):
            await asyncio.sleep(delays[i - 1])
            e = discord.Embed(title=title, description=desc, color=color)
            e.set_footer(text="🎴 CricStar Daily Card")
            await msg.edit(embed=e)

        await asyncio.sleep(delays[-1])

        bonus_attack = random.randint(-settings.max_attack_bonus, settings.max_attack_bonus)
        bonus_health = random.randint(-settings.max_health_bonus, settings.max_health_bonus)
        special = get_random_special()

        player, _ = await Player.objects.aget_or_create(discord_id=interaction.user.id)
        is_new = not await BallInstance.objects.filter(player=player, ball=card).aexists()
        from django.utils import timezone as dj_tz
        ball_instance = await BallInstance.objects.acreate(
            ball=card,
            player=player,
            special=special,
            attack_bonus=bonus_attack,
            health_bonus=bonus_health,
            server_id=interaction.guild_id,
            catch_date=dj_tz.now(),
        )

        daily_claims[user_id] = today
        claims["daily"] = daily_claims
        save_claims(claims)

        rarity_label = get_rarity_label(card.rarity)
        new_badge = "🆕 **NEW to your collection!**\n" if is_new else ""
        special_text = f"✨ *{special.catch_phrase}*\n" if special and special.catch_phrase else ""

        reveal_embed = discord.Embed(
            title="🏏  Your Daily Cricketer!",
            description=(
                f"## {card.country}\n"
                f"{rarity_label}\n\n"
                f"{special_text}"
                f"{new_badge}"
                f"**ATK Bonus:** {bonus_attack:+}%  •  **HP Bonus:** {bonus_health:+}%\n"
                f"**Card ID:** `#{ball_instance.pk:0X}`"
            ),
            color=0xf5a623,
        )
        reveal_embed.set_footer(
            text=f"CricStar Daily • Come back tomorrow!",
            icon_url=interaction.user.display_avatar.url,
        )
        reveal_embed.set_author(
            name=f"{interaction.user.display_name}'s Daily Card",
            icon_url=interaction.user.display_avatar.url,
        )

        card_path = MEDIA_DIR / card.collection_card.name
        if card_path.exists():
            reveal_embed.set_image(url="attachment://card.png")
            await msg.delete()
            await interaction.followup.send(
                embed=reveal_embed,
                file=discord.File(str(card_path), filename="card.png"),
            )
        else:
            await msg.edit(embed=reveal_embed)

        log.info(
            f"{interaction.user} claimed daily card: {card.country} "
            f"(rarity={card.rarity}, special={special})"
        )
