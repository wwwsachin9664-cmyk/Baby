from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bd_models.models import Ball, BallInstance, Player, Special
from bd_models.models import balls as balls_cache

if TYPE_CHECKING:
    from cricstar.core.bot import CricStarBot

type Interaction = discord.Interaction["CricStarBot"]

log = logging.getLogger(__name__)

SHINY_BG_PATH = Path("admin_panel/media/shiny_bg.png")
SHINY_SPECIAL_NAME = "Shiny"

RANKUP_TABLE: list[tuple[float, float, int]] = [
    (0.01,  0.03,  3),
    (0.05,  0.08,  5),
    (0.10,  0.40, 10),
    (0.50,  0.90, 15),
    (1.00,  5.00, 20),
    (5.10, 10.00, 25),
    (10.10, 15.00, 30),
    (15.10, 20.00, 35),
]


def badge_rarity(ball: Ball) -> float:
    """Return the display rarity shown on the card face (badge_rarity in capacity_logic).

    This is different from ball.rarity which is the spawn weight.
    The rankup table uses badge_rarity (e.g. 0.01, 0.5, 1.0) not spawn weight.
    """
    logic = ball.capacity_logic or {}
    br = logic.get("badge_rarity")
    if br is not None:
        return float(br)
    return float(ball.rarity)


def get_required_copies(ball: Ball) -> int | None:
    """Look up required copies using the card's display rarity."""
    br = badge_rarity(ball)
    for low, high, cost in RANKUP_TABLE:
        if low - 0.001 <= br <= high + 0.001:
            return cost
    return None


