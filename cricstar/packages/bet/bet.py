"""
Core betting logic. BettingUser and BetInstance are the two main classes.

BetInstance doubles as the discord LayoutView that is sent to the channel.
Each user adds cards to their proposal (staked cards). When both lock and
confirm, a winner is picked at random and receives all cards from both proposals.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast

import discord
from asgiref.sync import sync_to_async
from discord.ui import ActionRow, Button, Item, Section, Separator, TextDisplay, Thumbnail
from discord.utils import format_dt
from django.db import transaction
from django.utils import timezone

from cricstar.core.discord import UNKNOWN_INTERACTION, Container, LayoutView
from cricstar.core.utils.buttons import ConfirmChoiceView
from cricstar.core.utils.menus import CountryballFormatter, Menu, ModelSource, TextFormatter, TextSource
from bd_models.models import BallInstance, Player
from settings.models import settings

from .errors import (
    AlreadyLockedError,
    BetError,
    CancelledError,
    IntegrityError,
    LockedError,
    NotProposedError,
    NotTradeableError,
    OwnershipError,
)

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from cricstar.core.bot import CricStarBot

    from .cog import Bet as BetCog

type Interaction = discord.Interaction[CricStarBot]

log = logging.getLogger(__name__)

BET_TIMEOUT = 60 * 30  # 30 minutes


# ─────────────────────────────────────────────
#  One user's side of a bet
# ─────────────────────────────────────────────


class BettingUser(Container):
    """Represent one user's side of a bet."""

    def __init__(self, bet: BetInstance, player: Player, user: discord.abc.User):
        super().__init__()
        self.bet = bet
        self.cog = bet.cog
        self.player = player
        self.user = user
        self.proposal: set[int] = set()
        self.locked: bool = False
        self.cancelled: bool = False
        self.confirmed: bool = False

        self.menu = Menu(
            self.cog.bot,
            bet,
            ModelSource(self.get_queryset()),
            CountryballFormatter(self.select_menu, max_values=25),
        )

        self.view: BetInstance

    def __repr__(self) -> str:
        return f"<BettingUser player_id={self.player.pk} discord_id={self.user.id}>"

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id not in (self.bet.bettor1.user.id, self.bet.bettor2.user.id):
            await interaction.response.send_message("You are not part of this bet!", ephemeral=True)
            return False
        return True

    # ── Utils ──────────────────────────────────────────────────────────────────

    def get_queryset(self) -> QuerySet[BallInstance]:
        if not self.proposal:
            return BallInstance.objects.none()
        return BallInstance.objects.filter(id__in=self.proposal)

    # ── Container items ────────────────────────────────────────────────────────

    proposal_list = TextDisplay("")
    select_row = ActionRow()

    @select_row.select(placeholder="Click to remove an item", min_values=1)
    async def select_menu(self, interaction: Interaction, select: discord.ui.Select):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                "You are not allowed to do this — edit your own bet proposal.", ephemeral=True
            )
            return
        await interaction.response.defer()
        try:
            await self.remove_from_proposal(BallInstance.objects.filter(id__in=(int(x) for x in select.values)))
        except BetError as e:
            await interaction.followup.send(e.error_message, ephemeral=True)
        else:
            await self.view.edit_message(interaction)

    # ── Display helpers ────────────────────────────────────────────────────────

    async def refresh_container(self):
        self.clear_items()

        section = Section(
            TextDisplay(f"## {self.user.display_name}"),
            accessory=Thumbnail(self.user.display_avatar.url),
        )

        if self.view.cancelled:
            if self.cancelled:
                self.accent_colour = discord.Colour.red()
                section.add_item(TextDisplay("You have cancelled the bet."))
            else:
                section.add_item(TextDisplay("The bet has been cancelled."))
        elif self.confirmed:
            self.accent_colour = discord.Colour.green()
            section.add_item(TextDisplay("You have confirmed your bet proposal."))
        elif self.view.confirmation_phase:
            self.accent_colour = discord.Colour.gold()
            section.add_item(TextDisplay("Both proposals locked. Review and confirm to resolve the bet!"))
        elif self.locked:
            self.accent_colour = discord.Colour.yellow()
            section.add_item(
                TextDisplay(
                    "You have locked your proposal. "
                    "Waiting for the other player to lock before finishing the bet."
                )
            )
        else:
            self.accent_colour = discord.Colour.blue()
            add_cmd = self.cog.add.extras.get("mention", "`/bet add`")
            del_cmd = self.cog.remove.extras.get("mention", "`/bet remove`")
            section.add_item(
                TextDisplay(
                    f"Add or remove {settings.plural_collectible_name} to bet using {add_cmd} and {del_cmd}.\n"
                    f"Once you're finished, click the lock button below to confirm your proposal.\n"
                    f"*This bet will timeout in 30 minutes.*"
                )
            )

        section.add_item(TextDisplay(f"-# {len(self.proposal)} {settings.plural_collectible_name} staked"))
        self.add_item(section)
        self.add_item(Separator())

        self.select_menu.disabled = self.locked or self.view.cancelled

        if not self.locked:
            self.select_menu.options.clear()  # pyright: ignore[reportAttributeAccessIssue]
            self.add_item(self.select_row)
            if self.proposal:
                cast(ModelSource, self.menu.source).queryset = self.get_queryset().order_by("locked")
                await self.menu.init(container=self)
            else:
                self.select_menu.add_option(label="Nothing yet")
                self.select_menu.disabled = True
                self.select_menu.max_values = 1
        else:
            self.add_item(self.proposal_list)
            if self.proposal:
                await self.menu.init(container=self)

        if not self.view.active:
            for item in self.walk_children():
                if hasattr(item, "disabled"):
                    item.disabled = True  # type: ignore

    # ── API functions ──────────────────────────────────────────────────────────

    async def add_to_proposal(self, queryset: QuerySet[BallInstance]):
        """Add cricketers to this user's bet proposal, locking them in the DB."""
        if self.locked:
            raise LockedError()
        if self.view.cancelled:
            raise CancelledError()

        proposal: set[int] = set()
        async for ball in queryset.only("id", "locked", "player_id", "tradeable", "ball__tradeable", "special__tradeable"):
            if ball.player_id != self.player.pk:
                raise OwnershipError()
            if await ball.is_locked(refresh=False):
                raise AlreadyLockedError()
            if not ball.is_tradeable:
                raise NotTradeableError()
            proposal.add(ball.pk)

        await queryset.aupdate(locked=timezone.now())
        self.proposal.update(proposal)

    async def remove_from_proposal(self, queryset: QuerySet[BallInstance]):
        """Remove cricketers from this user's proposal and unlock them."""
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
        if self.locked:
            raise LockedError()
        if self.view.cancelled:
            raise CancelledError()
        self.locked = True

        if not self.proposal:
            self.proposal_list.content = "*Empty*"
            return

        text = ""
        async for ball in self.get_queryset().prefetch_related("special"):
            text += f"- {ball.description(include_emoji=True, bot=self.cog.bot, is_trade=True)}\n"
        self.menu = Menu(self.cog.bot, self.view, TextSource(text, page_length=1800), TextFormatter(self.proposal_list))

    async def clear(self):
        if self.locked:
            raise LockedError()
        if self.view.cancelled:
            raise CancelledError()
        await self.get_queryset().aupdate(locked=None)
        self.proposal.clear()

    async def cancel(self):
        self.cancelled = True
        self.view.stop()
        await self.view.cleanup()

    async def confirm(self):
        assert self.view.confirmation_phase is True
        self.confirmed = True
        if self.view.bettor1.confirmed and self.view.bettor2.confirmed:
            await self.view.finish_bet()


