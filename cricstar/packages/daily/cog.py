import json
import logging
import random
from datetime import date, datetime, timedelta, timezone
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

CLAIMS_FILE  = Path(__file__).parent.parent.parent.parent / "data" / "claims.json"
MEDIA_DIR    = Path("admin_panel/media")
WEEKLY_DAYS  = 7


# ── Rarity weight tables ──────────────────────────────────────────────────────

WEEKLY_BADGE_RARITY_WEIGHTS = {
    0.2: 1.0,
    0.5: 2.0,
    1.0: 90.0,
    1.5: 20.0,
    5.0: 20.0,
}


def _badge_rarity(ball: Ball) -> float | None:
    logic = ball.capacity_logic or {}
    raw = logic.get("badge_rarity")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


# Daily weights by spawn_chance tier
#   < 1%             → 0   (never — too rare for daily)
#   1  – 5%          → 2   (rare cards, very small daily chance)
#   6  – 15%         → 15  (medium-rare, occasional)
#   16 – 35%         → 50  (common, frequently given in daily)
#   36 – 100%        → 85  (very common, most frequent daily reward)
def _get_daily_weight(rarity: float) -> float:
    if rarity < 1.0:
        return 0.0
    if 1.0 <= rarity <= 5.0:
        return 2.0
    if 6.0 <= rarity <= 15.0:
        return 15.0
    if 16.0 <= rarity <= 35.0:
        return 50.0
    if rarity > 35.0:
        return 85.0
    return 0.0


# ── Claims file helpers ───────────────────────────────────────────────────────

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


def reset_all_cooldowns():
    """Clear every daily and weekly cooldown (used by /csresetcooldown)."""
    claims = load_claims()
    claims["daily"]  = {}
    claims["weekly"] = {}
    save_claims(claims)


# ── Card pickers ─────────────────────────────────────────────────────────────

def pick_daily_card() -> Ball | None:
    eligible = [b for b in balls_cache.values() if b.enabled]
    if not eligible:
        return None
    filtered = [(b, _get_daily_weight(b.rarity)) for b in eligible]
    filtered = [(b, w) for b, w in filtered if w > 0]
    if not filtered:
        return None
    balls_list, weights_list = zip(*filtered)
    return random.choices(balls_list, weights=weights_list, k=1)[0]


def pick_weekly_card() -> Ball | None:
    eligible = [b for b in balls_cache.values() if b.enabled]
    if not eligible:
        return None
    grouped: dict[float, list[Ball]] = {}
    for ball in eligible:
        badge_rarity = _badge_rarity(ball)
        if badge_rarity not in WEEKLY_BADGE_RARITY_WEIGHTS:
            continue
        grouped.setdefault(badge_rarity, []).append(ball)
    if not grouped:
        return None
    rarity_tiers = list(grouped.keys())
    weights = [WEEKLY_BADGE_RARITY_WEIGHTS[tier] for tier in rarity_tiers]
    selected_tier = random.choices(rarity_tiers, weights=weights, k=1)[0]
    return random.choice(grouped[selected_tier])


# ── Special helper ────────────────────────────────────────────────────────────

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


# ── Shared card-claim logic ───────────────────────────────────────────────────

async def _give_card(
    interaction: discord.Interaction,
    card: Ball,
    label: str,          # "daily" or "weekly"
    include_special: bool = True,
) -> discord.File | None:
    """Create a BallInstance and return the card PNG as a discord.File."""
    bonus_attack = random.randint(-settings.max_attack_bonus, settings.max_attack_bonus)
    bonus_health = random.randint(-settings.max_health_bonus, settings.max_health_bonus)

    forced_id = card.capacity_logic.get("forced_special") if card.capacity_logic else None
    if forced_id:
        special = specials.get(int(forced_id)) or None
    else:
        special = get_random_special() if include_special else None

    player, _ = await Player.objects.aget_or_create(discord_id=interaction.user.id)
    is_new    = not await BallInstance.objects.filter(player=player, ball=card).aexists()

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

    special_text = f"✨ *{special.catch_phrase}*\n\n" if special and special.catch_phrase else ""
    new_badge    = "This is a **new cricketer** added to your collection!" if is_new else ""

    message = (
        f"{interaction.user.mention} You packed **{card.country}**! "
        f"`#{ball_instance.pk:0X}, {bonus_attack:+}%, {bonus_health:+}%`\n\n"
        f"{special_text}"
        f"{new_badge}"
    )

    # Attach the card image
    card_file: discord.File | None = None
    card_path = MEDIA_DIR / str(card.collection_card)
    if card_path.exists():
        card_file = discord.File(str(card_path), filename=card_path.name)

    await interaction.followup.send(content=message, file=card_file) if card_file else \
        await interaction.followup.send(content=message)

    log.info(
        "%s claimed %s card: %s (rarity=%.2f, special=%s)",
        interaction.user, label, card.country, card.rarity, special,
    )

    return card_file


# ── Cog ───────────────────────────────────────────────────────────────────────

class DailyCog(commands.Cog):
    def __init__(self, bot: "CricStarBot"):
        self.bot = bot

    # ── /csdaily ─────────────────────────────────────────────────────────────
    @app_commands.command(name="csdaily", description="Claim your daily cricketer card!")
    @app_commands.guild_only()
    async def csdaily(self, interaction: discord.Interaction["CricStarBot"]):
        user_id = str(interaction.user.id)
        today   = date.today().isoformat()

        claims       = load_claims()
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
            await interaction.followup.send("No cards available right now. Try again later!", ephemeral=True)
            return

        daily_claims[user_id]  = today
        claims["daily"]        = daily_claims
        save_claims(claims)

        await _give_card(interaction, card, "daily", include_special=True)

    # ── /csweekly ────────────────────────────────────────────────────────────
    @app_commands.command(name="csweekly", description="Claim your weekly cricketer card!")
    @app_commands.guild_only()
    async def csweekly(self, interaction: discord.Interaction["CricStarBot"]):
        user_id = str(interaction.user.id)
        now     = datetime.now(tz=timezone.utc)

        claims        = load_claims()
        weekly_claims = claims.get("weekly", {})

        last_str = weekly_claims.get(user_id)
        if last_str:
            last_dt  = datetime.fromisoformat(last_str)
            next_dt  = last_dt + timedelta(days=WEEKLY_DAYS)
            if now < next_dt:
                remaining  = next_dt - now
                days, secs = divmod(int(remaining.total_seconds()), 86400)
                hours       = secs // 3600
                await interaction.response.send_message(
                    f"You've already claimed your weekly card, {interaction.user.mention}!\n"
                    f"Come back in **{days}d {hours}h** for your next weekly card. 🏏",
                    ephemeral=True,
                )
                return

        await interaction.response.defer()

        card = pick_weekly_card()
        if not card:
            await interaction.followup.send("No cards available right now. Try again later!", ephemeral=True)
            return

        weekly_claims[user_id] = now.isoformat()
        claims["weekly"]       = weekly_claims
        save_claims(claims)

        await _give_card(interaction, card, "weekly", include_special=False)


async def setup(bot):
    await bot.add_cog(DailyCog(bot))
