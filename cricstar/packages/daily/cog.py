import json
import logging
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

# Rarity -> weight mapping (only 1.0 to 20.0 allowed)
RARITY_WEIGHTS = {
    1.0:  4,
    1.5:  6,
    2.0:  7,
    2.5:  9,
    3.0:  12,
    4.0:  13,
    5.0:  15,
    6.0:  18,
    7.0:  19,
    8.0:  20,
    9.0:  22,
}
# 10.0 to 15.0 = weight 60
# 16.0 to 20.0 = weight 90


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


def get_rarity_weight(rarity: float) -> int:
    if rarity in RARITY_WEIGHTS:
        return RARITY_WEIGHTS[rarity]
    if 10.0 <= rarity <= 15.0:
        return 60
    if 16.0 <= rarity <= 20.0:
        return 90
    return 0


def pick_daily_card() -> Ball | None:
    # Only cards with rarity 1.0 to 20.0 (never below 1.0)
    eligible = [
        b for b in balls_cache.values()
        if b.enabled and 1.0 <= b.rarity <= 20.0
    ]
    if not eligible:
        return None

    weights = [get_rarity_weight(b.rarity) for b in eligible]
    # Filter out zero-weight cards
    filtered = [(b, w) for b, w in zip(eligible, weights) if w > 0]
    if not filtered:
        return None

    balls_list, weights_list = zip(*filtered)
    return random.choices(balls_list, weights=weights_list, k=1)[0]


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
            await interaction.response.send_message(
                f"You've already claimed your daily card today, {interaction.user.mention}!\n"
                f"Come back **tomorrow** for your next card. 🏏",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        card = pick_daily_card()
        if not card:
            await interaction.followup.send(
                "No cards available right now. Try again later!", ephemeral=True
            )
            return

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

        special_text = f"✨ *{special.catch_phrase}*\n\n" if special and special.catch_phrase else ""
        new_badge = "This is a **new cricketer** that has been added to your completion!" if is_new else ""

        message = (
            f"{interaction.user.mention} You packed **{card.country}**! "
            f"`#{ball_instance.pk:0X}, {bonus_attack:+}%, {bonus_health:+}%`\n\n"
            f"{special_text}"
            f"{new_badge}"
        )

        await interaction.followup.send(message)

        log.info(
            f"{interaction.user} claimed daily card: {card.country} "
            f"(rarity={card.rarity}, special={special})"
        )


async def setup(bot):
    await bot.add_cog(DailyCog(bot))
    