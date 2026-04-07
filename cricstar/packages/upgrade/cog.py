from __future__ import annotations

import logging
import random
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bd_models.models import Ball
from bd_models.models import balls as balls_cache
from cricstar.core.image_generator.image_gen import patch_card_stats

if TYPE_CHECKING:
    from cricstar.core.bot import CricStarBot

log = logging.getLogger("cricstar.packages.upgrade")

UPGRADE_COOLDOWN_HOURS = 15

# Global cooldown — only ONE upgrade allowed every 15 hours across ALL users
_last_upgrade_time: datetime | None = None


def reset_upgrade_cooldown():
    """Reset the global upgrade cooldown (called by /csresetcooldown)."""
    global _last_upgrade_time
    _last_upgrade_time = None


MEDIA_DIR       = Path("admin_panel/media")
BACKGROUNDS_DIR = Path("admin_panel/media/backgrounds")


class Upgrade(commands.Cog):
    def __init__(self, bot: "CricStarBot"):
        self.bot = bot

    cricket = app_commands.Group(
        name="cricket",
        description="Cricket card management commands",
    )

    async def _ball_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        current_lower = current.lower()
        matches = [
            app_commands.Choice(name=ball.country, value=ball.country)
            for ball in balls_cache.values()
            if current_lower in ball.country.lower()
        ]
        return matches[:25]

    @cricket.command(name="upgrade", description="Upgrade a cricketer's bat and bowl stats")
    @app_commands.describe(player_name="Name of the cricketer to upgrade")
    @app_commands.autocomplete(player_name=_ball_autocomplete)
    async def upgrade(
        self,
        interaction: discord.Interaction["CricStarBot"],
        player_name: str,
    ):
        global _last_upgrade_time

        now = datetime.now(tz=timezone.utc)

        # Global 15-hour cooldown check
        if _last_upgrade_time is not None:
            cooldown_end = _last_upgrade_time + timedelta(hours=UPGRADE_COOLDOWN_HOURS)
            if now < cooldown_end:
                remaining = cooldown_end - now
                total_minutes = int(remaining.total_seconds() / 60)
                hours, minutes = divmod(total_minutes, 60)
                await interaction.response.send_message(
                    f"⏳ An upgrade was already done recently! Try again in **{hours}h {minutes}m**.",
                    ephemeral=True,
                )
                return

        await interaction.response.defer()

        try:
            ball = await Ball.objects.select_related("regime").aget(country=player_name)
        except Ball.DoesNotExist:
            close = [b.country for b in balls_cache.values() if player_name.lower() in b.country.lower()][:5]
            hint = f"\nDid you mean: {', '.join(close)}?" if close else ""
            await interaction.followup.send(
                f"❌ No cricketer named **{player_name}** found.{hint}", ephemeral=True
            )
            return

        old_bat  = ball.health
        old_bowl = ball.attack
        bat_gain  = random.randint(0, 5)
        bowl_gain = random.randint(0, 5)

        ball.health = old_bat  + bat_gain
        ball.attack = old_bowl + bowl_gain
        await ball.asave(update_fields=["health", "attack"])
        balls_cache[ball.id] = ball

        # Mark global cooldown
        _last_upgrade_time = now

        # ── Patch ONLY the stats bar on the existing card image ────────────────
        wild_card_name = str(ball.wild_card)
        card_path      = MEDIA_DIR / wild_card_name

        if card_path.exists():
            try:
                # Find the background file for this card's regime
                regime     = ball.regime
                bg_file    = str(regime.background) if regime and regime.background else ""
                bg_path    = str(MEDIA_DIR / bg_file) if bg_file else ""

                # Fall back to custom_bg if regime bg not found
                if not bg_path or not Path(bg_path).exists():
                    candidate = BACKGROUNDS_DIR / "custom_bg.png"
                    if candidate.exists():
                        bg_path = str(candidate)

                if bg_path and Path(bg_path).exists():
                    credits_str = ball.credits or ""

                    def _patch():
                        patch_card_stats(
                            str(card_path), bg_path,
                            ball.health, ball.attack, credits_str,
                        )

                    with ThreadPoolExecutor() as pool:
                        await self.bot.loop.run_in_executor(pool, _patch)

                    log.info(
                        "upgrade: patched stats for %s — bat %d→%d, bowl %d→%d",
                        ball.country, old_bat, ball.health, old_bowl, ball.attack,
                    )
                else:
                    log.warning("upgrade: no background found for %s, skipping patch", ball.country)
            except Exception as e:
                log.warning("upgrade: stat patch failed for %s: %s", ball.country, e)

        # Build result message
        ball_id_str = f"#{ball.id}"
        bat_part    = f"**Bat** by **+{bat_gain}**"   if bat_gain  > 0 else "**Bat** unchanged"
        bowl_part   = f"**Bowl** by **+{bowl_gain}**"  if bowl_gain > 0 else "**Bowl** unchanged"
        msg = f"{ball_id_str} **{ball.country}** increased its {bat_part}! {bowl_part}!"

        await interaction.followup.send(content=msg)
