"""
Trade logic for CricStar. Uses a single-container layout matching the CricDex reference style.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast

import discord
from asgiref.sync import sync_to_async
from discord.ui import ActionRow, Button, Item, Section, Separator, TextDisplay, TextInput
from discord.utils import format_dt
from django.db import transaction
from django.utils import timezone

from cricstar.core.discord import UNKNOWN_INTERACTION, Container, LayoutView, Modal
from cricstar.core.utils.buttons import ConfirmChoiceView
from cricstar.core.utils.emojis import get_player_emoji
from cricstar.core.utils.menus import CountryballFormatter, Menu, ModelSource, TextFormatter, TextSource
from bd_models.enums import TradeCooldownPolicy
from bd_models.models import BallInstance, Player, Trade, TradeObject
from settings.models import settings
from settings.utils import format_currency

from .errors import (
    AlreadyLockedError,
    CancelledError,
    IntegrityError,
    LockedError,
    NotProposedError,
    NotTradeableError,
    OwnershipError,
    SynchronizationError,
    TradeError,
)

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from cricstar.core.bot import CricStarBot

    from .cog import Trade as TradeCog

type Interaction = discord.Interaction[CricStarBot]

log = logging.getLogger(__name__)

TRADE_TIMEOUT = 60 * 30
COOLDOWN_BYPASS_TIMEOUT = 10


class SetMoneyModal(Modal, title="Set money offering"):
    proposal = TextInput(label=f"How much {settings.currency_name} to propose?", style=discord.TextStyle.short)

    def __init__(self, trading_user: TradingUser):
        super().__init__()
        self.trading_user = trading_user

    async def interaction_check(self, interaction: Interaction) -> bool:
        if not interaction.user.id == self.trading_user.user.id:
            await interaction.response.send_message(
                "You are not allowed to do this, edit your own trade.", ephemeral=True
            )
            return False
        return await super().interaction_check(interaction)

    async def on_submit(self, interaction: Interaction):
        if self.trading_user.locked:
            await interaction.response.send_message("You have already locked your proposal!", ephemeral=True)
            return
        try:
            proposal_amount = int(self.proposal.value.strip())
        except ValueError:
            await interaction.response.send_message("This number could not be parsed.", ephemeral=True)
            return
        await self.trading_user.player.arefresh_from_db(fields=["money"])
        if not self.trading_user.player.can_afford(proposal_amount):
            await interaction.response.send_message("You cannot afford that amount.", ephemeral=True)
            return
        await interaction.response.defer()
        self.trading_user.money = proposal_amount
        await self.trading_user.view.edit_message(interaction)


class TradingUser:
    """
    Represent one user part of a trade. Plain data/logic class (not a UI element).

    Parameters
    ----------
    trade: TradeInstance
        The trade instance attached to this user.
    player: Player
        The fetched player model of the user.
    user: discord.abc.User
        The Discord user model.
    """

    def __init__(self, trade: TradeInstance, player: Player, user: discord.abc.User):
        self.trade = trade
        self.cog = trade.cog
        self.player = player
        self.user = user
        self.proposal: set[int] = set()
        self.money = 0
        self.locked: bool = False
        self.cancelled: bool = False
        self.confirmed: bool = False

        self.proposal_list = TextDisplay("")
        self.select_row: ActionRow | None = None
        self.menu: Menu | None = None

        self.view: TradeInstance = trade

    def __repr__(self) -> str:
        return f"<TradingUser player_id={self.player.pk} discord_id={self.user.id}>"

    def get_queryset(self) -> "QuerySet[BallInstance]":
        if not self.proposal:
            return BallInstance.objects.none()
        return BallInstance.objects.filter(id__in=self.proposal)

    async def render_proposal(self, container: Container):
        """Add this trader's proposal section into the given container."""
        if not self.locked:
            assert self.select_row is not None
            select = cast(discord.ui.Select, self.select_row.children[0])
            select.options.clear()
            container.add_item(self.select_row)

            if self.proposal:
                if self.menu is None or not isinstance(self.menu.source, ModelSource):
                    self.menu = Menu(
                        self.cog.bot, self.view,
                        ModelSource(self.get_queryset().order_by("locked")),
                        CountryballFormatter(select, max_values=25),
                    )
                else:
                    cast(ModelSource, self.menu.source).queryset = self.get_queryset().order_by("locked")
                    cast(CountryballFormatter, self.menu.formatters[0]).item = select
                await self.menu.init(container=container)

                # Show card names as text immediately (same as bet/lock view)
                text = ""
                async for ball in self.get_queryset().select_related("ball").prefetch_related("special"):
                    desc = ball.description(include_emoji=True, bot=self.cog.bot, is_trade=True)
                    emoji_str = get_player_emoji(ball.ball.country)
                    if emoji_str:
                        desc = f"{emoji_str}{desc}"
                    text += f"- {desc}\n"
                container.add_item(TextDisplay(text.strip()))
            else:
                select.add_option(label="Nothing yet")
                select.disabled = True
                select.max_values = 1

            if settings.currency_enabled:
                button = Button(label="Change", style=discord.ButtonStyle.primary)
                button.callback = self._set_currency_callback()
                container.add_item(
                    Section(
                        TextDisplay(f"{settings.currency_name} proposed: {format_currency(self.money)}"),
                        accessory=button,
                    )
                )
        else:
            container.add_item(self.proposal_list)
            if self.proposal and self.menu is not None:
                await self.menu.init(container=container)

        if not self.view.active:
            if self.select_row:
                for child in self.select_row.children:
                    if hasattr(child, "disabled"):
                        child.disabled = True  # type: ignore

    def _set_currency_callback(self):
        trading_user = self

        async def callback(interaction: Interaction):
            modal = SetMoneyModal(trading_user)
            await interaction.response.send_modal(modal)
            await modal.wait()

        return callback

    async def add_to_proposal(self, queryset: "QuerySet[BallInstance]"):
        """
        Add cricketers to a trader's proposal.

        Raises
        ------
        LockedError, OwnershipError, AlreadyLockedError, NotTradeableError
        """
        if self.locked:
            raise LockedError()
        if self.view.cancelled:
            raise CancelledError()
        proposal: set[int] = set()
        async for ball in queryset.only(
            "id", "locked", "player_id", "tradeable", "ball__tradeable", "special__tradeable"
        ):
            if ball.player_id != self.player.pk:
                raise OwnershipError()
            if await ball.is_locked(refresh=False):
                raise AlreadyLockedError()
            if not ball.is_tradeable:
                raise NotTradeableError()
            proposal.add(ball.pk)
        await queryset.aupdate(locked=timezone.now())
        self.proposal.update(proposal)

    async def remove_from_proposal(self, queryset: "QuerySet[BallInstance]"):
        """
        Remove the given cricketer from the trader's proposal.

        Raises
        ------
        LockedError, NotProposedError
        """
        if self.locked:
            raise LockedError()
        if self.view.cancelled:
            raise CancelledError()
        ids: set[int] = {x.pk async for x in queryset.only("pk")}
        if not ids.issubset(self.proposal):
            raise NotProposedError()
        self.proposal.difference_update(ids)
        await queryset.aupdate(locked=None)

    async def lock(self):
        """
        Lock the proposal.

        Raises
        ------
        LockedError, CancelledError
        """
        if self.locked:
            raise LockedError()
        if self.view.cancelled:
            raise CancelledError()
        self.locked = True

        if not self.proposal:
            self.proposal_list.content = "*Empty*"
            return

        if (
            (not self.view.trader1.proposal and not self.view.trader1.money)
            and (not self.view.trader2.proposal and not self.view.trader2.money)
            and self.view.confirmation_phase
        ):
            await self.view.cleanup()
            return

        text = ""
        async for ball in self.get_queryset().select_related("ball").prefetch_related("special"):
            desc = ball.description(include_emoji=True, bot=self.cog.bot, is_trade=True)
            emoji_str = get_player_emoji(ball.ball.country)
            if emoji_str:
                desc = f"{emoji_str}{desc}"
            text += f"- {desc}\n"
        self.menu = Menu(
            self.cog.bot, self.view,
            TextSource(text, page_length=1800),
            TextFormatter(self.proposal_list),
        )

    async def clear(self):
        """
        Remove all items from the proposal.

        Raises
        ------
        AlreadyLockedError, CancelledError
        """
        if self.locked:
            raise AlreadyLockedError()
        if self.view.cancelled:
            raise CancelledError()
        await self.get_queryset().aupdate(locked=None)
        self.proposal.clear()

    async def cancel(self):
        self.cancelled = True
        self.view.stop()
        await self.view.cleanup()

    async def confirm(self):
        """
        Confirm the trade for this user.

        Raises
        ------
        SynchronizationError, AssertionError, IntegrityError
        """
        assert self.view.confirmation_phase is True
        self.confirmed = True
        if self.view.trader1.confirmed and self.view.trader2.confirmed:
            await self.view.finish_trade()


