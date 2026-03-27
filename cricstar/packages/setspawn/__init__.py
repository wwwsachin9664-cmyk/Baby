from .cog import SetSpawn

async def setup(bot):
    await bot.add_cog(SetSpawn(bot))
