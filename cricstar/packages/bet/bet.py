"""
Core betting logic.

BettingUser  – lightweight data holder for one user's side of a bet.
BetInstance  – LayoutView that renders the entire bet in one Container.

Flow
────
1. /bet begin  → BetInstance.configure() creates the view and sends the message.
2. Each user adds cards via /bet add (locked in DB immediately).
3. When both click "Lock proposal" → confirmation phase (✅ appears next to each name
   as they confirm).
4. Both click "Confirm" → finish_bet() picks a random winner,
   transfers all of the loser's staked cards to the winner atomically,
   then displays the result: only the winner's name (plain, no emoji) and
   the combined list of all cards they won.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import discord
from asgiref.sync import sync_to_async
from discord.ui import ActionRow, Button, Container, Item, Separator, TextDisplay
from discord.utils import format_dt
from django.db import transaction
from django.utils import timezone

from cricstar.core.discord import UNKNOWN_INTERACTION, LayoutView
from cricstar.core.utils.buttons import ConfirmChoiceView
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


# ─────────────────────────────────────────────────────────────────
#  BettingUser — data + API only, no rendering
# ─────────────────────────────────────────────────────────────────


class BettingUser:
    def __init__(self, player: Player, user: discord.abc.User):
        self.player = player
        self.user = user
        self.proposal: set[int] = set()
        self.locked: bool = False
        self.cancelled: bool = False
        self.confirmed: bool = False

    def __repr__(self) -> str:
        return f"<BettingUser player_id={self.player.pk} discord_id={self.user.id}>"

    def get_queryset(self) -> QuerySet[BallInstance]:
        if not self.proposal:
            return BallInstance.objects.none()
        return BallInstance.objects.filter(id__in=self.proposal)

    async def card_list_text(self, bot: CricStarBot) -> str:
        from cricstar.core.utils.emojis import get_player_emoji

        if not self.proposal:
            return "*Empty*"
        lines = []
        async for ball in (
            self.get_queryset().select_related("ball").prefetch_related("special")
        ):
            desc = ball.description(include_emoji=True, bot=bot, is_trade=True)
            emoji_str = get_player_emoji(ball.ball.country)
            if emoji_str:
                desc = f"{emoji_str}{desc}"
            lines.append(f"• {desc}")
        return "\n".join(lines) if lines else "*Empty*"

    # ── API ──────────────────────────────────────────────────────

    async def add_to_proposal(self, queryset: QuerySet[BallInstance]):
        if self.locked:
            raise LockedError()
        if self.cancelled:
            raise CancelledError()
        proposal: set[int] = set()
        async for ball in queryset.only(
            "id",
            "locked",
            "player_id",
            "tradeable",
            "ball__tradeable",
            "special__tradeable",
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

    async def remove_from_proposal(self, queryset: QuerySet[BallInstance]):
        if self.locked:
            raise LockedError()
        if self.cancelled:
            raise CancelledError()
        ids: set[int] = {x.pk async for x in queryset.only("pk")}
        if not ids.issubset(self.proposal):
            raise NotProposedError()
        self.proposal.difference_update(ids)
        await queryset.aupdate(locked=None)

    async def clear(self):
        if self.locked:
            raise LockedError()
        if self.cancelled:
            raise CancelledError()
        await self.get_queryset().aupdate(locked=None)
        self.proposal.clear()


# ─────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────


async def _combined_card_text(all_ids: set[int], bot: CricStarBot) -> str:
    """Return a card list string for a combined set of ball IDs."""
    from cricstar.core.utils.emojis import get_player_emoji

    if not all_ids:
        return "*No cards staked*"
    lines = []
    async for ball in (
        BallInstance.objects.filter(id__in=all_ids)
        .select_related("ball")
        .prefetch_related("special")
    ):
        desc = ball.description(include_emoji=True, bot=bot, is_trade=True)
        emoji_str = get_player_emoji(ball.ball.country)
        if emoji_str:
            desc = f"{emoji_str}{desc}"
        lines.append(f"• {desc}")
    return "\n".join(lines) if lines else "*No cards staked*"


# ─────────────────────────────────────────────────────────────────
#  BetInstance — the LayoutView, handles all rendering
# ─────────────────────────────────────────────────────────────────


class BetInstance(LayoutView):
    """
    A running bet between two players.

    Renders as a single Container with two player sections inside,
    and action buttons outside it.
    """

    def __init__(self, cog: BetCog):
        # No discord.py built-in timeout — we manage our own lifecycle via _timeout_task
        super().__init__(timeout=None)
        self.cog = cog
        self.bettor1: BettingUser
        self.bettor2: BettingUser
        self.message: discord.Message

        self._resolved: bool = False  # True once finish_bet() has run
        self._timed_out: bool = False  # True once the custom timeout fires
        self.edit_lock = asyncio.Lock()
        self.confirmation_phase_start: datetime | None = None

        self._timeout_task = asyncio.create_task(
            self._timeout(), name=f"bet-timeout-{id(self)}"
        )

    # ── Properties ──────────────────────────────────────────────

    @property
    def cancelled(self) -> bool:
        return self.bettor1.cancelled or self.bettor2.cancelled

    @property
    def active(self) -> bool:
        """True while the bet is still accepting interactions."""
        return not self._timed_out and not self.cancelled and not self._resolved

    @property
    def confirmation_phase(self) -> bool:
        return self.bettor1.locked and self.bettor2.locked and not self.cancelled

    # ── Error handling a��──────────────────────────────────────────

    async def on_error(
        self, interaction: Interaction, error: Exception, item: Item
    ) -> None:
        if isinstance(error, discord.NotFound) and error.code in UNKNOWN_INTERACTION:
            log.warning("Expired bet interaction", exc_info=error)
            return
        log.exception(
            f"Error in bet between {self.bettor1} and {self.bettor2}", exc_info=error
        )
        await self._do_cleanup()
        send = (
            interaction.followup.send
            if interaction.response.is_done()
            else interaction.response.send_message
        )
        await send("An error occurred, the bet will be cancelled.", ephemeral=True)
        self.clear_items()
        self.add_item(
            TextDisplay(
                "An error occurred and the bet has been cancelled! Contact support if this persists."
            )
        )
        await self.message.edit(view=self)

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id not in (self.bettor1.user.id, self.bettor2.user.id):
            await interaction.response.send_message(
                "You are not part of this bet!", ephemeral=True
            )
            return False
        return True

    # ── Timeout ──────────────────────────────────────────────────

    async def _timeout(self):
        await asyncio.sleep(BET_TIMEOUT)
        if self.active:
            self._timed_out = True
            await self.cleanup()

    # ── Rendering ────────────────────────────────────────────────

    async def _build_view(self):
        """Rebuild the entire LayoutView from scratch."""
        self.clear_items()

        container = Container(accent_colour=self._accent_colour())
        await self._fill_container(container)
        self.add_item(container)

        # Buttons live outside the container (hidden once resolved)
        if not self._resolved and not self.cancelled and not self._timed_out:
            row = self._build_button_row()
            self.add_item(row)

    def _accent_colour(self) -> discord.Colour:
        if self._resolved:
            return discord.Colour.green()
        if self.cancelled or self._timed_out:
            return discord.Colour.red()
        if self.confirmation_phase:
            return discord.Colour.gold()
        return discord.Colour.blue()

    async def _fill_container(self, container: Container):
        b1, b2 = self.bettor1, self.bettor2

        # ── Resolved: show ONLY the winner, no loser section ────────────────
        if self._resolved:
            container.add_item(TextDisplay(self._result_header))
            container.add_item(Separator())

            winner = b1 if b1.user.id == self._winner_user_id else b2
            loser = b2 if b1.user.id == self._winner_user_id else b1

            # Winner's name — plain, no emoji prefix
            container.add_item(TextDisplay(f"**{winner.user.display_name}**"))

            # All cards the winner now holds (their stake + loser's captured cards)
            all_won_ids = winner.proposal | loser.proposal
            container.add_item(
                TextDisplay(await _combined_card_text(all_won_ids, self.cog.bot))
            )
            return

        # ── Cancelled / timed-out ────────────────────────────────────────────
        if self.cancelled or self._timed_out:
            reason = (
                "The bet has been cancelled."
                if self.cancelled
                else "The bet timed out."
            )
            container.add_item(TextDisplay(f"## CricStar Betting\n{reason}"))
            return

        # ── Intro mention (only while bet is live) ───────────────────────────
        container.add_item(
            TextDisplay(
                f"Hey {b2.user.mention}, **{b1.user.display_name}** is proposing a CricStar Bet with you!"
            )
        )
        container.add_item(Separator())

        # ── Header ───────────────────────────────────────────────────────────
        if self.confirmation_phase:
            header = (
                "## CricStar Betting\n"
                "Both users locked their propositions!\n"
                "Now confirm to conclude this bet."
            )
        else:
            header = (
                "## CricStar Betting\n"
                f"Add or remove {settings.plural_collectible_name} you want to propose to the other player using "
                f"`/bet add` and `/bet remove` commands.\n"
                "Once you're finished, click the lock button below to confirm your proposal.\n"
                "You can also lock with nothing if it's an empty bet.\n"
                "NOTE: This is a randomly selected 50/50 chance, it is NOT influenced by: "
                "Past results, ticking times, send order or other factors.\n\n"
                "*This bet will timeout in 30 minutes.*"
            )

        container.add_item(TextDisplay(header))
        container.add_item(Separator())

        # ── Bettor 1 ─────────────────────────────────────────────────────────
        b1_prefix = self._live_prefix(b1)
        container.add_item(TextDisplay(f"**{b1_prefix}{b1.user.display_name}**"))
        container.add_item(TextDisplay(await b1.card_list_text(self.cog.bot)))
        container.add_item(Separator())

        # ── Bettor 2 ─────────────────────────────────────────────────────────
        b2_prefix = self._live_prefix(b2)
        container.add_item(TextDisplay(f"**{b2_prefix}{b2.user.display_name}**"))
        container.add_item(TextDisplay(await b2.card_list_text(self.cog.bot)))

        container.add_item(Separator())
        container.add_item(
            TextDisplay(
                "-# This message is updated every 15 seconds, but you can keep on editing your proposal."
            )
        )

    def _live_prefix(self, bettor: BettingUser) -> str:
        """Emoji prefix shown while the bet is live (not yet resolved)."""
        if bettor.cancelled:
            return "❌ "
        if bettor.confirmed:
            return "✅ "
        if bettor.locked:
            return "🔒 "
        return ""

    def _build_button_row(self) -> ActionRow:
        row = ActionRow()
        if self.confirmation_phase:
            confirm = Button(
                label="Confirm",
                emoji="\N{HEAVY CHECK MARK}\N{VARIATION SELECTOR-16}",
                style=discord.ButtonStyle.success,
            )
            confirm.callback = self._confirm_callback
            row.add_item(confirm)

            cancel = Button(
                label="Cancel bet",
                emoji="\N{HEAVY MULTIPLICATION X}\N{VARIATION SELECTOR-16}",
                style=discord.ButtonStyle.danger,
            )
            cancel.callback = self._cancel_callback
            row.add_item(cancel)
        else:
            lock = Button(
                label="Lock proposal",
                emoji="\N{HEAVY CHECK MARK}\N{VARIATION SELECTOR-16}",
                style=discord.ButtonStyle.success,
            )
            lock.callback = self._lock_callback
            row.add_item(lock)

            reset = Button(
                label="Reset",
                emoji="\N{DASH SYMBOL}",
                style=discord.ButtonStyle.secondary,
            )
            reset.callback = self._reset_callback
            row.add_item(reset)

            cancel = Button(
                label="Cancel bet",
                emoji="\N{HEAVY MULTIPLICATION X}\N{VARIATION SELECTOR-16}",
                style=discord.ButtonStyle.danger,
            )
            cancel.callback = self._cancel_callback
            row.add_item(cancel)
        return row

    # ── Button callbacks (dynamic) ────────────────────────────────

    async def _lock_callback(self, interaction: Interaction):
        bettor = self._get_bettor(interaction.user.id)
        if bettor is None:
            await interaction.response.send_message(
                "You are not part of this bet!", ephemeral=True
            )
            return
        if bettor.locked:
            await interaction.response.send_message(
                "You have already locked your proposal!", ephemeral=True
            )
            return
        await interaction.response.defer()
        bettor.locked = True
        if self.confirmation_phase and self.confirmation_phase_start is None:
            self.confirmation_phase_start = datetime.now()
        await self._update(interaction)
        await interaction.followup.send(
            "Your proposal has been locked. Now confirm again to end the bet.",
            ephemeral=True,
        )

    async def _reset_callback(self, interaction: Interaction):
        bettor = self._get_bettor(interaction.user.id)
        if bettor is None:
            await interaction.response.send_message(
                "You are not part of this bet!", ephemeral=True
            )
            return
        if bettor.locked:
            await interaction.response.send_message(
                "You have already locked your proposal!", ephemeral=True
            )
            return
        view = ConfirmChoiceView(
            interaction, accept_message="Clearing your proposal..."
        )
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
            await self._update(None)

    async def _cancel_callback(self, interaction: Interaction):
        bettor = self._get_bettor(interaction.user.id)
        if bettor is None:
            await interaction.response.send_message(
                "You are not part of this bet!", ephemeral=True
            )
            return
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
        bettor.cancelled = True
        await self._cleanup_cards()
        await self._update(None)
        await self._do_cleanup()

    async def _confirm_callback(self, interaction: Interaction):
        bettor = self._get_bettor(interaction.user.id)
        if bettor is None:
            await interaction.response.send_message(
                "You are not part of this bet!", ephemeral=True
            )
            return
        if not self.confirmation_phase:
            await interaction.response.send_message(
                "Both players need to lock their proposals first!", ephemeral=True
            )
            return
        bettor.confirmed = True
        await interaction.response.send_message(
            "Your confirmation has been recorded. Waiting for the other player...",
            ephemeral=True,
        )

        if self.bettor1.confirmed and self.bettor2.confirmed:
            # Both confirmed — resolve the bet
            await self.finish_bet()
        else:
            await self._update(None)

    def _get_bettor(self, user_id: int) -> BettingUser | None:
        if self.bettor1.user.id == user_id:
            return self.bettor1
        if self.bettor2.user.id == user_id:
            return self.bettor2
        return None

    # ── State updates ────────────────────────────────────────────

    async def _update(self, interaction: Interaction | None):
        """Rebuild the view and edit the message. Safe to call after stop()."""
        async with self.edit_lock:
            if self._resolved:
                return  # finish_bet handles the final message
            await self._build_view()
            try:
                await self.message.edit(view=self)
            except discord.NotFound:
                pass

    async def edit_message(self, interaction: Interaction | None):
        """Public alias used by the cog after add/remove/bulk_add."""
        await self._update(interaction)

    # ── Lifecycle ────────────────────────────────────────────────

    @classmethod
    def configure(
        cls,
        cog: BetCog,
        bettor1: tuple[Player, discord.abc.User],
        bettor2: tuple[Player, discord.abc.User],
    ) -> BetInstance:
        bet = cls(cog)
        bet.bettor1 = BettingUser(*bettor1)
        bet.bettor2 = BettingUser(*bettor2)
        bet._result_header = ""
        bet._winner_user_id = None
        return bet

    async def start(self, channel: discord.abc.Messageable) -> discord.Message:
        """Build the initial view and send it as a single message."""
        await self._build_view()
        msg = await channel.send(view=self)
        self.message = msg
        return msg

    async def cleanup(self):
        """Public cleanup — called by cog.get_bet() and timeout handler."""
        await self._cleanup_cards()
        self._do_cleanup_sync()

    async def _do_cleanup(self):
        """Internal cleanup: deregister from cog and stop."""
        self._do_cleanup_sync()

    def _do_cleanup_sync(self):
        """Remove this bet from the cog's tracking dict."""
        msg = getattr(self, "message", None)
        if msg is not None:
            channel_bets = self.cog.bets.get(msg.channel.id, {})
            channel_bets.pop(self.bettor1.user.id, None)
            channel_bets.pop(self.bettor2.user.id, None)
        # Cancel our custom timeout task if it's still running
        if not self._timeout_task.done():
            self._timeout_task.cancel()

    async def _cleanup_cards(self):
        all_ids = self.bettor1.proposal | self.bettor2.proposal
        if all_ids:
            await BallInstance.objects.filter(id__in=all_ids).aupdate(locked=None)

    async def admin_cancel(self, reason: str):
        await self._cleanup_cards()
        self._do_cleanup_sync()
        try:
            self.clear_items()
            self.add_item(TextDisplay(f"This bet was cancelled by an admin: {reason}"))
            await self.message.edit(view=self)
        except Exception:
            pass

            # ── Resolution ───────────────────────────────────────────────
    async def finish_bet(self):
        """
        Pick a random winner, atomically transfer the loser's cards,
        then post the result message.
        """
        self._resolved = True
        self._do_cleanup_sync()

        winner, loser = random.choice(
            [(self.bettor1, self.bettor2), (self.bettor2, self.bettor1)]
        )
        self._winner_user_id = winner.user.id

        @sync_to_async
        def _transfer() -> tuple[int, int]:
            transferred = 0
            missing = 0
            with transaction.atomic():
                if loser.proposal:
                    loser_qs = BallInstance.objects.select_for_update().filter(
                        id__in=loser.proposal,
                        player_id=loser.player.pk,
                        deleted=False,
                    )
                    owned_ids = list(loser_qs.values_list("id", flat=True))
                    transferred = len(owned_ids)
                    missing = len(loser.proposal) - transferred
                    if owned_ids:
                        BallInstance.objects.filter(id__in=owned_ids).update(
                            player_id=winner.player.pk,
                            trade_player_id=loser.player.pk,
                            locked=None,
                        )
                    stale_ids = set(loser.proposal) - set(owned_ids)
                    if stale_ids:
                        BallInstance.objects.filter(id__in=stale_ids).update(locked=None)

                if winner.proposal:
                    BallInstance.objects.select_for_update().filter(
                        id__in=winner.proposal,
                        deleted=False,
                    ).update(
                        locked=None,
                        trade_player_id=winner.player.pk,
                    )
            return transferred, missing

        msg = getattr(self, "message", None)

        try:
            transferred, missing = await _transfer()
        except Exception:
            log.exception("Unexpected error while resolving bet, refunding all cards")
            await self._cleanup_cards()
            self.clear_items()
            self.add_item(
                TextDisplay(
                    "⚠️ The bet could not be resolved due to an internal error.\n"
                    "All staked cards have been returned to their owners."
                )
            )
            if msg:
                await msg.edit(view=self)
            return

        self._result_header = (
            f"## CricStar Betting\n"
            f"*The winner is* ***{winner.user.display_name}***"
        )
        if missing > 0:
            self._result_header += (
                f"\n-# Note: {missing} staked card(s) were no longer available "
                f"and were skipped."
            )

        await self._build_view()
        if msg:
            await msg.edit(view=self)
    