class TradeInstance(LayoutView):
    """
    A trade instance. Displays as a single container matching CricDex style.
    """

    def __init__(self, cog: "TradeCog"):
        super().__init__(timeout=TRADE_TIMEOUT)
        self.cog = cog
        self.trader1: TradingUser
        self.trader2: TradingUser
        self.message: discord.Message
        self.invite_mention: str = ""

        self.confirmation_lock = asyncio.Lock()
        self.edit_lock = asyncio.Lock()
        self.next_edit_interaction: Interaction | None = None
        self.confirmation_phase_start: datetime | None = None

        self.timeout_task = asyncio.create_task(self._timeout(), name=f"trade-timeout-{id(self)}")

    async def on_error(self, interaction: Interaction, error: Exception, item: Item) -> None:
        if isinstance(error, discord.NotFound) and error.code in UNKNOWN_INTERACTION:
            log.warning("Expired interaction", exc_info=error)
            return
        log.exception(f"Error in trade between {self.trader1} and {self.trader2}", exc_info=error)
        await self.cleanup()
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send("An error occured, the trade will be cancelled.", ephemeral=True)
        self.clear_items()
        self.add_item(
            TextDisplay("An error occured and the trade has been cancelled! Contact support if this persists.")
        )
        await self.message.edit(view=self)

    async def _timeout(self):
        await asyncio.sleep(TRADE_TIMEOUT)
        if self.active:
            await self._cleanup()

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id not in (self.trader1.user.id, self.trader2.user.id):
            await interaction.response.send_message("You are not part of this trade!", ephemeral=True)
            return False
        return True

    buttons = ActionRow()

    @buttons.button(label="Lock proposal", emoji="\N{LOCK}", style=discord.ButtonStyle.primary)
    async def lock_button(self, interaction: Interaction, button: Button):
        trader = {self.trader1.user.id: self.trader1, self.trader2.user.id: self.trader2}[interaction.user.id]
        if trader.locked:
            await interaction.response.send_message("You have already locked your proposal!", ephemeral=True)
            return

        await interaction.response.defer()
        try:
            await trader.lock()
        except TradeError as e:
            await interaction.followup.send(e.error_message, ephemeral=True)
        else:
            if self.confirmation_phase and self.confirmation_phase_start is None:
                self.confirmation_phase_start = datetime.now()
            await self.edit_message(interaction)

    @buttons.button(label="Reset", emoji="\N{DASH SYMBOL}", style=discord.ButtonStyle.secondary)
    async def clear_button(self, interaction: Interaction, button: Button):
        trader = {self.trader1.user.id: self.trader1, self.trader2.user.id: self.trader2}[interaction.user.id]
        if trader.locked:
            await interaction.response.send_message("You have already locked your proposal!", ephemeral=True)
            return
        view = ConfirmChoiceView(interaction, accept_message="Clearing your proposal...")
        await interaction.response.send_message(
            "Are you sure you want to clear your proposal?", view=view, ephemeral=True
        )
        await view.wait()
        if not view.value:
            return

        try:
            await trader.clear()
        except TradeError as e:
            await interaction.followup.send(e.error_message, ephemeral=True)
        else:
            await self.edit_message(None)

    @buttons.button(
        label="Cancel trade",
        emoji="\N{HEAVY MULTIPLICATION X}\N{VARIATION SELECTOR-16}",
        style=discord.ButtonStyle.danger,
    )
    async def cancel_button(self, interaction: Interaction, button: Button):
        trader = {self.trader1.user.id: self.trader1, self.trader2.user.id: self.trader2}[interaction.user.id]
        view = ConfirmChoiceView(
            interaction, accept_message="Cancelling the trade...", cancel_message="This request has been cancelled."
        )
        await interaction.response.send_message(
            "Are you sure you want to cancel this trade?", view=view, ephemeral=True
        )
        await view.wait()
        if not view.value:
            return

        try:
            await trader.cancel()
        except TradeError as e:
            await interaction.followup.send(e.error_message, ephemeral=True)
        else:
            await self.edit_message(None)

    @buttons.button(
        label="Confirm", emoji="\N{HEAVY CHECK MARK}\N{VARIATION SELECTOR-16}", style=discord.ButtonStyle.success
    )
    async def confirm_button(self, interaction: Interaction, button: Button):
        trader = {self.trader1.user.id: self.trader1, self.trader2.user.id: self.trader2}[interaction.user.id]
        both_bypass = (
            self.trader1.player.trade_cooldown_policy == TradeCooldownPolicy.BYPASS
            and self.trader2.player.trade_cooldown_policy == TradeCooldownPolicy.BYPASS
        )
        if not both_bypass and self.confirmation_phase_start is not None:
            elapsed = (datetime.now() - self.confirmation_phase_start).total_seconds()
            remaining = COOLDOWN_BYPASS_TIMEOUT - elapsed
            if remaining > 0:
                await interaction.response.send_message(
                    f"Please wait {remaining:.0f} more second(s) before confirming.", ephemeral=True
                )
                return
        await interaction.response.defer()
        try:
            await trader.confirm()
        except TradeError as e:
            await interaction.followup.send(e.error_message, ephemeral=True)
        else:
            await self.edit_message(interaction)

    @classmethod
    def configure(
        cls, cog: "TradeCog", trader1: tuple[Player, discord.abc.User], trader2: tuple[Player, discord.abc.User]
    ):
        trade = cls(cog)

        select1 = discord.ui.Select(
            placeholder="Click to remove an item",
            min_values=1,
            max_values=1,
            custom_id=f"ts1_{id(trade)}",
        )
        select2 = discord.ui.Select(
            placeholder="Click to remove an item",
            min_values=1,
            max_values=1,
            custom_id=f"ts2_{id(trade)}",
        )

        async def remove1(interaction: Interaction):
            t = trade.trader1
            if interaction.user.id != t.user.id:
                await interaction.response.send_message(
                    "You are not allowed to do this, edit your own trade.", ephemeral=True
                )
                return
            await interaction.response.defer()
            try:
                await t.remove_from_proposal(
                    BallInstance.objects.filter(id__in=(int(x) for x in interaction.data.get("values", [])))  # type: ignore
                )
            except TradeError as e:
                await interaction.followup.send(e.error_message, ephemeral=True)
            else:
                await trade.edit_message(interaction)

        async def remove2(interaction: Interaction):
            t = trade.trader2
            if interaction.user.id != t.user.id:
                await interaction.response.send_message(
                    "You are not allowed to do this, edit your own trade.", ephemeral=True
                )
                return
            await interaction.response.defer()
            try:
                await t.remove_from_proposal(
                    BallInstance.objects.filter(id__in=(int(x) for x in interaction.data.get("values", [])))  # type: ignore
                )
            except TradeError as e:
                await interaction.followup.send(e.error_message, ephemeral=True)
            else:
                await trade.edit_message(interaction)

        select1.callback = remove1
        select2.callback = remove2

        select_row1 = ActionRow()
        select_row1.add_item(select1)
        select_row2 = ActionRow()
        select_row2.add_item(select2)

        trade.trader1 = TradingUser(trade, *trader1)
        trade.trader1.select_row = select_row1
        trade.trader2 = TradingUser(trade, *trader2)
        trade.trader2.select_row = select_row2

        trade.buttons.remove_item(trade.confirm_button)
        return trade

    async def _rebuild_view(self):
        """Rebuild the entire view as a single container, matching CricDex style."""
        self.clear_items()
        container = Container()

        t1, t2 = self.trader1, self.trader2
        finished = self.is_finished() and not self.cancelled

        if finished:
            container.accent_colour = discord.Colour.green()
            header = "**Cricketers trading**\nTrade concluded!"
            t1_prefix = "✅ "
            t2_prefix = "✅ "
            show_buttons = False
        elif self.cancelled:
            container.accent_colour = discord.Colour.red()
            header = "**Cricketers trading**\nThe trade has been cancelled."
            t1_prefix = ""
            t2_prefix = ""
            show_buttons = False
        elif self.confirmation_phase:
            container.accent_colour = discord.Colour.gold()
            header = "**Cricketers trading**\nBoth users locked their propositions! Now confirm to conclude this trade."
            t1_prefix = "🔒 "
            t2_prefix = "🔒 "
            show_buttons = True
        else:
            container.accent_colour = discord.Colour.blue()
            add_cmd = self.cog.add.extras.get("mention", "`/trade add`")
            del_cmd = self.cog.remove.extras.get("mention", "`/trade remove`")
            invite_prefix = f"{self.invite_mention}\n\n" if self.invite_mention else ""
            header = (
                f"{invite_prefix}**Cricketers trading**\n"
                f"Add or remove cricketers you want to propose to the other player using the "
                f"{add_cmd} and {del_cmd} commands. Once you're finished, click the lock button "
                f"below to confirm your proposal. You can also lock with nothing if you're receiving a gift.\n\n"
                f"*This trade will timeout in 30 minutes.*"
            )
            t1_prefix = "🔒 " if t1.locked else ""
            t2_prefix = "🔒 " if t2.locked else ""
            show_buttons = True

        container.add_item(TextDisplay(header))
        container.add_item(Separator())

        container.add_item(TextDisplay(f"**{t1_prefix}{t1.user.display_name}**"))
        await t1.render_proposal(container)

        container.add_item(Separator())

        container.add_item(TextDisplay(f"**{t2_prefix}{t2.user.display_name}**"))
        await t2.render_proposal(container)

        container.add_item(TextDisplay(
            "-# This message is updated every 15 seconds, but you can keep on editing your proposal."
        ))

        if show_buttons:
            self.buttons.clear_items()
            if self.confirmation_phase:
                self.buttons.add_item(self.confirm_button)
            else:
                self.buttons.add_item(self.lock_button)
                self.buttons.add_item(self.clear_button)
            self.buttons.add_item(self.cancel_button)
            container.add_item(self.buttons)

        if not self.active:
            for item in container._children:
                if hasattr(item, "disabled"):
                    item.disabled = True  # type: ignore

        self.add_item(container)

    @property
    def cancelled(self):
        return self.trader1.cancelled or self.trader2.cancelled

    @property
    def active(self):
        return not self.is_finished() and not self.cancelled

    @property
    def confirmation_phase(self):
        return self.trader1.locked and self.trader2.locked and not self.cancelled

    async def edit_message(self, interaction: Interaction | None):
        """Edit the main message, rate-limited."""

        async def refresh():
            await self._rebuild_view()

        if interaction is not None:
            self.next_edit_interaction = interaction
        if self.edit_lock.locked():
            return
        async with self.edit_lock:
            if self.next_edit_interaction is None:
                await asyncio.sleep(0.5)
                await refresh()
                await self.message.edit(view=self)
                return
            while self.next_edit_interaction is not None:
                inter = self.next_edit_interaction
                self.next_edit_interaction = None
                await asyncio.sleep(0.5)
                await refresh()
                if self.is_finished():
                    for child in self.walk_children():
                        if hasattr(child, "disabled"):
                            child.disabled = True  # type: ignore
                await inter.edit_original_response(view=self)
                if self.is_finished():
                    break

    @transaction.atomic()
    def perform_trade_operation(self) -> Trade:
        assert self.confirmation_phase
        assert self.trader1.confirmed and self.trader2.confirmed
        trade_objects: list[TradeObject] = []
        balls: list[BallInstance] = []
        trade = Trade.objects.create(player1=self.trader1.player, player2=self.trader2.player)

        def money_check(trader: TradingUser) -> Player:
            player = Player.objects.select_for_update(nowait=True).get(id=trader.player.pk)
            if not player.can_afford(trader.money):
                raise IntegrityError()
            return player

        def queryset_for_update(trader: TradingUser):
            return trader.get_queryset().select_for_update(nowait=True, of=("self",)).only("player__discord_id")

        for cricketer in queryset_for_update(self.trader1):
            if cricketer.player.discord_id != self.trader1.player.discord_id:
                raise IntegrityError()
            cricketer.player = self.trader2.player
            cricketer.trade_player = self.trader1.player
            cricketer.favorite = False
            cricketer.locked = None
            balls.append(cricketer)
            trade_objects.append(TradeObject(trade=trade, ballinstance=cricketer, player=self.trader1.player))

        for cricketer in queryset_for_update(self.trader2):
            if cricketer.player.discord_id != self.trader2.player.discord_id:
                raise IntegrityError()
            cricketer.player = self.trader1.player
            cricketer.trade_player = self.trader2.player
            cricketer.favorite = False
            cricketer.locked = None
            balls.append(cricketer)
            trade_objects.append(TradeObject(trade=trade, ballinstance=cricketer, player=self.trader2.player))

        if self.trader1.money or self.trader2.money:
            player1 = money_check(self.trader1)
            player2 = money_check(self.trader2)
            player1.money += self.trader2.money - self.trader1.money
            player2.money += self.trader1.money - self.trader2.money
            player1.save(update_fields=("money",))
            player2.save(update_fields=("money",))

        BallInstance.objects.bulk_update(balls, fields=("player", "trade_player", "favorite", "locked"))
        TradeObject.objects.bulk_create(trade_objects)
        return trade

    async def finish_trade(self):
        if self.confirmation_lock.locked():
            raise SynchronizationError()
        await self.confirmation_lock.acquire()
        self.timeout_task.cancel()
        trade = await sync_to_async(self.perform_trade_operation)()
        self.stop()

    async def _cleanup(self):
        self.stop()
        await BallInstance.objects.filter(id__in=self.trader1.proposal | self.trader2.proposal).aupdate(locked=None)
        for item in self.walk_children():
            if hasattr(item, "disabled"):
                item.disabled = True  # type: ignore

    async def cleanup(self):
        self.timeout_task.cancel()
        await self._cleanup()

    async def admin_cancel(self, reason: str):
        await self.cleanup()
        self.clear_items()
        self.add_item(
            TextDisplay(f"Trading has been globally disabled by administrators for the following reason: {reason}")
        )
        await self.message.edit(view=self)
