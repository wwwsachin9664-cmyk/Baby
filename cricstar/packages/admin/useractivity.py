"""
Bot-owner-only command group for inspecting any user's full activity log:
trades, card catches, and card gives/receives.
"""

import datetime
from typing import TYPE_CHECKING, cast

import discord
from discord.ext import commands
from discord.utils import format_dt
from django.db.models import Q
from django.urls import reverse
from django.utils.timezone import get_current_timezone

from cricstar.core.bot import CricStarBot
from bd_models.models import BallInstance, Player, Trade, TradeObject
from settings.models import settings

if TYPE_CHECKING:
    import discord.types.interactions
    from cricstar.packages.trade.cog import Trade as TradeCog


@commands.hybrid_group(name="useractivity")
@commands.is_owner()
async def useractivity(ctx: commands.Context[CricStarBot]):
    """
    (Bot owner only) Inspect any user's full activity log.
    """
    await ctx.send_help(ctx.command)


# ---------------------------------------------------------------------------
# Catches sub-command
# ---------------------------------------------------------------------------

@useractivity.command(name="catches")
@commands.is_owner()
async def useractivity_catches(
    ctx: commands.Context[CricStarBot],
    user: discord.User,
    limit: int = 20,
    days: int | None = None,
):
    """
    (Bot owner only) Show cards caught by a user.

    Parameters
    ----------
    user: discord.User
        The user whose catches you want to inspect.
    limit: int
        Maximum number of recent catches to display (default 20, max 50).
    days: int | None
        Restrict results to the last N days.
    """
    await ctx.defer(ephemeral=True)

    player = await Player.objects.aget_or_none(discord_id=user.id)
    if not player:
        await ctx.send(f"No player record found for {user}.", ephemeral=True)
        return

    limit = min(max(limit, 1), 50)

    qs = BallInstance.objects.filter(
        player=player,
        trade_player=None,
    ).order_by("-catch_date")

    if days is not None and days > 0:
        cutoff = datetime.datetime.now(tz=get_current_timezone()) - datetime.timedelta(days=days)
        qs = qs.filter(catch_date__gte=cutoff)

    total = await qs.acount()
    balls = qs[:limit]

    admin_url = f"{settings.site_base_url}{reverse('admin:bd_models_player_change', args=(player.pk,))}"
    embed = discord.Embed(
        title=f"Card catches for {user} ({user.id})",
        url=admin_url,
        color=discord.Color.green(),
        description=f"**Total matching catches:** {total}\nShowing the {limit} most recent.",
    )

    if days is not None:
        embed.set_footer(text=f"Filtered to the last {days} day(s).")

    lines = []
    async for ball in balls:
        caught_at = format_dt(ball.catch_date, "f") if ball.catch_date else "unknown date"
        server = f"server `{ball.server_id}`" if ball.server_id else "unknown server"
        card_id = f"{ball.pk:0X}"
        lines.append(f"• **{ball.description(short=True)}** (ID `{card_id}`) — {caught_at} in {server}")

    if lines:
        embed.add_field(name="Catches", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Catches", value="No catches found.", inline=False)

    await ctx.send(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# Gives sub-command
# ---------------------------------------------------------------------------

@useractivity.command(name="gives")
@commands.is_owner()
async def useractivity_gives(
    ctx: commands.Context[CricStarBot],
    user: discord.User,
    limit: int = 20,
    days: int | None = None,
):
    """
    (Bot owner only) Show cards given to or received from trades by a user.

    Parameters
    ----------
    user: discord.User
        The user whose gives/receives you want to inspect.
    limit: int
        Maximum number of records to display (default 20, max 50).
    days: int | None
        Restrict results to the last N days.
    """
    await ctx.defer(ephemeral=True)

    player = await Player.objects.aget_or_none(discord_id=user.id)
    if not player:
        await ctx.send(f"No player record found for {user}.", ephemeral=True)
        return

    limit = min(max(limit, 1), 50)

    # Cards currently owned by this player that came via a trade (received from someone)
    received_qs = BallInstance.objects.filter(
        player=player,
        trade_player__isnull=False,
    ).select_related("trade_player").order_by("-catch_date")

    # Cards that were originally owned by this player but now belong to someone else (given away)
    given_qs = BallInstance.objects.filter(
        trade_player=player,
    ).select_related("player").order_by("-catch_date")

    if days is not None and days > 0:
        cutoff = datetime.datetime.now(tz=get_current_timezone()) - datetime.timedelta(days=days)
        received_qs = received_qs.filter(catch_date__gte=cutoff)
        given_qs = given_qs.filter(catch_date__gte=cutoff)

    total_received = await received_qs.acount()
    total_given = await given_qs.acount()

    admin_url = f"{settings.site_base_url}{reverse('admin:bd_models_player_change', args=(player.pk,))}"
    embed = discord.Embed(
        title=f"Card gives/receives for {user} ({user.id})",
        url=admin_url,
        color=discord.Color.orange(),
        description=(
            f"**Cards received via trade:** {total_received}\n"
            f"**Cards given away via trade:** {total_given}\n"
            f"Showing up to {limit} of each."
        ),
    )

    if days is not None:
        embed.set_footer(text=f"Filtered to the last {days} day(s).")

    # --- Received ---
    received_lines = []
    async for ball in received_qs[:limit]:
        card_id = f"{ball.pk:0X}"
        from_who = f"from player ID `{ball.trade_player.discord_id}`"
        caught_at = format_dt(ball.catch_date, "f") if ball.catch_date else "unknown date"
        received_lines.append(
            f"• **{ball.description(short=True)}** (ID `{card_id}`) — received {from_who} on {caught_at}"
        )

    embed.add_field(
        name=f"Received ({total_received})",
        value="\n".join(received_lines) if received_lines else "None.",
        inline=False,
    )

    # --- Given ---
    given_lines = []
    async for ball in given_qs[:limit]:
        card_id = f"{ball.pk:0X}"
        to_who = f"to player ID `{ball.player.discord_id}`"
        caught_at = format_dt(ball.catch_date, "f") if ball.catch_date else "unknown date"
        given_lines.append(
            f"• **{ball.description(short=True)}** (ID `{card_id}`) — given {to_who} on {caught_at}"
        )

    embed.add_field(
        name=f"Given away ({total_given})",
        value="\n".join(given_lines) if given_lines else "None.",
        inline=False,
    )

    await ctx.send(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# Trades sub-command
# ---------------------------------------------------------------------------

@useractivity.command(name="trades")
@commands.is_owner()
async def useractivity_trades(
    ctx: commands.Context[CricStarBot],
    user: discord.User,
    days: int | None = None,
    sort_oldest: bool = False,
):
    """
    (Bot owner only) Show the full trade history of a user.

    Parameters
    ----------
    user: discord.User
        The user whose trades you want to inspect.
    days: int | None
        Restrict results to the last N days.
    sort_oldest: bool
        Set to True to show oldest trades first.
    """
    await ctx.defer(ephemeral=True)

    sort_value = "date" if sort_oldest else "-date"
    queryset = (
        Trade.objects.order_by(sort_value)
        .prefetch_related("player1", "player2")
        .filter(Q(player1__discord_id=user.id) | Q(player2__discord_id=user.id))
    )

    if days is not None and days > 0:
        cutoff = datetime.datetime.now(tz=get_current_timezone()) - datetime.timedelta(days=days)
        queryset = queryset.filter(date__gte=cutoff)

    if not await queryset.aexists():
        await ctx.send(f"No trade history found for {user}.", ephemeral=True)
        return

    trade_cog = cast("TradeCog | None", ctx.bot.get_cog("Trade"))
    if not trade_cog:
        await ctx.send("Trade system is currently unavailable.", ephemeral=True)
        return

    from cricstar.packages.admin.history import _build_history_view

    title = f"Trade history of {user.display_name} ({user.id})"
    admin_path = f"/bd_models/trade/?q={user.id}"
    await _build_history_view(ctx, queryset, title, admin_path)


# ---------------------------------------------------------------------------
# Summary sub-command
# ---------------------------------------------------------------------------

@useractivity.command(name="summary")
@commands.is_owner()
async def useractivity_summary(
    ctx: commands.Context[CricStarBot],
    user: discord.User,
    days: int = 30,
):
    """
    (Bot owner only) Show a full activity summary for a user.

    Parameters
    ----------
    user: discord.User
        The user you want to summarise.
    days: int
        Look back this many days (default 30).
    """
    await ctx.defer(ephemeral=True)

    player = await Player.objects.aget_or_none(discord_id=user.id)
    if not player:
        await ctx.send(f"No player record found for {user}.", ephemeral=True)
        return

    cutoff = datetime.datetime.now(tz=get_current_timezone()) - datetime.timedelta(days=days)

    # --- Catches (original, not from trades) ---
    total_catches = await BallInstance.objects.filter(player=player, trade_player=None).acount()
    recent_catches = await BallInstance.objects.filter(
        player=player, trade_player=None, catch_date__gte=cutoff
    ).acount()

    # --- Cards received via trade ---
    total_received = await BallInstance.objects.filter(player=player, trade_player__isnull=False).acount()
    recent_received = await BallInstance.objects.filter(
        player=player, trade_player__isnull=False, catch_date__gte=cutoff
    ).acount()

    # --- Cards given away (currently owned by someone else, originally from this player) ---
    total_given = await BallInstance.objects.filter(trade_player=player).acount()
    recent_given = await BallInstance.objects.filter(
        trade_player=player, catch_date__gte=cutoff
    ).acount()

    # --- Trades ---
    total_trades = await Trade.objects.filter(
        Q(player1__discord_id=user.id) | Q(player2__discord_id=user.id)
    ).acount()
    recent_trades = await Trade.objects.filter(
        Q(player1__discord_id=user.id) | Q(player2__discord_id=user.id),
        date__gte=cutoff,
    ).acount()

    # --- Current collection size ---
    current_collection = await BallInstance.objects.filter(player=player).acount()

    admin_url = f"{settings.site_base_url}{reverse('admin:bd_models_player_change', args=(player.pk,))}"
    embed = discord.Embed(
        title=f"Activity summary for {user} ({user.id})",
        url=admin_url,
        color=discord.Color.blurple(),
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(
        name=f"Card Catches (last {days}d / all-time)",
        value=f"{recent_catches} / {total_catches}",
        inline=True,
    )
    embed.add_field(
        name=f"Cards Received via Trade (last {days}d / all-time)",
        value=f"{recent_received} / {total_received}",
        inline=True,
    )
    embed.add_field(
        name=f"Cards Given Away (last {days}d / all-time)",
        value=f"{recent_given} / {total_given}",
        inline=True,
    )
    embed.add_field(
        name=f"Trades Participated (last {days}d / all-time)",
        value=f"{recent_trades} / {total_trades}",
        inline=True,
    )
    embed.add_field(
        name="Current Collection Size",
        value=str(current_collection),
        inline=True,
    )

    await ctx.send(embed=embed, ephemeral=True)
