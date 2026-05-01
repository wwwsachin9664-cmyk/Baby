import logging
from typing import TYPE_CHECKING

from discord import app_commands

from .cog import Admin
from .ownercheck import ownercheck as ownercheck_command

if TYPE_CHECKING:
    from cricstar.core.bot import CricStarBot

log = logging.getLogger("cricstar.packages.admin")


def command_count(cog: Admin) -> int:
    total = 0
    for command in cog.walk_app_commands():
        total += len(command.name) + len(command.description)
        if isinstance(command, app_commands.Group):
            continue
        for param in command.parameters:
            total += len(param.name) + len(param.description)
            for choice in param.choices:
                total += len(choice.name) + (
                    int(choice.value) if isinstance(choice.value, int | float) else len(choice.value)
                )
    return total


def strip_descriptions(cog: Admin):
    for command in cog.walk_app_commands():
        command.description = "."
        if isinstance(command, app_commands.Group):
            continue
        for param in command.parameters:
            param._Parameter__parent.description = "."  # type: ignore


async def setup(bot: "CricStarBot"):
    n = Admin(bot)
    if command_count(n) > 3900:
        strip_descriptions(n)
        log.warn("/admin command too long, stripping descriptions.")
    await bot.add_cog(n)
    bot.tree.add_command(ownercheck_command)
