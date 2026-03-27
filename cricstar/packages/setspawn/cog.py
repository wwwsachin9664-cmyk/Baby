import logging
from typing import TYPE_CHECKING, Optional, cast

import discord
from discord import app_commands
from discord.ext import commands

from bd_models.models import GuildConfig
from settings.models import settings

if TYPE_CHECKING:
    from cricstar.core.bot import CricStarBot
    from cricstar.packages.cricketers.cog import CountryBallsSpawner

log = logging.getLogger("cricstar.packages.setspawn")


async def owner_only(interaction: discord.Interaction["CricStarBot"]) -> bool:
    is_owner = await interaction.client.is_owner(interaction.user)
    if not is_owner:
        await interaction.response.send_message(
            "Only the bot owner can use this command.", ephemeral=True
        )
    return is_owner


async def admin_or_owner(interaction: discord.Interaction["CricStarBot"]) -> bool:
    if await interaction.client.is_owner(interaction.user):
        return True
    if isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.manage_guild:
        return True
    await interaction.response.send_message(
        "You need the **Manage Server** permission to use this command.", ephemeral=True
    )
    return False


@app_commands.guild_only()
class SetSpawn(commands.GroupCog, group_name="setspawn"):
    """
    Manage cricketer spawn settings for your server.
    """

    def __init__(self, bot: "CricStarBot"):
        self.bot = bot

    # ------------------------------------------------------------------
    # /setspawn channel [channel]   — server admin OR owner
    # ------------------------------------------------------------------
    @app_commands.command(name="channel")
    @app_commands.check(admin_or_owner)
    @app_commands.checks.bot_has_permissions(read_messages=True, send_messages=True, embed_links=True)
    async def set_channel(
        self,
        interaction: discord.Interaction["CricStarBot"],
        channel: Optional[discord.TextChannel] = None,
    ):
        """
        Set the channel where cricketers will spawn in this server.

        Parameters
        ----------
        channel: discord.TextChannel
            The channel to use. Defaults to the current channel.
        """
        guild = cast(discord.Guild, interaction.guild)

        target_channel = channel or (
            interaction.channel
            if isinstance(interaction.channel, discord.TextChannel)
            else None
        )
        if not target_channel:
            await interaction.response.send_message(
                "Could not determine a valid text channel. Please specify one.", ephemeral=True
            )
            return

        config, _ = await GuildConfig.objects.aget_or_create(guild_id=guild.id)
        config.spawn_channel = target_channel.id  # type: ignore
        config.enabled = True
        await config.asave()
        self.bot.dispatch("cricstar_settings_change", guild, channel=target_channel, enabled=True)

        await interaction.response.send_message(
            f"Spawn channel set to {target_channel.mention}. Cricketers will start spawning here!",
            ephemeral=True,
        )
        log.info(
            f"Spawn channel for guild {guild.id} ({guild.name}) set to "
            f"#{target_channel.name} by {interaction.user}"
        )

    # ------------------------------------------------------------------
    # /setspawn remove   — server admin OR owner
    # ------------------------------------------------------------------
    @app_commands.command(name="remove")
    @app_commands.check(admin_or_owner)
    async def remove_channel(
        self,
        interaction: discord.Interaction["CricStarBot"],
    ):
        """
        Remove the spawn channel and disable spawning in this server.
        """
        guild = cast(discord.Guild, interaction.guild)

        config = await GuildConfig.objects.aget_or_none(guild_id=guild.id)
        if not config or not config.spawn_channel:
            await interaction.response.send_message(
                "This server has no spawn channel configured.", ephemeral=True
            )
            return

        config.spawn_channel = None  # type: ignore
        config.enabled = False
        await config.asave()

        await interaction.response.send_message(
            "Spawn channel removed. Spawning is now disabled in this server.",
            ephemeral=True,
        )
        log.info(f"Spawn channel removed for guild {guild.id} ({guild.name}) by {interaction.user}")

    # ------------------------------------------------------------------
    # /setspawn enable [guild_id]   — owner only
    # ------------------------------------------------------------------
    @app_commands.command(name="enable")
    @app_commands.check(owner_only)
    async def enable_spawn(
        self,
        interaction: discord.Interaction["CricStarBot"],
        guild_id: Optional[str] = None,
    ):
        """
        Enable cricketer spawning for this server or any server by ID.

        Parameters
        ----------
        guild_id: str
            Guild ID to enable. Defaults to the current server.
        """
        target_guild_id = interaction.guild_id
        guild_name = interaction.guild.name if interaction.guild else str(target_guild_id)

        if guild_id:
            try:
                target_guild_id = int(guild_id)
            except ValueError:
                await interaction.response.send_message(
                    "Invalid guild ID — must be a number.", ephemeral=True
                )
                return
            guild = self.bot.get_guild(target_guild_id)
            guild_name = guild.name if guild else str(target_guild_id)

        config = await GuildConfig.objects.aget_or_none(guild_id=target_guild_id)
        if not config or not config.spawn_channel:
            await interaction.response.send_message(
                f"**{guild_name}** has no spawn channel set. Use `/setspawn channel` first.",
                ephemeral=True,
            )
            return

        config.enabled = True
        await config.asave()
        await interaction.response.send_message(
            f"Spawning **enabled** for **{guild_name}**.", ephemeral=True
        )
        log.info(f"Spawn enabled for guild {target_guild_id} by {interaction.user}")

    # ------------------------------------------------------------------
    # /setspawn disable [guild_id]   — owner only
    # ------------------------------------------------------------------
    @app_commands.command(name="disable")
    @app_commands.check(owner_only)
    async def disable_spawn(
        self,
        interaction: discord.Interaction["CricStarBot"],
        guild_id: Optional[str] = None,
    ):
        """
        Disable cricketer spawning for this server or any server by ID.

        Parameters
        ----------
        guild_id: str
            Guild ID to disable. Defaults to the current server.
        """
        target_guild_id = interaction.guild_id
        guild_name = interaction.guild.name if interaction.guild else str(target_guild_id)

        if guild_id:
            try:
                target_guild_id = int(guild_id)
            except ValueError:
                await interaction.response.send_message(
                    "Invalid guild ID — must be a number.", ephemeral=True
                )
                return
            guild = self.bot.get_guild(target_guild_id)
            guild_name = guild.name if guild else str(target_guild_id)

        config, _ = await GuildConfig.objects.aget_or_create(guild_id=target_guild_id)
        config.enabled = False
        await config.asave()
        await interaction.response.send_message(
            f"Spawning **disabled** for **{guild_name}**.", ephemeral=True
        )
        log.info(f"Spawn disabled for guild {target_guild_id} by {interaction.user}")

    # ------------------------------------------------------------------
    # /setspawn cooldown <min> <max>   — owner only
    # ------------------------------------------------------------------
    @app_commands.command(name="cooldown")
    @app_commands.check(owner_only)
    async def set_cooldown(
        self,
        interaction: discord.Interaction["CricStarBot"],
        min_messages: int,
        max_messages: int,
    ):
        """
        Set the global spawn cooldown range (messages needed between spawns).

        Parameters
        ----------
        min_messages: int
            Minimum number of messages before a cricketer can spawn.
        max_messages: int
            Maximum messages (threshold is random between min and max).
        """
        if min_messages < 1:
            await interaction.response.send_message(
                "Minimum must be at least 1.", ephemeral=True
            )
            return
        if max_messages <= min_messages:
            await interaction.response.send_message(
                "Maximum must be greater than minimum.", ephemeral=True
            )
            return

        old_min = settings.spawn_chance_min
        old_max = settings.spawn_chance_max

        settings.spawn_chance_min = min_messages  # type: ignore
        settings.spawn_chance_max = max_messages  # type: ignore
        await settings.asave(update_fields=["spawn_chance_min", "spawn_chance_max"])

        await interaction.response.send_message(
            f"Spawn cooldown updated.\n"
            f"**Before:** {old_min}–{old_max} messages\n"
            f"**After:** {min_messages}–{max_messages} messages\n\n"
            f"New thresholds apply to the next spawn cycle in each server.",
            ephemeral=True,
        )
        log.info(
            f"Spawn cooldown changed from {old_min}-{old_max} to "
            f"{min_messages}-{max_messages} by {interaction.user}"
        )

    # ------------------------------------------------------------------
    # /setspawn reset [guild_id]   — owner only
    # ------------------------------------------------------------------
    @app_commands.command(name="reset")
    @app_commands.check(owner_only)
    async def reset_cooldown(
        self,
        interaction: discord.Interaction["CricStarBot"],
        guild_id: Optional[str] = None,
    ):
        """
        Reset the live spawn counter for a server, starting a fresh cycle.

        Parameters
        ----------
        guild_id: str
            Guild ID to reset. Defaults to the current server.
        """
        target_guild_id = interaction.guild_id
        guild_name = interaction.guild.name if interaction.guild else str(target_guild_id)

        if guild_id:
            try:
                target_guild_id = int(guild_id)
            except ValueError:
                await interaction.response.send_message(
                    "Invalid guild ID — must be a number.", ephemeral=True
                )
                return
            guild = self.bot.get_guild(target_guild_id)
            guild_name = guild.name if guild else str(target_guild_id)

        spawner = cast("CountryBallsSpawner | None", self.bot.get_cog("CountryBallsSpawner"))
        if not spawner:
            await interaction.response.send_message(
                "Spawn system is not loaded.", ephemeral=True
            )
            return

        cooldowns = spawner.spawn_manager.cooldowns  # type: ignore
        if target_guild_id in cooldowns:
            del cooldowns[target_guild_id]
            await interaction.response.send_message(
                f"Spawn cooldown for **{guild_name}** has been reset. "
                f"A fresh cycle will begin on the next message.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"**{guild_name}** has no active cooldown to reset.", ephemeral=True
            )
        log.info(f"Spawn cooldown reset for guild {target_guild_id} by {interaction.user}")

    # ------------------------------------------------------------------
    # /setspawn status [guild_id]   — owner only
    # ------------------------------------------------------------------
    @app_commands.command(name="status")
    @app_commands.check(owner_only)
    async def spawn_status(
        self,
        interaction: discord.Interaction["CricStarBot"],
        guild_id: Optional[str] = None,
    ):
        """
        View the spawn configuration for this server or any server by ID.

        Parameters
        ----------
        guild_id: str
            Guild ID to inspect. Defaults to the current server.
        """
        target_guild_id = interaction.guild_id
        guild_name = interaction.guild.name if interaction.guild else str(target_guild_id)

        if guild_id:
            try:
                target_guild_id = int(guild_id)
            except ValueError:
                await interaction.response.send_message(
                    "Invalid guild ID — must be a number.", ephemeral=True
                )
                return
            guild = self.bot.get_guild(target_guild_id)
            guild_name = guild.name if guild else str(target_guild_id)

        config = await GuildConfig.objects.aget_or_none(guild_id=target_guild_id)

        embed = discord.Embed(
            title=f"Spawn Status — {guild_name}",
            color=0x00D936 if (config and config.enabled and config.spawn_channel) else 0xFF4444,
        )

        if not config or not config.spawn_channel:
            embed.description = "No spawn channel configured."
        else:
            guild_obj = self.bot.get_guild(target_guild_id)
            channel = guild_obj.get_channel(config.spawn_channel) if guild_obj else None
            channel_mention = channel.mention if channel else f"`#{config.spawn_channel}` (not found)"
            embed.add_field(name="Spawn Channel", value=channel_mention, inline=True)
            embed.add_field(
                name="Status",
                value="✅ Enabled" if config.enabled else "❌ Disabled",
                inline=True,
            )

        embed.add_field(
            name="Global Cooldown Range",
            value=f"{settings.spawn_chance_min}–{settings.spawn_chance_max} messages",
            inline=False,
        )

        spawner = cast("CountryBallsSpawner | None", self.bot.get_cog("CountryBallsSpawner"))
        if spawner:
            cooldowns = spawner.spawn_manager.cooldowns  # type: ignore
            cooldown = cooldowns.get(target_guild_id)
            if cooldown:
                progress = f"{cooldown.scaled_message_count:.1f} / {cooldown.threshold}"
                embed.add_field(name="Live Progress", value=progress, inline=True)
            else:
                embed.add_field(name="Live Progress", value="No active cycle", inline=True)

        await interaction.response.send_message(embed=embed, ephemeral=True)
