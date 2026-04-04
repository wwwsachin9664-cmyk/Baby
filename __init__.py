from .owner_cog import OwnerCog


async def setup(bot):
    await bot.add_cog(OwnerCog(bot))