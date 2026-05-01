from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from django.db.models import Q
from django.utils import timezone
from discord.utils import format_dt

from bd_models.models import BallInstance, Trade

if TYPE_CHECKING:
    from cricstar.core.bot import CricStarBot

log = logging.getLogger(__name__)

CATEGORIES = ["trades", "bets", "packs", "gives", "catches"]
CAT_LABELS = {
    "trades":  "📊 Trades",
    "bets":    "🎲 Bets",
    "packs":   "📦 Packs",
    "gives":   "🎁 Gives",
    "catches": "🏏 Catches",
}
PAGE_SIZE = 10


async def _get_trades(discord_id: str, days: int) -> list[str]:
    since = timezone.now() - timedelta(days=days)
    trades = Trade.objects.filter(
        Q(player1__discord_id=discord_id) | Q(player2__discord_id=discord_id),
        date__gte=since,
    ).prefetch_related("player1", "player2").order_by("-date")

    lines = []
    async for t in trades[:50]:
        other = t.player2.discord_id if str(t.player1.discord_id) == discord_id else t.player1.discord_id
        lines.append(f"`#{t.pk:0X}` {format_dt(t.date, 'R')} ↔ <@{other}>")
    return lines


async def _get_packs(discord_id: str, days: int) -> list[str]:
    since = timezone.now() - timedelta(days=days)
    qs = BallInstance.objects.filter(
        player__discord_id=discord_id,
        catch_date__gte=since,
        spawned_time__isnull=True,
        trade_player__isnull=True,
        server_id__isnull=False,
    ).select_related("ball", "special").order_by("-catch_date")

    lines = []
    async for bi in qs[:50]:
        special = f" ✦ {bi.special.name}" if bi.special_id else ""
        lines.append(f"`#{bi.pk:0X}` {format_dt(bi.catch_date, 'R')} — **{bi.ball.country}**{special}")
    return lines


async def _get_gives(discord_id: str, days: int) -> list[str]:
    since = timezone.now() - timedelta(days=days)
    qs = BallInstance.objects.filter(
        player__discord_id=discord_id,
        catch_date__gte=since,
        spawned_time__isnull=True,
        trade_player__isnull=True,
        server_id__isnull=True,
    ).select_related("ball", "special").order_by("-catch_date")

    lines = []
    async for bi in qs[:50]:
        special = f" ✦ {bi.special.name}" if bi.special_id else ""
        lines.append(f"`#{bi.pk:0X}` {format_dt(bi.catch_date, 'R')} — **{bi.ball.country}**{special}")
    return lines


async def _get_catches(discord_id: str, days: int) -> list[str]:
    since = timezone.now() - timedelta(days=days)
    qs = BallInstance.objects.filter(
        player__discord_id=discord_id,
        catch_date__gte=since,
        spawned_time__isnull=False,
        trade_player__isnull=True,
    ).select_related("ball", "special").order_by("-catch_date")

    lines = []
    async for bi in qs[:50]:
        special = f" ✦ {bi.special.name}" if bi.special_id else ""
        lines.append(f"`#{bi.pk:0X}` {format_dt(bi.catch_date, 'R')} — **{bi.ball.country}**{special}")
    return lines


class OwnerCheckView(discord.ui.View):
    def __init__(self, user: discord.User, days: int, data: dict[str, list[str]]):
        super().__init__(timeout=120)
        self.user = user
        self.days = days
        self.data = data
        self.current = "trades"
        self.page = 0
        self._refresh_buttons()

    def _refresh_buttons(self):
        self.clear_items()
        for cat in CATEGORIES:
            btn = discord.ui.Button(
                label=CAT_LABELS[cat],
                style=discord.ButtonStyle.primary if cat == self.current else discord.ButtonStyle.secondary,
                custom_id=f"cat_{cat}",
                row=0,
            )
            btn.callback = self._make_cat_callback(cat)
            self.add_item(btn)

        lines = self.data.get(self.current, [])
        total_pages = max(1, (len(lines) + PAGE_SIZE - 1) // PAGE_SIZE)

        prev_btn = discord.ui.Button(label="◀ Prev", style=discord.ButtonStyle.secondary, row=1, disabled=self.page == 0)
        prev_btn.callback = self._prev_page
        self.add_item(prev_btn)

        next_btn = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.secondary, row=1, disabled=self.page >= total_pages - 1)
        next_btn.callback = self._next_page
        self.add_item(next_btn)

    def _make_cat_callback(self, cat: str):
        async def callback(interaction: discord.Interaction):
            self.current = cat
            self.page = 0
            self._refresh_buttons()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
        return callback

    async def _prev_page(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _next_page(self, interaction: discord.Interaction):
        lines = self.data.get(self.current, [])
        total_pages = max(1, (len(lines) + PAGE_SIZE - 1) // PAGE_SIZE)
        self.page = min(total_pages - 1, self.page + 1)
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    def build_embed(self) -> discord.Embed:
        lines = self.data.get(self.current, [])
        total = len(lines)
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page_lines = lines[self.page * PAGE_SIZE : (self.page + 1) * PAGE_SIZE]

        colour_map = {
            "trades": discord.Colour.blue(),
            "bets":   discord.Colour.gold(),
            "packs":  discord.Colour.green(),
            "gives":  discord.Colour.purple(),
            "catches": discord.Colour.og_blurple(),
        }

        embed = discord.Embed(
            title=f"{CAT_LABELS[self.current]} — {self.user.display_name}",
            description=f"Last **{self.days}** days • {total} entries\n\n",
            colour=colour_map[self.current],
        )
        embed.set_thumbnail(url=self.user.display_avatar.url)

        if self.current == "bets":
            embed.description += "⚠️ Bet history is not stored in the database — it runs in memory only."
        elif not page_lines:
            embed.description += "*No records found.*"
        else:
            embed.description += "\n".join(page_lines)

        embed.set_footer(text=f"Page {self.page + 1}/{total_pages} • ID: {self.user.id}")
        return embed

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True  # type: ignore


@app_commands.command(name="ownercheck", description="[Admin] View full activity history for a user.")
@app_commands.describe(
    user="The Discord user to check",
    days="How many days back to look (default: 7)",
)
@app_commands.default_permissions(administrator=True)
async def ownercheck(interaction: discord.Interaction["CricStarBot"], user: discord.User, days: int = 7):
    await interaction.response.defer(ephemeral=True)

    uid = str(user.id)
    trades  = await _get_trades(uid, days)
    packs   = await _get_packs(uid, days)
    gives   = await _get_gives(uid, days)
    catches = await _get_catches(uid, days)

    data = {
        "trades":  trades,
        "bets":    [],
        "packs":   packs,
        "gives":   gives,
        "catches": catches,
    }

    view = OwnerCheckView(user, days, data)
    await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True)