# ─────────────────────────────────────────────
#  The bet instance (LayoutView)
# ─────────────────────────────────────────────


class BetInstance(LayoutView):
    """A running bet between two players. Also serves as the Discord LayoutView."""

    def __init__(self, cog: BetCog):
        super().__init__(timeout=BET_TIMEOUT)
        self.cog = cog
        self.bettor1: BettingUser
        self.bettor2: BettingUser
        self.message: discord.Message

        self.edit_lock = asyncio.Lock()
        self.next_edit_interaction: Interaction | None = None
        self.confirmation_phase_start: datetime | None = None

        self.timeout_task = asyncio.create_task(self._timeout(), name=f"bet-timeout-{id(self)}")

    async def on_error(self, interaction: Interaction, error: Exception, item: Item) -> None:
        if isinstance(error, discord.NotFound) and error.code in UNKNOWN_INTERACTION:
            log.warning("Expired interaction", exc_info=error)
            return
        log.exception(f"Error in bet between {self.bettor1} and {self.bettor2}", exc_info=error)
        await self.cleanup()
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send("An error occurred, the bet will be cancelled.", ephemeral=True)
        self.clear_items()
        self.add_item(TextDisplay("An error occurred and the bet has been cancelled! Contact support if this persists."))
        await self.message.edit(view=self)

    async def _timeout(self):
        await asyncio.sleep(BET_TIMEOUT)
        if self.active:
            await self._cleanup()

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id not in (self.bettor1.user.id, self.bettor2.user.id):
            await interaction.response.send_message("You are not part of this bet!", ephemeral=True)
            return False
        return True

    buttons = ActionRow()

    @buttons.button(label="Lock proposal", emoji="\N{LOCK}", style=discord.ButtonStyle.primary)
    async def lock_button(self, interaction: Interaction, button: Button):
        bettor = {self.bettor1.user.id: self.bettor1, self.bettor2.user.id: self.bettor2}[interaction.user.id]
        if bettor.locked:
            await interaction.response.send_message("You have already locked your proposal!", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            await bettor.lock()
        except BetError as e:
            await interaction.followup.send(e.error_message, ephemeral=True)
        else:
            if self.confirmation_phase and self.confirmation_phase_start is None:
                self.confirmation_phase_start = datetime.now()
            await self.edit_message(interaction)

    @buttons.button(label="Reset", emoji="\N{DASH SYMBOL}", style=discord.ButtonStyle.secondary)
    async def clear_button(self, interaction: Interaction, button: Button):
        bettor = {self.bettor1.user.id: self.bettor1, self.bettor2.user.id: self.bettor2}[interaction.user.id]
        if bettor.locked:
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
            await bettor.clear()
        except BetError as e:
            await interaction.followup.send(e.error_message, ephemeral=True)
        else:
            await self.edit_message(None)

    @buttons.button(
        label="Cancel bet",
        emoji="\N{HEAVY MULTIPLICATION X}\N{VARIATION SELECTOR-16}",
        style=discord.ButtonStyle.danger,
    )
    async def cancel_button(self, interaction: Interaction, button: Button):
        bettor = {self.bettor1.user.id: self.bettor1, self.bettor2.user.id: self.bettor2}[interaction.user.id]
        view = ConfirmChoiceView(
            interaction,
            accept_message="Cancelling the bet...",
            cancel_message="Request cancelled.",
        )
        await interaction.response.send_message(
            "Are you sure you want to cancel this bet?", view=view, ephemeral=True
        )
        await view.wait()
        if not view.value:
            return
        try:
            await bettor.cancel()
        except BetError as e:
            await interaction.followup.send(e.error_message, ephemeral=True)
        else:
            await self.edit_message(None)

    @buttons.button(
        label="Confirm",
        emoji="\N{HEAVY CHECK MARK}\N{VARIATION SELECTOR-16}",
        style=discord.ButtonStyle.success,
    )
    async def confirm_button(self, interaction: Interaction, button: Button):
        bettor = {self.bettor1.user.id: self.bettor1, self.bettor2.user.id: self.bettor2}[interaction.user.id]
        await interaction.response.defer()
        try:
            await bettor.confirm()
        except BetError as e:
            await interaction.followup.send(e.error_message, ephemeral=True)
        else:
            await self.edit_message(interaction)

    # ── Helpers ────────────────────────────────────────────────────────────────

    @classmethod
    def configure(
        cls,
        cog: BetCog,
        bettor1: tuple[Player, discord.abc.User],
        bettor2: tuple[Player, discord.abc.User],
    ) -> BetInstance:
        bet = cls(cog)
        bet.bettor1 = BettingUser(bet, *bettor1)
        bet.bettor2 = BettingUser(bet, *bettor2)
        bet.clear_items()
        bet.add_item(
            TextDisplay(f"Hey {bettor2[1].mention}, **{bettor1[1].display_name}** is proposing a CricStar Bet with you!")
        )
        bet.add_item(bet.bettor1)
        bet.add_item(bet.bettor2)
        # Confirm button only shown after both lock
        bet.buttons.remove_item(bet.confirm_button)
        bet.add_item(bet.buttons)
        timeout_dt = datetime.now() + timedelta(seconds=BET_TIMEOUT)
        bet.add_item(TextDisplay(f"-# This bet will timeout {format_dt(timeout_dt, style='R')}."))
        return bet

    @property
    def cancelled(self) -> bool:
        return self.bettor1.cancelled or self.bettor2.cancelled

    @property
    def active(self) -> bool:
        return not self.is_finished() and not self.cancelled

    @property
    def confirmation_phase(self) -> bool:
        return self.bettor1.locked and self.bettor2.locked and not self.cancelled

    async def edit_message(self, interaction: Interaction | None):
        """
        Rate-limit-safe edit helper (LIFO queue of depth 1).
        Rebuilds both user containers then edits the message.
        """
        async with self.edit_lock:
            # Rebuild confirmation-phase state (show/hide confirm button)
            if self.confirmation_phase:
                if self.confirm_button not in self.buttons.children:
                    self.buttons.add_item(self.confirm_button)

            await self.bettor1.refresh_container()
            await self.bettor2.refresh_container()

            if interaction:
                await interaction.edit_original_response(view=self)
            else:
                await self.message.edit(view=self)

    async def cleanup(self):
        """Unlock all staked cards and mark as finished."""
        await self._cleanup()

    async def _cleanup(self):
        all_ids = self.bettor1.proposal | self.bettor2.proposal
        if all_ids:
            await BallInstance.objects.filter(id__in=all_ids).aupdate(locked=None)
        self.stop()

        # Unregister from cog — message may not exist if bet failed at startup
        msg = getattr(self, "message", None)
        if msg is not None:
            channel_bets = self.cog.bets.get(msg.channel.id, {})
            channel_bets.pop(self.bettor1.user.id, None)
            channel_bets.pop(self.bettor2.user.id, None)

    async def admin_cancel(self, reason: str):
        await self._cleanup()
        try:
            self.clear_items()
            self.add_item(TextDisplay(f"This bet was cancelled by an admin: {reason}"))
            await self.message.edit(view=self)
        except Exception:
            pass

    async def finish_bet(self):
        """
        Resolve the bet. Pick a random winner, then transfer the loser's staked
        cards to the winner atomically with ownership verification.
        """
        # random winner — 50/50
        winner, loser = random.choice(
            [(self.bettor1, self.bettor2), (self.bettor2, self.bettor1)]
        )

        @sync_to_async
        def _transfer():
            with transaction.atomic():
                if loser.proposal:
                    # Lock loser's rows for update (anti-dupe)
                    loser_qs = BallInstance.objects.select_for_update().filter(
                        id__in=loser.proposal,
                        player_id=loser.player.pk,
                        deleted=False,
                    )
                    actual_count = loser_qs.count()
                    if actual_count != len(loser.proposal):
                        raise IntegrityError()

                    loser_qs.update(
                        player_id=winner.player.pk,
                        trade_player_id=loser.player.pk,
                        locked=None,
                    )

                if winner.proposal:
                    # Unlock winner's own cards
                    BallInstance.objects.select_for_update().filter(
                        id__in=winner.proposal,
                        player_id=winner.player.pk,
                        deleted=False,
                    ).update(locked=None)

        try:
            await _transfer()
        except IntegrityError as e:
            # Ownership changed mid-bet → cancel for safety
            await self._cleanup()
            self.clear_items()
            self.add_item(
                TextDisplay(
                    "⚠️ The bet was cancelled because card ownership changed during the bet. "
                    "All staked cards have been returned."
                )
            )
            await self.message.edit(view=self)
            return

        self.stop()

        # Unregister from cog
        msg = getattr(self, "message", None)
        if msg is not None:
            channel_bets = self.cog.bets.get(msg.channel.id, {})
            channel_bets.pop(self.bettor1.user.id, None)
            channel_bets.pop(self.bettor2.user.id, None)

        # Build result embed
        winner_lines = ""
        if winner.proposal:
            async for ball in BallInstance.objects.filter(id__in=winner.proposal).prefetch_related("special"):
                winner_lines += f"• {ball.description(include_emoji=True, bot=self.cog.bot, is_trade=True)}\n"
        else:
            winner_lines = "*Empty*\n"

        loser_lines = ""
        if loser.proposal:
            async for ball in BallInstance.objects.filter(id__in=loser.proposal).prefetch_related("special"):
                loser_lines += f"• {ball.description(include_emoji=True, bot=self.cog.bot, is_trade=True)}\n"
        else:
            loser_lines = "*Empty*\n"

        self.clear_items()
        self.add_item(
            TextDisplay(
                f"## CricStar Betting\n"
                f"**The winner is {winner.user.display_name}!** 🎉\n\n"
                f"✅ **{winner.user.display_name}** (winner — receives all)\n"
                f"{winner_lines}\n"
                f"❌ **{loser.user.display_name}** (loser)\n"
                f"{loser_lines}"
            )
        )
        await self.message.edit(view=self)
