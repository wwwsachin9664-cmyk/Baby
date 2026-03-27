from .cog import DailyCog

async def setup(bot):
    await bot.add_cog(DailyCog(bot))
