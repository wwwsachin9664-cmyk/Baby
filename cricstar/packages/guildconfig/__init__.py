from typing import TYPE_CHECKING

from .cog import Config

if TYPE_CHECKING:
    from cricstar.core.bot import CricStarBot


async def setup(bot: "CricStarBot"):
    await bot.add_cog(Config(bot))
