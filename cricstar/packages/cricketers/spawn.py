import asyncio
import logging
import random
from abc import abstractmethod
from collections import deque, namedtuple
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Literal

import discord
from asgiref.sync import sync_to_async
from discord.utils import format_dt

from settings.models import settings

if TYPE_CHECKING:
    from discord.ext.commands import Context

    from cricstar.core.bot import CricStarBot

log = logging.getLogger("cricstar.packages.cricketers")

CachedMessage = namedtuple("CachedMessage", ["content", "author_id"])


class BaseSpawnManager:
    """
    A class instancied on cog load that will include the logic determining when a cricketer
    should be spawned. You can implement your own version and configure it in config.yml.

    Be careful with optimization and memory footprint, this will be called very often and should
    not slow down the bot or cause memory leaks.
    """

    def __init__(self, bot: "CricStarBot"):
        self.bot = bot

    @abstractmethod
    async def handle_message(self, message: discord.Message) -> bool | tuple[Literal[True], str]:
        """
        Handle a message event and determine if a cricketer should be spawned next.

        Parameters
        ----------
        message: discord.Message
            The message that triggered the event

        Returns
        -------
        bool | tuple[Literal[True], str]
            `True` if a cricketer should be spawned, else `False`.

            If a cricketer should spawn, do not forget to cleanup induced context to avoid
            infinite spawns.

            You can also return a tuple (True, msg) to indicate which spawn algorithm has been
            used, which is then reported to prometheus. This is useful for comparing the results
            of your algorithms using A/B testing.
        """
        raise NotImplementedError

    @abstractmethod
    async def admin_explain(self, ctx: "Context[CricStarBot]", guild: discord.Guild):
        """
        Invoked by "/admin cooldown", this function should provide insights of the cooldown
        system for admins.

        Parameters
        ----------
        ctx: ~discord.ext.commands.Context[CricStarBot]
            The context of the invoking hybrid command
        guild: discord.Guild
            The guild that is targeted for the insights
        """
        raise NotImplementedError


