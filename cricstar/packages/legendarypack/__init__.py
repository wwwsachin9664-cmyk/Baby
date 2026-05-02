from cricstar.packages.legendarypack.cog import LegendaryPackCog


async def setup(bot):
    await bot.add_cog(LegendaryPackCog(bot))
