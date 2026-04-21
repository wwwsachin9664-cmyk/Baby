from cricstar.packages.catcherpack.cog import CatcherPackCog


async def setup(bot):
    await bot.add_cog(CatcherPackCog(bot))
