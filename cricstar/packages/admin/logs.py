import discord
from discord.ext import commands

from cricstar.core.bot import CricStarBot
from cricstar.core.utils import checks


@commands.hybrid_group()
@checks.has_permissions("admincmd.logs")
async def logs(ctx: commands.Context[CricStarBot]):
    """
    Bot logs management
    """
    await ctx.send_help(ctx.command)


@logs.command(name="catchlogs")
@checks.has_permissions("admincmd.logs")
async def logs_add(ctx: commands.Context[CricStarBot], user: discord.User):
    """
    Add or remove a user from catch logs.

    Parameters
    ----------
    user: discord.User
        The user you want to add or remove to the logs.
    """
    if user.id in ctx.bot.catch_log:
        ctx.bot.catch_log.remove(user.id)
        await ctx.send(f"{user} removed from catch logs.", ephemeral=True)
    else:
        ctx.bot.catch_log.add(user.id)
        await ctx.send(f"{user} added to catch logs.", ephemeral=True)


@logs.command(name="commandlogs")
@checks.has_permissions("admincmd.logs")
async def commandlogs_add(ctx: commands.Context[CricStarBot], user: discord.User):
    """
    Add or remove a user from command logs.

    Parameters
    ----------
    user: discord.User
        The user you want to add or remove to the logs.
    """
    if user.id in ctx.bot.command_log:
        ctx.bot.command_log.remove(user.id)
        await ctx.send(f"{user} removed from command logs.", ephemeral=True)
    else:
        ctx.bot.command_log.add(user.id)
        await ctx.send(f"{user} added to command logs.", ephemeral=True)
