from .cog import Upgrade


async def setup(bot):
    await bot.add_cog(Upgrade(bot))