class ConfirmView(discord.ui.View):
    def __init__(self, timeout: float = 30):
        super().__init__(timeout=timeout)
        self.confirmed: bool = False

    @discord.ui.button(label="✅ Yes, rank up!", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        self.stop()
        await interaction.response.defer()


class RankUp(commands.GroupCog, name="rankup"):
    def __init__(self, bot: "CricStarBot"):
        self.bot = bot

    async def _owned_eligible_autocomplete(
        self, interaction: Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Return player names the user actually owns and that are eligible for rankup."""
        try:
            player, _ = await Player.objects.aget_or_create(discord_id=interaction.user.id)
        except Exception:
            return []

        current_lower = current.lower()
        seen: set[str] = set()
        choices: list[app_commands.Choice[str]] = []

        async for inst in (
            BallInstance.objects
            .filter(player=player, deleted=False)
            .select_related("ball")
            .order_by("ball__country")
        ):
            name = inst.ball.country
            if name in seen:
                continue
            if current_lower and current_lower not in name.lower():
                continue
            if get_required_copies(inst.ball) is None:
                continue
            seen.add(name)
            choices.append(app_commands.Choice(name=name, value=name))
            if len(choices) >= 25:
                break
        return choices

    # ── /rankup info ────────────────────────────────────────────────────────

    @app_commands.command(name="info")
    @app_commands.describe(player="Select a cricketer you own")
    @app_commands.autocomplete(player=_owned_eligible_autocomplete)
    async def rankup_info(self, interaction: Interaction, player: str):
        """Check how many copies you need to rank up a cricketer you own."""
        await interaction.response.defer(ephemeral=True)

        target_ball: Ball | None = None
        for b in balls_cache.values():
            if b.country.lower() == player.lower():
                target_ball = b
                break

        if target_ball is None:
            await interaction.followup.send(
                f"❌ No cricketer named **{player}** found.", ephemeral=True
            )
            return

        required = get_required_copies(target_ball)
        if required is None:
            await interaction.followup.send(
                f"❌ **{player}** (rarity `{badge_rarity(target_ball)}`) is not eligible for rankup.",
                ephemeral=True,
            )
            return

        player_obj, _ = await Player.objects.aget_or_create(discord_id=interaction.user.id)
        count = await (
            BallInstance.objects
            .filter(player=player_obj, ball=target_ball, deleted=False)
            .exclude(special__name=SHINY_SPECIAL_NAME)
            .acount()
        )

        needed = max(0, required - count)
        if needed == 0:
            msg = (
                f"**{player}** – {required} copies required\n"
                f"You have **{count}** copies. You can rank up! ✅"
            )
        else:
            msg = (
                f"**{player}** – {required} copies required\n"
                f"You need **{needed} more** copies to rank up"
            )

        await interaction.followup.send(msg, ephemeral=True)

    # ── /rankup upgrade ─────────────────────────────────────────────────────

    @app_commands.command(name="upgrade")
    @app_commands.describe(player="Select a cricketer you own to rank up")
    @app_commands.autocomplete(player=_owned_eligible_autocomplete)
    async def rankup_upgrade(self, interaction: Interaction, player: str):
        """Sacrifice all copies of a cricketer to receive its ✨ shiny version."""
        await interaction.response.defer(ephemeral=True)

        # Resolve the ball
        target_ball: Ball | None = None
        for b in balls_cache.values():
            if b.country.lower() == player.lower():
                target_ball = b
                break

        if target_ball is None:
            await interaction.followup.send(
                f"❌ No cricketer named **{player}** found.", ephemeral=True
            )
            return

        required = get_required_copies(target_ball)
        if required is None:
            await interaction.followup.send(
                f"❌ **{player}** (rarity `{badge_rarity(target_ball)}`) is not eligible for rankup.",
                ephemeral=True,
            )
            return

        try:
            shiny_special = await Special.objects.aget(name=SHINY_SPECIAL_NAME)
        except Special.DoesNotExist:
            await interaction.followup.send(
                f"❌ Could not find a Special named **{SHINY_SPECIAL_NAME}** in the database.\n"
                "Please create it in the admin panel first.",
                ephemeral=True,
            )
            return

        player_obj, _ = await Player.objects.aget_or_create(discord_id=interaction.user.id)

        all_copies: list[BallInstance] = []
        async for copy in (
            BallInstance.objects
            .filter(player=player_obj, ball=target_ball, deleted=False)
            .select_related("ball", "special")
            .order_by("id")
        ):
            if not (copy.special and copy.special.name == SHINY_SPECIAL_NAME):
                all_copies.append(copy)

        if not all_copies:
            await interaction.followup.send(
                f"{interaction.user.mention}, you do not have enough copies to rank *{player}* up\n"
                f"Check the requirements for ranking up with `/rankup info`",
                ephemeral=True,
            )
            return

        if len(all_copies) < required:
            await interaction.followup.send(
                f"{interaction.user.mention}, you do not have enough copies to rank *{player}* up\n"
                f"Check the requirements for ranking up with `/rankup info`",
                ephemeral=True,
            )
            return

        target_copy = all_copies[0]

        confirm_view = ConfirmView(timeout=30)
        await interaction.followup.send(
            f"⚠️ **Rankup Confirmation**\n\n"
            f"You are about to sacrifice **ALL {len(all_copies)} copies** of "
            f"**{player}** to create a ✨ **shiny** version.\n\n"
            f"This **cannot be undone**. Do you want to continue?",
            view=confirm_view,
            ephemeral=True,
        )
        await confirm_view.wait()

        if not confirm_view.confirmed:
            await interaction.followup.send("❌ Rankup cancelled.", ephemeral=True)
            return

        # Delete ALL non-shiny copies
        all_ids = [c.id for c in all_copies]
        await BallInstance.objects.filter(id__in=all_ids).aupdate(deleted=True)

        # Try to generate shiny card image if shiny bg is configured
        shiny_extra: dict = {}
        shiny_image_note = ""
        slug = re.sub(r"[^a-z0-9]+", "_", target_ball.country.lower().strip()).strip("_")
        fg_src: Path | None = None
        foregrounds_dir = Path("admin_panel/media/foregrounds")
        for ext in ("", ".png", ".jpg", ".jpeg", ".webp"):
            candidate = foregrounds_dir / f"{slug}{ext}"
            if candidate.exists():
                fg_src = candidate
                break

        if not SHINY_BG_PATH.exists():
            shiny_image_note = "\n-# *(No shiny background set — run `/makeshinybackground` to enable custom shiny card images.)*"
        elif fg_src is None:
            shiny_image_note = "\n-# *(Foreground preset not found — shiny card image could not be generated.)*"

        if SHINY_BG_PATH.exists() and fg_src is not None:
            logic = dict(target_ball.capacity_logic or {})
            _card_name = logic.get("display_name") or target_ball.country
            _codename = target_ball.capacity_name or ""
            _description = target_ball.capacity_description or ""
            _rarity = logic.get("badge_rarity", target_ball.rarity)
            _fg_border = logic.get("foreground_border", True)
            _credit_stroke = logic.get("credit_stroke", True)
            _credit_font = logic.get("credit_font", "default")

            from cricstar.core.image_generator.image_gen import draw_premade_card, get_neon_color
            from cricstar.core.foreground_border_overrides import resolve_border as _resolve_border

            _border_size = _resolve_border(target_ball.pk, str(fg_src))
            _fg_src_str = str(fg_src)
            _bg_str = str(SHINY_BG_PATH)

            def _gen_shiny() -> tuple:
                return draw_premade_card(
                    _bg_str, _fg_src_str,
                    _card_name, _codename, _description,
                    _rarity, target_ball.health, target_ball.attack,
                    target_ball.credits or "", None,
                    neon_color=get_neon_color(target_ball.country),
                    foreground_border=_fg_border,
                    credit_stroke=_credit_stroke,
                    credit_font=_credit_font,
                    border_size=_border_size,
                )

            try:
                with ThreadPoolExecutor() as pool:
                    shiny_img, shiny_kwargs = await self.bot.loop.run_in_executor(pool, _gen_shiny)
                shiny_filename = f"premade_{slug}_shiny.png"
                shiny_card_path = Path("admin_panel/media") / shiny_filename
                shiny_img.save(str(shiny_card_path), **shiny_kwargs)
                shiny_img.close()
                shiny_extra["shiny_card"] = shiny_filename
                log.info("rankup: generated shiny card %s", shiny_filename)
            except Exception as e:
                log.warning("rankup: could not generate shiny card image: %s", e)

        # Create the shiny BallInstance
        shiny_card = await BallInstance.objects.acreate(
            ball=target_copy.ball,
            player=player_obj,
            attack_bonus=target_copy.attack_bonus,
            health_bonus=target_copy.health_bonus,
            tradeable=target_copy.tradeable,
            special=shiny_special,
            extra_data=shiny_extra,
        )

        atk_pct = (
            f"+{target_copy.attack_bonus}%"
            if target_copy.attack_bonus >= 0
            else f"{target_copy.attack_bonus}%"
        )
        hp_pct = (
            f"+{target_copy.health_bonus}%"
            if target_copy.health_bonus >= 0
            else f"{target_copy.health_bonus}%"
        )

        success_msg = (
            f"Congratulations {interaction.user.mention}, you have successfully ranked up "
            f"*{player}* to its ✨ **shiny** ✨ version\n\n"
            f"✨``{player}``✨\n"
            f"**ATK:** ``{atk_pct}`` • **HP:** ``{hp_pct}``"
            f"{shiny_image_note}"
        )

        await interaction.channel.send(
            content=f"{interaction.user.mention} used 🌟 **rankup upgrade**",
        )
        await interaction.channel.send(success_msg)
        await interaction.followup.send("✅ Done! Check the channel.", ephemeral=True)

        log.info(
            "User %s (%d) ranked up %s to shiny. New card ID: %d. %d copies consumed.",
            interaction.user, interaction.user.id, player, shiny_card.id, len(all_copies),
        )


async def setup(bot: "CricStarBot"):
    await bot.add_cog(RankUp(bot))
