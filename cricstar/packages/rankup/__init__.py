from .cog import RankUp
__all__ = ["RankUp"]

async def setup(bot):
    await bot.add_cog(RankUp(bot))