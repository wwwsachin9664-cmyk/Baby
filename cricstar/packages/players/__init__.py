from typing import TYPE_CHECKING

from .cog import Player

if TYPE_CHECKING:
    from cricstar.core.bot import CricStarBot


async def setup(bot: "CricStarBot"):
    await bot.add_cog(Player(bot))
