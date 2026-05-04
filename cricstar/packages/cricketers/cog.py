import asyncio
import importlib
import logging
from typing import TYPE_CHECKING, cast

import discord
from asgiref.sync import sync_to_async
from django.db import close_old_connections
from discord.ext import commands

from bd_models.models import GuildConfig
from settings.models import settings

from .cricketer import BallSpawnView
from .spawn import BaseSpawnManager

if TYPE_CHECKING:
    from cricstar.core.bot import CricStarBot

log = logging.getLogger("cricstar.packages.cricketers")


class CountryBallsSpawner(commands.Cog):
    spawn_manager: BaseSpawnManager

    def __init__(self, bot: "CricStarBot"):
        self.bot = bot
        self.cache: dict[int, int] = {}
        self._spawn_locks: dict[int, asyncio.Lock] = {}
        self.cricketer_cls = BallSpawnView

        module_path, class_name = settings.spawn_manager.rsplit(".", 1)
        module = importlib.import_module(module_path)
        # force a reload, otherwise cog reloads won't reflect to this class
        importlib.reload(module)
        spawn_manager = getattr(module, class_name)
        self.spawn_manager = spawn_manager(bot)

    async def load_cache(self):
        i = 0
        async for config in GuildConfig.objects.filter(enabled=True, spawn_channel__isnull=False).only(
            "guild_id", "spawn_channel"
        ):
            self.cache[config.guild_id] = cast(int, config.spawn_channel)
            i += 1
        grammar = "" if i == 1 else "s"
        log.info(f"Loaded {i} guild{grammar} in cache.")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        await sync_to_async(close_old_connections)()
        if not self.bot.is_ready():
            return
        if message.author.bot or message.webhook_id is not None:
            return
        guild = message.guild
        if not guild:
            return
        if guild.id not in self.cache:
            return
        if guild.id in self.bot.blacklist_guild:
            return

        lock = self._spawn_locks.setdefault(guild.id, asyncio.Lock())
        if lock.locked():
            return

        async with lock:
            result = await self.spawn_manager.handle_message(message)
            if result is False:
                return

            if isinstance(result, tuple):
                result, algo = result
            else:
                algo = settings.spawn_manager

            channel = guild.get_channel(self.cache[guild.id])
            if not channel:
                log.warning(f"Lost channel {self.cache[guild.id]} for guild {guild.name}.")
                del self.cache[guild.id]
                return
            ball = await BallSpawnView.get_random(self.bot)
            ball.algo = algo
            await ball.spawn(cast(discord.TextChannel, channel))

    @commands.Cog.listener()
    async def on_cricstar_settings_change(
        self, guild: discord.Guild, channel: discord.TextChannel | None = None, enabled: bool | None = None
    ):
        if guild.id not in self.cache:
            if enabled is False:
                return  # guild not active, nothing to do
            if channel:
                self.cache[guild.id] = channel.id
            else:
                try:
                    config = await GuildConfig.objects.aget(guild_id=guild.id)
                except GuildConfig.DoesNotExist:
                    return
                else:
                    if config.spawn_channel and config.enabled:
                        self.cache[guild.id] = cast(int, config.spawn_channel)
        else:
            if enabled is False:
                del self.cache[guild.id]
                self._spawn_locks.pop(guild.id, None)
            elif channel:
                self.cache[guild.id] = channel.id