@dataclass
class SpawnCooldown:
    """
    Represents the default spawn internal system per guild. Contains the counters that will
    determine if a cricketer should be spawned next or not.

    Attributes
    ----------
    time: datetime
        Time when the object was initialized. Block spawning when it's been less than ten minutes
    scaled_message_count: float
        A number starting at 0, incrementing with the messages until reaching `threshold`. At this
        point, a ball will be spawned next.
    threshold: int
        The number `scaled_message_count` has to reach for spawn.
        Determined randomly with `SPAWN_CHANCE_RANGE`
    lock: asyncio.Lock
        Used to ratelimit messages and ignore fast spam
    message_cache: ~collections.deque[CachedMessage]
        A list of recent messages used to reduce the spawn chance when too few different chatters
        are present. Limited to the 100 most recent messages in the guild.
    """

    time: datetime
    # initialize partially started, to reduce the dead time after starting the bot
    scaled_message_count: float = field(default_factory=lambda: settings.spawn_chance_min // 2)
    threshold: int = field(default_factory=lambda: random.randint(settings.spawn_chance_min, settings.spawn_chance_max))
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    message_cache: deque[CachedMessage] = field(default_factory=lambda: deque(maxlen=100))

    def reset(self, time: datetime):
        self.scaled_message_count = 1.0
        self.threshold = random.randint(settings.spawn_chance_min, settings.spawn_chance_max)
        try:
            self.lock.release()
        except RuntimeError:  # lock is not acquired
            pass
        self.time = time

    async def increase(self, message: discord.Message) -> bool:
        # this is a deque, not a list
        # its property is that, once the max length is reached (100 for us),
        # the oldest element is removed, thus we only have the last 100 messages in memory
        self.message_cache.append(CachedMessage(content=message.content, author_id=message.author.id))

        if self.lock.locked():
            return False

        async with self.lock:
            message_multiplier = 1
            if message.guild.member_count and message.guild.member_count > 1000:  # type: ignore
                message_multiplier /= 2
            if message._state.intents.message_content and len(message.content) < 5:
                message_multiplier /= 2
            if len(set(x.author_id for x in self.message_cache)) < 4 or (
                len(list(filter(lambda x: x.author_id == message.author.id, self.message_cache)))
                / self.message_cache.maxlen  # type: ignore
                > 0.4
            ):
                message_multiplier /= 2
            self.scaled_message_count += message_multiplier
            await asyncio.sleep(10)
        return True


@sync_to_async
def _load_guild_spawn_state(guild_id: int) -> tuple[datetime | None, int | None]:
    """Load persisted spawn state from the database for a guild."""
    from bd_models.models import GuildConfig
    try:
        config = GuildConfig.objects.only("last_spawn_at", "spawn_threshold").get(guild_id=guild_id)
        return config.last_spawn_at, config.spawn_threshold
    except GuildConfig.DoesNotExist:
        return None, None


@sync_to_async
def _claim_spawn(guild_id: int, new_time: datetime, new_threshold: int) -> bool:
    """
    Atomically claim the right to spawn for a guild.

    Uses a conditional UPDATE so that even when two bot processes run concurrently
    (e.g. after a Replit restart where the old process lingers), only one process
    can win the race per spawn event.

    The claim succeeds only when the stored last_spawn_at is either NULL or more
    than 5 seconds in the past, preventing a duplicate spawn within that window.

    Returns True if this process should spawn, False if another already did.
    """
    from datetime import timedelta

    from django.db.models import Q

    from bd_models.models import GuildConfig

    cutoff = new_time - timedelta(seconds=5)
    rows_updated = GuildConfig.objects.filter(guild_id=guild_id).filter(
        Q(last_spawn_at__isnull=True) | Q(last_spawn_at__lt=cutoff)
    ).update(last_spawn_at=new_time, spawn_threshold=new_threshold)
    return rows_updated > 0


class SpawnManager(BaseSpawnManager):
    def __init__(self, bot: "CricStarBot"):
        super().__init__(bot)
        self.cooldowns: dict[int, SpawnCooldown] = {}

    async def _get_or_create_cooldown(self, guild_id: int, message_time: datetime) -> SpawnCooldown:
        """
        Return the cooldown for a guild, loading persisted state from DB on first access
        so spawn progress survives bot restarts and remixes.
        """
        if guild_id in self.cooldowns:
            return self.cooldowns[guild_id]

        last_spawn_at, saved_threshold = await _load_guild_spawn_state(guild_id)

        if last_spawn_at is not None:
            # Restore from DB — use the persisted last-spawn time so the 10-minute
            # minimum and time-based multiplier are calculated correctly.
            cooldown = SpawnCooldown(time=last_spawn_at)
            if saved_threshold is not None:
                cooldown.threshold = saved_threshold
            # Start progress at half-way so the bot doesn't immediately re-spawn
            # after a restart while still honouring time elapsed.
            cooldown.scaled_message_count = settings.spawn_chance_min // 2
        else:
            cooldown = SpawnCooldown(message_time)

        self.cooldowns[guild_id] = cooldown
        return cooldown

    async def handle_message(self, message: discord.Message) -> bool:
        guild = message.guild
        if not guild:
            return False

        cooldown = await self._get_or_create_cooldown(guild.id, message.created_at)

        delta_t = (message.created_at - cooldown.time).total_seconds()
        # change how the threshold varies according to the member count, while nuking farm servers
        if not guild.member_count:
            return False
        elif guild.member_count < 50:
            time_multiplier = 0.5
        elif guild.member_count < 100:
            time_multiplier = 0.8
        elif guild.member_count < 1000:
            time_multiplier = 0.6
        else:
            time_multiplier = 0.2

        # manager cannot be increased more than once per 10 seconds
        if not await cooldown.increase(message):
            return False

        # normal increase, need to reach goal
        if cooldown.scaled_message_count + time_multiplier * (delta_t // 60) <= cooldown.threshold:
            return False

        # at this point, the goal is reached
        if delta_t < 600:
            # wait for at least 10 minutes before spawning
            return False

        # Atomically claim the spawn in the database before doing anything in memory.
        # This prevents duplicate spawns when two bot processes run concurrently
        # (e.g. after a Replit restart where the old process didn't exit cleanly).
        # The DB UPDATE only succeeds for one process; the other gets rows_updated=0.
        new_time = message.created_at
        new_threshold = random.randint(settings.spawn_chance_min, settings.spawn_chance_max)
        try:
            claimed = await _claim_spawn(guild.id, new_time, new_threshold)
        except Exception:
            log.exception(f"Failed to claim spawn for guild {guild.id}, allowing spawn anyway")
            claimed = True  # fall back to spawning if DB is unreachable

        if not claimed:
            log.warning(f"Spawn race prevented for guild {guild.id} — another process already spawned")
            cooldown.reset(new_time)  # still reset locally so we don't instantly re-trigger
            return False

        # Won the DB race — apply the same values locally so in-memory state stays in sync
        cooldown.scaled_message_count = 1.0
        cooldown.threshold = new_threshold
        try:
            cooldown.lock.release()
        except RuntimeError:
            pass  # already released by the asyncio.sleep block
        cooldown.time = new_time
        return True

    async def admin_explain(self, ctx: "Context[CricStarBot]", guild: discord.Guild):
        cooldown = self.cooldowns.get(guild.id)
        if not cooldown:
            await ctx.send(
                "No spawn manager could be found for that guild. Spawn may have been disabled.", ephemeral=True
            )
            return

        if not guild.member_count:
            await ctx.send("`member_count` data not returned for this guild, spawn cannot work.")
            return

        embed = discord.Embed()
        embed.set_author(name=guild.name, icon_url=guild.icon.url if guild.icon else None)
        embed.colour = discord.Colour.orange()

        delta = (
            (ctx.interaction.created_at if ctx.interaction else ctx.message.created_at) - cooldown.time
        ).total_seconds()
        # change how the threshold varies according to the member count, while nuking farm servers
        if guild.member_count < 50:
            multiplier = 0.5
            range = "1-49"
        elif guild.member_count < 100:
            multiplier = 0.8
            range = "50-99"
        elif guild.member_count < 1000:
            multiplier = 0.6
            range = "100-999"
        else:
            multiplier = 0.2
            range = "1000+"

        penalities: list[str] = []
        if guild.member_count > 1000:
            penalities.append("Server has more than 1000 members (farm server protection)")
        if any(len(x.content) < 5 for x in cooldown.message_cache):
            penalities.append("Some cached messages are less than 5 characters long")

        authors_set = set(x.author_id for x in cooldown.message_cache)
        low_chatters = len(authors_set) < 4
        # check if one author has more than 40% of messages in cache
        major_chatter = any(
            (
                len(list(filter(lambda x: x.author_id == author, cooldown.message_cache)))
                / cooldown.message_cache.maxlen  # type: ignore
                > 0.4
            )
            for author in authors_set
        )
        # this mess is needed since either conditions make up to a single penality
        if low_chatters:
            if not major_chatter:
                penalities.append("Message cache has less than 4 chatters")
            else:
                penalities.append(
                    "Message cache has less than 4 chatters **and** "
                    "one user has more than 40% of messages within message cache"
                )
        elif major_chatter:
            if not low_chatters:
                penalities.append("One user has more than 40% of messages within cache")

        penality_multiplier = 0.5 ** len(penalities)
        if penalities:
            embed.add_field(
                name="\N{WARNING SIGN}\N{VARIATION SELECTOR-16} Penalities",
                value="Each penality divides the progress by 2\n\n- " + "\n- ".join(penalities),
            )

        chance = cooldown.threshold - multiplier * (delta // 60)

        embed.description = (
            f"Manager initiated **{format_dt(cooldown.time, style='R')}**\n"
            f"Initial number of points to reach: **{cooldown.threshold}**\n"
            f"Message cache length: **{len(cooldown.message_cache)}**\n\n"
            f"Time-based multiplier: **x{multiplier}** *({range} members)*\n"
            "*This affects how much the number of points to reach reduces over time*\n"
            f"Penality multiplier: **x{penality_multiplier}**\n"
            "*This affects how much a message sent increases the number of points*\n\n"
            f"__Current count: **{cooldown.threshold}/{chance}**__\n\n"
        )

        informations: list[str] = []
        if cooldown.lock.locked():
            informations.append("The manager is currently on cooldown.")
        if delta < 600:
            informations.append(
                f"The manager is less than 10 minutes old, {settings.plural_collectible_name} "
                "cannot spawn at the moment."
            )
        if informations:
            embed.add_field(
                name="\N{INFORMATION SOURCE}\N{VARIATION SELECTOR-16} Informations",
                value="- " + "\n- ".join(informations),
            )

        await ctx.send(embed=embed, ephemeral=True)
