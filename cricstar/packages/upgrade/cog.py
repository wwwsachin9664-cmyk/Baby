from __future__ import annotations

import logging
import os
import random
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from cricstar.core.image_generator.image_gen import draw_premade_card
from cricstar.core.utils.checks import ADMIN_ROLE_ID
from bd_models.models import Ball
from bd_models.models import balls as balls_cache

if TYPE_CHECKING:
    from cricstar.core.bot import CricStarBot

log = logging.getLogger("cricstar.packages.upgrade")

UPGRADE_COOLDOWN_HOURS = 15

_upgrade_cooldowns: dict[int, datetime] = {}


def _is_allowed(interaction: discord.Interaction) -> bool:
    user = interaction.user
    if isinstance(user, discord.Member):
        if any(role.id == ADMIN_ROLE_ID for role in user.roles):
            return True
    return False


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

    @cricket.command(name="upgrade", description="Upgrade a cricketer's bat and ball stats (admin only)")
    @app_commands.describe(player_name="Name of the cricketer to upgrade")
    @app_commands.autocomplete(player_name=_ball_autocomplete)
    async def upgrade(
        self,
        interaction: discord.Interaction["CricStarBot"],
        player_name: str,
    ):
        is_owner = await self.bot.is_owner(interaction.user)
        if not is_owner and not _is_allowed(interaction):
            await interaction.response.send_message(
                "❌ You don't have permission to use this command.",
                ephemeral=True,
            )
            return

        user_id = interaction.user.id
        now = datetime.now(tz=timezone.utc)
        last_used = _upgrade_cooldowns.get(user_id)
        if last_used:
            cooldown_end = last_used + timedelta(hours=UPGRADE_COOLDOWN_HOURS)
            if now < cooldown_end:
                remaining = cooldown_end - now
                total_minutes = int(remaining.total_seconds() / 60)
                hours, minutes = divmod(total_minutes, 60)
                await interaction.response.send_message(
                    f"⏳ You can use `/cricket upgrade` again in **{hours}h {minutes}m**.",
                    ephemeral=True,
                )
                return

        await interaction.response.defer()

        try:
            ball = await Ball.objects.aget(country=player_name)
        except Ball.DoesNotExist:
            close = [b.country for b in balls_cache.values() if player_name.lower() in b.country.lower()][:5]
            hint = f"\nDid you mean: {', '.join(close)}?" if close else ""
            await interaction.followup.send(
                f"❌ No cricketer named **{player_name}** found.{hint}", ephemeral=True
            )
            return

        old_bat = ball.health
        old_ball = ball.attack
        bat_gain = random.randint(0, 5)
        ball_gain = random.randint(0, 5)

        ball.health = old_bat + bat_gain
        ball.attack = old_ball + ball_gain
        await ball.asave(update_fields=["health", "attack"])
        balls_cache[ball.id] = ball

        _upgrade_cooldowns[user_id] = now

        card_file: discord.File | None = None
        wild_card_name = ball.wild_card.name if ball.wild_card else ""
        is_premade = wild_card_name.startswith("premade_")

        if is_premade:
            media_dir = Path("admin_panel/media")
            backgrounds_dir = media_dir / "backgrounds"
            foregrounds_dir = media_dir / "foregrounds"
            import re as _re
            slug = _re.sub(r"[^a-z0-9]+", "_", player_name.lower().strip()).strip("_")
            fg_preset = foregrounds_dir / slug

            bg_candidates = [
                backgrounds_dir / "base_background.jpg",
                backgrounds_dir / "base_background.png",
            ]
            bg_path_actual: Path | None = next((p for p in bg_candidates if p.exists()), None)
            if not fg_preset.exists():
                bg_path_actual = None

            if bg_path_actual and fg_preset.exists():
                try:
                    def _regen():
                        return draw_premade_card(
                            player_name=ball.country,
                            rarity=round(ball.rarity * 100, 2),
                            codename=ball.capacity_name or "",
                            description=ball.capacity_description or "",
                            bat_score=ball.health,
                            ball_score=ball.attack,
                            artwork_author=ball.credits or "",
                            background_path=bg_path_actual,
                            foreground_path=fg_preset,
                        )

                    with ThreadPoolExecutor() as pool:
                        image, img_kwargs = await self.bot.loop.run_in_executor(pool, _regen)

                    card_path = media_dir / wild_card_name
                    image.save(str(card_path), **img_kwargs)
                    image.close()
                    card_file = discord.File(str(card_path), filename=wild_card_name)
                except Exception as exc:
                    log.error(f"upgrade: card regen failed for {player_name}: {exc}")

        def _stat_line(old: int, new: int, gain: int, icon: str) -> str:
            if gain > 0:
                return f"{icon} **{old}** → **{new}** (+{gain}) ✨"
            return f"{icon} **{old}** → **{new}** (no change)"

        embed = discord.Embed(
            title=f"⬆️ Upgraded: {player_name}",
            color=discord.Color.gold(),
        )
        embed.add_field(
            name="🏏 Bat Score",
            value=_stat_line(old_bat, ball.health, bat_gain, "🏏"),
            inline=False,
        )
        embed.add_field(
            name="🔴 Ball Score",
            value=_stat_line(old_ball, ball.attack, ball_gain, "🔴"),
            inline=False,
        )
        embed.set_footer(text=f"Upgraded by {interaction.user.display_name}")

        if card_file:
            embed.set_image(url=f"attachment://{wild_card_name}")
            await interaction.followup.send(embed=embed, file=card_file)
        else:
            await interaction.followup.send(embed=embed)
