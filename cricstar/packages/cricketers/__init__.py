from typing import TYPE_CHECKING

from .cog import CountryBallsSpawner

if TYPE_CHECKING:
    from cricstar.core.bot import CricStarBot


async def setup(bot: "CricStarBot"):
    cog = CountryBallsSpawner(bot)
    await bot.add_cog(cog)
    await cog.load_cache()
