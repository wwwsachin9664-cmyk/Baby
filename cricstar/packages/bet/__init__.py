from .cog import Bet

__all__ = ["Bet"]


async def setup(bot):
    await bot.add_cog(Bet(bot))
