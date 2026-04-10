from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import ActionRow, Button, Container, Select, Separator, TextDisplay
from django.db.models import Q
from django.utils import timezone

from cricstar.core.discord import LayoutView
from cricstar.core.utils.menus import CountryballFormatter, Menu, ModelSource, TextSource, TextFormatter
from cricstar.core.utils.sorting import FilteringChoices, SortingChoices, filter_balls, sort_balls
from cricstar.core.utils.transformers import (
    BallEnabledTransform,
    BallInstanceTransform,
    SpecialEnabledTransform,
    TradeCommandType,
)
from bd_models.models import BallInstance, Player
from settings.models import settings

from .bet import BetInstance, BettingUser  # noqa: F401
from .errors import BetError

if TYPE_CHECKING:
    from cricstar.core.bot import CricStarBot

type Interaction = discord.Interaction["CricStarBot"]

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  Bulk selector (similar to trade's BulkSelector but for bets)
# ─────────────────────────────────────────────


class BulkBetSelector(Container):
    async def configure(
        self,
        bot: CricStarBot,
        cog: Bet,
        queryset,
    ):
        assert self.view
        self.bot = bot
        self.cog = cog
        self.queryset = queryset

        self.formatter = CountryballFormatter(self.select, max_values=25)
        self.source = ModelSource(queryset)
        self.menu = Menu(bot, self.view, self.source, self.formatter)
        await self.menu.init(position=7, container=self)

        self.display_menu = None

    async def _update_display(self):
        assert self.view
        if self.display_menu and self.display_menu.source.get_max_pages() > 1:
            self.remove_item(self.display_menu.controls)
        self.balls_count.content = f"-# {len(self.formatter.defaulted)} {settings.plural_collectible_name} selected"
        if not self.formatter.defaulted:
            self.balls.content = "Nothing selected yet"
            return
        text = ""
        async for ball in (
            BallInstance.objects.filter(id__in=self.formatter.defaulted)
            .annotate(**self.queryset.query.annotations)
            .order_by(*self.queryset.query.order_by)
        ):
            text += f"- {ball.description()}\n"
        self.display_menu = Menu(self.bot, self.view, TextSource(text, page_length=3800), TextFormatter(self.balls))
        await self.display_menu.init(position=3, container=self)

    header = TextDisplay(f"## Bet bulk selection\nYour selected {settings.plural_collectible_name} are shown below.")
    sep1 = Separator()
    balls = TextDisplay("Nothing selected yet")
    balls_count = TextDisplay(f"-# 0 {settings.plural_collectible_name} selected")
    sep2 = Separator(spacing=discord.SeparatorSpacing.large)
    description = TextDisplay("-# Use the drop-down menu below to select your items.")

    selector_row = ActionRow()

    @selector_row.select(placeholder=f"Select {settings.plural_collectible_name} to stake")
    async def select(self, interaction: Interaction, select: Select):
        await interaction.response.defer()
        self.formatter.defaulted.update((int(x) for x in select.values))
        for option in select.options:
            if option.value in select.values:
                self.formatter.defaulted.add(int(option.value))
                option.default = True
            else:
                self.formatter.defaulted.discard(int(option.value))
                option.default = False
        await self._update_display()
        await interaction.edit_original_response(view=self.view)

    sep3 = Separator()
    control_row = ActionRow()

    @control_row.button(label="Select page")
    async def select_all(self, interaction: Interaction, button: Button):
        await interaction.response.defer()
        for option in self.select.options:
            self.formatter.defaulted.add(int(option.value))
            option.default = True
        await self._update_display()
        await interaction.edit_original_response(view=self.view)

    @control_row.button(label="Clear")
    async def clear(self, interaction: Interaction, button: Button):
        await interaction.response.defer()
        self.formatter.defaulted.clear()
        for option in self.select.options:
            option.default = False
        await self._update_display()
        await interaction.edit_original_response(view=self.view)

    @control_row.button(label="Add to bet", style=discord.ButtonStyle.success)
    async def validate(self, interaction: Interaction, button: Button):
        if not self.formatter.defaulted:
            await interaction.response.send_message("Nothing was selected!", ephemeral=True)
            return

        result = await self.cog.get_bet(interaction)
        if result is None:
            await interaction.response.send_message(
                "Your bet was not found — it may have ended.", ephemeral=True
            )
            return
        bet, bettor = result

        try:
            await bettor.add_to_proposal(BallInstance.objects.filter(id__in=self.formatter.defaulted))
        except BetError as e:
            await interaction.response.send_message(e.error_message, ephemeral=True)
        else:
            assert self.view
            await interaction.response.defer()
            self.view.stop()
            for child in self.view.walk_children():
                if hasattr(child, "disabled"):
                    child.disabled = True  # type: ignore
            await interaction.edit_original_response(view=self.view)
            await bet.edit_message(None)
            await interaction.followup.send(
                f"{len(self.formatter.defaulted)} {settings.plural_collectible_name} added to bet.", ephemeral=True
            )


# ─────────────────────────────────────────────
#  Bet cog
# ─────────────────────────────────────────────


@app_commands.guild_only()
class Bet(commands.GroupCog, name="bet"):
    def __init__(self, bot: CricStarBot):
        self.bot = bot
        self.bets: dict[int, dict[int, BetInstance]] = defaultdict(dict)

    async def get_bet(
        self,
        interaction: Interaction,
        user: discord.User | discord.Member | None = None,
    ) -> tuple[BetInstance, BettingUser] | None:
        assert interaction.channel
        user = user or interaction.user
        bet = self.bets.get(interaction.channel.id, {}).get(user.id)
        if not bet:
            return None
        if not bet.active:
            self.bets[interaction.channel.id].pop(bet.bettor1.user.id, None)
            self.bets[interaction.channel.id].pop(bet.bettor2.user.id, None)
            await bet.cleanup()
            return None
        bettor = bet.bettor1 if bet.bettor1.user == user else bet.bettor2
        return bet, bettor

    # ── Commands ───────────────────────────────────────────────────────────────

    @app_commands.command()
    @app_commands.checks.bot_has_permissions(send_messages=True)
    async def begin(self, interaction: Interaction, user: discord.User):
        """
        Start a bet with another player — whoever wins gets all staked cricketers!

        Parameters
        ----------
        user: discord.User
            The user you want to bet with.
        """
        assert interaction.channel

        if user.bot:
            await interaction.response.send_message("You cannot bet with bots.", ephemeral=True)
            return
        if user.id == interaction.user.id:
            await interaction.response.send_message("You cannot bet with yourself.", ephemeral=True)
            return

        player1, _ = await Player.objects.aget_or_create(discord_id=interaction.user.id)
        player2, _ = await Player.objects.aget_or_create(discord_id=user.id)

        if await self.get_bet(interaction) is not None:
            await interaction.response.send_message("You already have an active bet.", ephemeral=True)
            return
        if await self.get_bet(interaction, user) is not None:
            await interaction.response.send_message(f"{user.mention} already has an active bet.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        bet = BetInstance.configure(self, (player1, interaction.user), (player2, user))
        self.bets[interaction.channel.id][interaction.user.id] = bet
        self.bets[interaction.channel.id][user.id] = bet

        try:
            await bet.start(interaction.channel)  # type: ignore
        except Exception:
            del self.bets[interaction.channel.id][interaction.user.id]
            del self.bets[interaction.channel.id][user.id]
            await bet.cleanup()
            raise
        else:
            await interaction.followup.send("The bet has started!", ephemeral=True)

    @app_commands.command(extras={"trade": TradeCommandType.PICK})
    async def add(self, interaction: Interaction, cricketer: BallInstanceTransform):
        """
        Add a cricketer to your bet proposal as a stake.

        Parameters
        ----------
        cricketer: BallInstance
            The cricketer you are staking in the bet.
        """
        result = await self.get_bet(interaction)
        if result is None:
            await interaction.response.send_message("You do not have any active bet.", ephemeral=True)
            return
        bet, bettor = result
        try:
            await bettor.add_to_proposal(BallInstance.objects.filter(id=cricketer.pk))
        except BetError as e:
            await interaction.response.send_message(e.error_message, ephemeral=True)
        else:
            await bet.edit_message(None)
            await interaction.response.send_message(
                f"{cricketer.description(is_trade=True)} added to bet.",
                ephemeral=True,
            )

    @app_commands.command()
    async def remove(self, interaction: Interaction, cricketer: BallInstanceTransform):
        """
        Remove a cricketer from your bet proposal.

        Parameters
        ----------
        cricketer: BallInstance
            The cricketer you are removing from your bet.
        """
        result = await self.get_bet(interaction)
        if result is None:
            await interaction.response.send_message("You do not have any active bet.", ephemeral=True)
            return
        bet, bettor = result
        try:
            await bettor.remove_from_proposal(BallInstance.objects.filter(id=cricketer.pk))
        except BetError as e:
            await interaction.response.send_message(e.error_message, ephemeral=True)
        else:
            await bet.edit_message(None)
            await interaction.response.send_message(
                f"{cricketer.description(is_trade=True)} removed from bet.",
                ephemeral=True,
            )

    @app_commands.command()
    async def bulk_add(
        self,
        interaction: Interaction,
        cricketer: BallEnabledTransform | None = None,
        sort: SortingChoices | None = None,
        special: SpecialEnabledTransform | None = None,
        filter: FilteringChoices | None = None,
    ):
        """
        Bulk add cricketers to the ongoing bet, with parameters to aid with searching.

        Parameters
        ----------
        cricketer: Ball
            Filter results to a specific cricketer.
        sort: SortingChoices
            Choose how cricketers are sorted (e.g. by rarity).
        special: Special
            Filter results to a special event.
        filter: FilteringChoices
            Filter results to a specific filter.
        """
        await interaction.response.defer(thinking=True, ephemeral=True)
        result = await self.get_bet(interaction)
        if result is None:
            await interaction.followup.send("You do not have any active bet.", ephemeral=True)
            return
        _, bettor = result
        if bettor.locked:
            await interaction.followup.send(
                "You have locked your proposal — it cannot be edited! "
                "Click Cancel bet to stop the bet instead.",
                ephemeral=True,
            )
            return

        query = (
            BallInstance.objects.filter(
                Q(locked=None) | Q(locked__lt=timezone.now() - timedelta(seconds=60)),
                player__discord_id=interaction.user.id,
            )
            .exclude(tradeable=False)
            .exclude(ball__tradeable=False)
            .exclude(special__tradeable=False)
        )
        if cricketer:
            query = query.filter(ball=cricketer)
        if special:
            query = query.filter(special=special)
        if sort:
            query = sort_balls(sort, query)
        if filter:
            query = filter_balls(filter, query, interaction.guild_id)
        query.query.add_ordering("-id")

        if not await query.aexists():
            await interaction.followup.send(f"No {settings.plural_collectible_name} found.", ephemeral=True)
            return

        view = LayoutView()
        selector = BulkBetSelector()
        view.add_item(selector)
        await selector.configure(self.bot, self, query)
        await interaction.followup.send(view=view, ephemeral=True)
