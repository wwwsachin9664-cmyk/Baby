import logging
import os
import re
import ssl
import tempfile
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, cast

import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import ActionRow, Button, Container, Section, TextDisplay

from cricstar.card_sync import (
    export_card as _export_card,
    export_event as _export_event,
    delete_card_export as _delete_card_export,
    has_custom_spawn_image as _has_custom_spawn_image,
    export_spawn_image as _export_spawn_image,
    remove_spawn_image as _remove_spawn_image,
    list_spawn_images as _list_spawn_images,
)
from cricstar.core.bot import impersonations
from cricstar.core.discord import LayoutView
from cricstar.core.image_generator.image_gen import draw_premade_card, get_neon_color, save_neon_color
from cricstar.core.utils.emojis import add_emoji, format_emoji, get_player_emoji, list_emojis, parse_emoji_input, remove_emoji
from cricstar.core.utils import checks
from cricstar.core.utils.buttons import ConfirmChoiceView
from cricstar.core.utils.menus import (
    ItemFormatter,
    ListSource,
    Menu,
    TextFormatter,
    TextSource,
    dynamic_chunks,
    iter_to_async,
)
from django.db.models import Max

from bd_models.models import Ball, BallInstance, GuildConfig, Logo, Regime, Special, TradeObject
from bd_models.models import balls as balls_cache
from bd_models.models import specials as specials_cache
from settings.models import settings

from .balls import balls as balls_group
from .blacklist import blacklist as blacklist_group
from .blacklist import blacklistguild as blacklist_guild_group
from .flags import RarityFlags, StatusFlags
from .history import history as history_group
from .info import info as info_group
from .logs import logs as logs_group
from .money import money as money_group

if TYPE_CHECKING:
    from cricstar.core.bot import CricStarBot
    from cricstar.packages.cricketers.cog import CountryBallsSpawner
    from cricstar.packages.trade.cog import Trade

log = logging.getLogger("cricstar.packages.admin")


class SyncView(LayoutView):
    def __init__(self, cog: "Admin", *, timeout: float | None = 180) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog

    text = TextDisplay("Admin commands are already synced here. What would you like to do?")
    action_row = ActionRow()

    @action_row.button(
        label="Synchronize",
        style=discord.ButtonStyle.primary,
        emoji="\N{CLOCKWISE RIGHTWARDS AND LEFTWARDS OPEN CIRCLE ARROWS}",
    )
    async def sync(self, interaction: discord.Interaction["CricStarBot"], button: Button):
        assert interaction.guild
        self.stop()
        await interaction.response.defer()
        if not interaction.client.tree.get_command("admin", guild=interaction.guild):
            interaction.client.tree.add_command(self.cog.admin.app_command, guild=interaction.guild)
        await interaction.client.tree.sync(guild=interaction.guild)
        await GuildConfig.objects.aupdate_or_create(
            guild_id=interaction.guild.id, defaults={"guild_id": interaction.guild.id, "admin_command_synced": True}
        )
        self.sync.disabled = True
        self.remove.disabled = True
        self.text.content += (
            "\n\nCommands have been refreshed. You may need to reload your Discord client to see the changes applied."
        )
        await interaction.edit_original_response(view=self)

    @action_row.button(
        label="Remove", style=discord.ButtonStyle.danger, emoji="\N{HEAVY MULTIPLICATION X}\N{VARIATION SELECTOR-16}"
    )
    async def remove(self, interaction: discord.Interaction["CricStarBot"], button: Button):
        assert interaction.guild
        self.stop()
        await interaction.response.defer()
        interaction.client.tree.remove_command("admin", guild=interaction.guild)
        await interaction.client.tree.sync(guild=interaction.guild)
        await GuildConfig.objects.filter(guild_id=interaction.guild.id).aupdate(admin_command_synced=True)
        self.sync.disabled = True
        self.remove.disabled = True
        self.text.content += (
            "\n\nCommands have been removed. You may need to reload your Discord client to see the changes applied."
        )
        await interaction.edit_original_response(view=self)
        log.info(f"Admin commands removed from guild {interaction.guild.id} by {interaction.user}")


class MultiSpawnView(discord.ui.View):
    """Select up to 20 cricketers and spawn them all at once in the configured spawn channel."""

    def __init__(self, options: list[discord.SelectOption], channel: discord.TextChannel, bot: "CricStarBot"):
        super().__init__(timeout=60)
        self.channel = channel
        self.bot = bot

        self.select = discord.ui.Select(
            placeholder="Choose cricketers to spawn (up to 20)…",
            min_values=1,
            max_values=min(20, len(options)),
            options=options,
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction["CricStarBot"]):
        await interaction.response.defer(ephemeral=True)
        from cricstar.packages.cricketers.cricketer import BallSpawnView

        spawned, failed = 0, 0
        for val in interaction.data["values"]:  # type: ignore[index]
            ball_pk = int(val)
            ball_model = balls_cache.get(ball_pk)
            if not ball_model:
                try:
                    ball_model = await Ball.objects.aget(pk=ball_pk)
                except Ball.DoesNotExist:
                    failed += 1
                    continue
            view = BallSpawnView(self.bot, ball_model)
            success = await view.spawn(self.channel)
            if success:
                spawned += 1
            else:
                failed += 1

        self.stop()
        msg = f"✅ Spawned **{spawned}** cricketer(s) in {self.channel.mention}."
        if failed:
            msg += f"\n❌ **{failed}** failed to spawn."
        await interaction.followup.send(msg, ephemeral=True)


def _list_background_presets() -> list[str]:
    """Return all saved background preset names (without extension)."""
    bg_dir = Path("admin_panel/media/backgrounds")
    if not bg_dir.exists():
        return []
    presets: list[str] = []
    for f in sorted(bg_dir.iterdir()):
        if f.is_file() and f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ""):
            presets.append(f.stem if f.suffix else f.name)
    return presets


async def _background_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Module-level autocomplete for saved background presets."""
    import asyncio
    presets = await asyncio.get_event_loop().run_in_executor(None, _list_background_presets)
    return [
        app_commands.Choice(name=p, value=p)
        for p in presets
        if current.lower() in p.lower()
    ][:25]


async def _event_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Module-level autocomplete for special events — always available when the class is loaded."""
    from bd_models.models import specials as _specials
    current_lower = current.lower()
    choices = [app_commands.Choice(name="None", value="none")]
    for special in _specials.values():
        if current_lower in special.name.lower():
            choices.append(app_commands.Choice(name=special.name, value=special.name))
    return choices[:25]


async def _ball_name_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Autocomplete for cricketer (Ball) names — used in addemoji and similar commands."""
    from bd_models.models import Ball as _Ball
    choices: list[app_commands.Choice[str]] = []
    async for country in _Ball.objects.filter(country__icontains=current).values_list("country", flat=True)[:25]:
        choices.append(app_commands.Choice(name=country, value=country))
    return choices


class Admin(commands.Cog):
    """
    Bot admin commands.
    """

    def __init__(self, bot: "CricStarBot"):
        self.bot = bot

        self.admin.add_command(info_group)
        self.admin.add_command(balls_group)
        self.admin.add_command(blacklist_group)
        self.admin.add_command(blacklist_guild_group)
        self.admin.add_command(history_group)
        self.admin.add_command(logs_group)
        self.admin.add_command(money_group)

    async def _resolve_logo(self, logo_input: str, dest_path: str) -> bool:
        """
        Resolve a logo from either a name (looks up Logo model / logos dir) or a URL.
        Copies/downloads the logo to dest_path. Returns True on success.
        """
        import shutil as _shutil

        logo_input = logo_input.strip()
        if not logo_input:
            return False

        logos_dir = Path("admin_panel/media/logos")

        if not logo_input.startswith(("http://", "https://")):
            # Name-based lookup: check Logo DB entry first
            try:
                db_logo = await Logo.objects.aget(name__iexact=logo_input)
                # Priority 1: uploaded image file
                if db_logo.image:
                    img_path = Path("admin_panel/media") / str(db_logo.image)
                    if img_path.exists():
                        _shutil.copy2(str(img_path), dest_path)
                        return True
                # Priority 2: pathname field
                if db_logo.pathname:
                    candidate = logos_dir / db_logo.pathname
                    if candidate.exists():
                        _shutil.copy2(str(candidate), dest_path)
                        return True
                # Priority 3: URL field on the Logo entry
                if db_logo.url:
                    logo_input = db_logo.url
            except Logo.DoesNotExist:
                pass

            if not logo_input.startswith(("http://", "https://")):
                # Fallback: scan logos dir for matching filename
                for ext in (".png", ".jpg", ".jpeg", ".webp", ""):
                    candidate = logos_dir / f"{logo_input}{ext}"
                    if candidate.exists():
                        _shutil.copy2(str(candidate), dest_path)
                        return True
                return False

        # URL download
        try:
            _ssl_ctx = ssl.create_default_context()
            _ssl_ctx.check_hostname = False
            _ssl_ctx.verify_mode = ssl.CERT_NONE
            req = urllib.request.Request(
                logo_input,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                },
            )
            with urllib.request.urlopen(req, timeout=20, context=_ssl_ctx) as resp:
                data = resp.read(2 * 1024 * 1024)
            with open(dest_path, "wb") as f:
                f.write(data)
            return True
        except Exception as e:
            log.warning("Logo download failed for %r: %s", logo_input[:80], e)
            return False

    async def cog_check(self, ctx: commands.Context["CricStarBot"]) -> bool:
        return await checks.is_staff().predicate(ctx)

    async def cog_app_command_error(
        self, interaction: discord.Interaction["CricStarBot"], error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.CommandSignatureMismatch):
            assert self.bot.user
            await interaction.response.send_message(
                "Admin commands are desynchronized and needs to be re-synced. "
                f"Run `{self.bot.user.mention} admin syncslash` to fix this.",
                ephemeral=True,
            )
            interaction.extras["handled"] = True

    @tasks.loop(minutes=10)
    async def _export_holdings_task(self):
        from cricstar.card_sync import export_all_holdings
        try:
            await self.bot.loop.run_in_executor(None, export_all_holdings)
        except Exception as e:
            log.warning("Periodic holdings export failed (non-critical): %s", e)

    async def cog_load(self):
        guilds = [
            discord.Object(guild_id)
            async for guild_id in GuildConfig.objects.filter(admin_command_synced=True).values_list(
                "guild_id", flat=True
            )
        ]
        self.bot.tree.add_command(self.admin.app_command, guilds=guilds)
        self._export_holdings_task.start()

    async def cog_unload(self):
        self._export_holdings_task.cancel()
        from cricstar.card_sync import export_all_holdings
        try:
            await self.bot.loop.run_in_executor(None, export_all_holdings)
        except Exception as e:
            log.warning("Holdings export on unload failed (non-critical): %s", e)

    @commands.hybrid_group()
    @app_commands.guilds(0)
    @app_commands.default_permissions()
    @app_commands.guilds(checks.ADMIN_GUILD_ID)
    @app_commands.check(checks.is_developer)
    @checks.is_staff()
    async def admin(self, ctx: commands.Context):
        """
        Bot admin commands.
        """
        await ctx.send_help(ctx.command)

    @admin.command(with_app_command=False)
    @commands.is_owner()
    @commands.guild_only()
    async def syncslash(self, ctx: commands.Context["CricStarBot"]):
        """
        Synchronize all the admin commands in the current server, or remove them if already existing.
        """
        assert ctx.guild
        commands = await self.bot.tree.fetch_commands(guild=ctx.guild)
        if commands:
            view = SyncView(self)
            await ctx.send(view=view)
        else:
            view = ConfirmChoiceView(ctx, accept_message="Registering commands...")
            await ctx.send(
                "Would you like to add admin slash commands in this server? "
                "They can only be used with the appropriate Django permissions",
                view=view,
            )
            await view.wait()
            if not view.value:
                return
            async with ctx.typing():
                self.bot.tree.add_command(self.admin.app_command, guild=ctx.guild)
                await self.bot.tree.sync(guild=ctx.guild)
                log.info(f"Admin commands added to guild {ctx.guild.id} by {ctx.author}")
                await ctx.send(
                    "Admin slash commands added.\nYou need admin permissions in this server to view them "
                    f"(this can be changed [here](discord://-/guilds/{ctx.guild.id}/settings/integrations)). You might "
                    "need to refresh your Discord client to view them."
                )
                await GuildConfig.objects.aupdate_or_create(
                    guild_id=ctx.guild.id, defaults={"guild_id": ctx.guild.id, "admin_command_synced": True}
                )

    @admin.command()
    @checks.is_superuser()
    async def status(self, ctx: commands.Context["CricStarBot"], *, flags: StatusFlags):
        """
        Change the status of the bot. Provide at least status or text.
        """
        if not flags.status and not flags.name and not flags.state:
            await ctx.send("You must provide at least `status`, `name` or `state`.", ephemeral=True)
            return

        activity: discord.Activity | None = None
        if flags.activity_type == discord.ActivityType.custom and flags.name and not flags.state:
            await ctx.send("You must provide `state` for custom activities. `name` is unused.", ephemeral=True)
            return
        if flags.activity_type != discord.ActivityType.custom and not flags.name:
            await ctx.send("You must provide `name` for pre-defined activities.", ephemeral=True)
            return
        if flags.name or flags.state:
            activity = discord.Activity(name=flags.name or flags.state, state=flags.state, type=flags.activity_type)
        await self.bot.change_presence(status=flags.status, activity=activity)
        await ctx.send("Status updated.", ephemeral=True)

    @admin.command()
    @checks.is_superuser()
    async def trade_lockdown(self, ctx: commands.Context["CricStarBot"], *, reason: str):
        """
        Cancel all ongoing trades and lock down further trades from being started.

        Parameters
        ----------
        reason: str
            The reason of the lockdown. This will be displayed to all trading users.
        """
        cog = cast("Trade | None", self.bot.get_cog("Trade"))
        if not cog:
            await ctx.send("The trade cog is not loaded.", ephemeral=True)
            return

        await ctx.defer()
        result = await cog.cancel_all_trades(reason)

        assert self.bot.user
        prefix = settings.prefix if self.bot.intents.message_content else f"{self.bot.user.mention} "

        if not result:
            await ctx.send(
                "All trades were successfully cancelled, and further trades cannot be started "
                f'anymore.\nTo enable trades again, the bot owner must use the "{prefix}reload '
                'trade" command.'
            )
        else:
            await ctx.send(
                "Lockdown mode enabled, trades can no longer be started. "
                f"While cancelling ongoing trades, {len(result)} failed to cancel, check your "
                "logs for info.\nTo enable trades again, the bot owner must use the "
                f'"{prefix}reload trade" command.'
            )

    @admin.command()
    @checks.is_superuser()
    async def cooldown(self, ctx: commands.Context["CricStarBot"], guild_id: str | None = None):
        """
        Show the details of the spawn cooldown system for the given server

        Parameters
        ----------
        guild_id: int | None
            ID of the server you want to inspect. If not given, inspect the current server.
        """
        if guild_id:
            try:
                guild = self.bot.get_guild(int(guild_id))
            except ValueError:
                await ctx.send("Invalid guild ID. Please make sure it's a number.", ephemeral=True)
                return
        else:
            guild = ctx.guild
        if not guild:
            await ctx.send("The given guild could not be found.", ephemeral=True)
            return

        spawn_manager = cast("CountryBallsSpawner", self.bot.get_cog("CountryBallsSpawner")).spawn_manager
        await spawn_manager.admin_explain(ctx, guild)

    @admin.command()
    async def guilds(self, ctx: commands.Context["CricStarBot"], user: discord.User):
        """
        Shows the guilds shared with the specified user. Provide either user or user_id.

        Parameters
        ----------
        user: discord.User
            The user you want to check, if available in the current server.
        """
        if self.bot.intents.members:
            guilds = user.mutual_guilds
        else:
            guilds = [x for x in self.bot.guilds if x.owner_id == user.id]

        if not guilds:
            if self.bot.intents.members:
                await ctx.send(f"The user does not own any server with {settings.bot_name}.", ephemeral=True)
            else:
                await ctx.send(
                    f"The user does not own any server with {settings.bot_name}.\n"
                    ":warning: *The bot cannot be aware of the member's presence in servers, "
                    "it is only aware of server ownerships.*",
                    ephemeral=True,
                )
            return
            entries: list[TextDisplay] = []
        for guild in guilds:
            if config := await GuildConfig.objects.aget_or_none(guild_id=guild.id):
                spawn_enabled = config.enabled and config.guild_id
            else:
                spawn_enabled = False

            text = f"## {guild.name} - `{guild.id}`\n"

            # highlight suspicious server names
            if any(x in guild.name.lower() for x in ("farm", "grind", "spam")):
                text += f"- :warning: **{guild.name}**\n"
            else:
                text += f"- {guild.name}\n"

            # highlight low member count
            if guild.member_count <= 3:  # type: ignore
                text += f"- :warning: **{guild.member_count} members**\n"
            else:
                text += f"- {guild.member_count} members\n"

            # highlight if spawning is enabled
            if spawn_enabled:
                text += "- :warning: **Spawn is enabled**"
            else:
                text += "- Spawn is disabled"

            entries.append(TextDisplay(text))

        view = LayoutView()
        container = Container()
        view.add_item(container)
        section = Section(
            TextDisplay(f"## {len(guilds)} servers shared"),
            TextDisplay(f"{user.mention} ({user.id})"),
            accessory=Button(
                style=discord.ButtonStyle.link,
                label="View profile",
                url=f"discord://-/users/{user.id}",
                emoji="\N{LEFT-POINTING MAGNIFYING GLASS}",
            ),
        )
        container.add_item(section)

        if not self.bot.intents.members:
            section.add_item(
                TextDisplay(
                    "\N{WARNING SIGN} The bot cannot be aware of the member's "
                    "presence in servers, it is only aware of server ownerships."
                )
            )

        pages = Menu(
            self.bot, view, ListSource(await dynamic_chunks(view, iter_to_async(entries))), ItemFormatter(container, 1)
        )
        await pages.init()
        await ctx.send(view=view, ephemeral=True)

    @commands.hybrid_command(name="cardmaker")
    @app_commands.default_permissions()
    @app_commands.guilds(checks.ADMIN_GUILD_ID)
    @app_commands.check(checks.is_developer)
    @checks.is_superuser()
    @app_commands.describe(
        player_name="Unique identifier name of the cricketer (used for DB/file lookup)",
        display_name="Name shown on the card header — if blank, player_name is used",
        codename="Codename shown on the card (e.g. KING KOHLI)",
        description="Description text shown on the card (max 256 characters)",
        bat_score="Bat / health score shown on left (e.g. 342)",
        ball_score="Ball / attack score shown on right (e.g. 327)",
        rarity="Value shown on card badge (e.g. 50.0) — cosmetic only",
        spawn_chance="Spawn weight from 0 to 1000 — 1000 is max, 0 disables spawning",
        artwork_author="Name of the artwork creator",
        background="Preset name (e.g. custom_bg) or image URL",
        foreground="Player image URL or preset name (saved by player name after first use)",
        logo_url="Logo name (e.g. ICONS) or direct image URL shown on the card",
        event="Assign card to a special event (always spawns with it)",
        tradeable="Whether this card can be traded (default True)",
        unobtainable="If True, this card cannot be obtained from spawn, daily, or weekly rewards",
        catch_name="Name(s) players must type to catch this card. Separate multiples with semicolons (e.g. virat;vk;king)",
        foreground_border="Whether to draw a white border around the foreground image (default True)",
        credit_stroke="Whether to draw a stroke/outline on the credit and artwork author text (default True)",
        only_spawn_in_event="If True, this card only spawns while its assigned event is active. Once the event ends, it stops spawning.",
        credit_font="Font for 'Created by El Laggron' and 'Artwork' credit lines. Default = arial. optimus = Optimus Bold.",
        weekly_chance="Weekly weight from 0 to 1000 — 900 means 90%, 0 disables weekly spawning",
        daily_chance="Daily weight from 0 to 1000 — 900 means 90%, 0 disables daily spawning",
    )
    @app_commands.autocomplete(event=_event_autocomplete, background=_background_autocomplete)
    @app_commands.choices(credit_font=[
        app_commands.Choice(name="Default (Arial)", value="default"),
        app_commands.Choice(name="Optimus Bold", value="optimus"),
    ])
    async def cardmaker(
        self,
        ctx: commands.Context["CricStarBot"],
        player_name: str,
        codename: str,
        description: str,
        bat_score: int,
        ball_score: int,
        rarity: float,
        spawn_chance: app_commands.Range[float, 0.0, 1000.0],
        artwork_author: str,
        background: str,
        foreground: str,
        logo_url: str = "",
        event: str = "none",
        tradeable: bool = True,
        unobtainable: bool = False,
        display_name: str = "",
        catch_name: str = "",
        foreground_border: bool = True,
        credit_stroke: bool = True,
        only_spawn_in_event: bool = False,
        credit_font: str = "default",
        weekly_chance: app_commands.Range[float, 0.0, 1000.0] = 0.0,
        daily_chance: app_commands.Range[float, 0.0, 1000.0] = 0.0,
    ):
        """
        Generate a Dembele-style cricket card and add it to the database.
        rarity: badge display value (any number, e.g. 50.0).
        spawn_chance: spawn weight from 0 to 1000. Set to 0 to disable spawning.
        background: preset name or URL. foreground: URL or saved preset name.
        """
        await ctx.defer()

        if len(description) > 256:
            await ctx.send(
                f"❌ Description is too long — **{len(description)} characters**. "
                f"Maximum is **256 characters**. Please shorten it and try again.",
                ephemeral=True,
            )
            return

        # display_name is what shows on the card header; player_name is used for slug/file
        card_name = display_name.strip() if display_name.strip() else player_name

        slug = re.sub(r"[^a-z0-9]+", "_", player_name.lower().strip()).strip("_")
        filename = f"premade_{slug}.png"
        media_dir = Path("admin_panel/media")
        backgrounds_dir = Path("admin_panel/media/backgrounds")
        foregrounds_dir = Path("admin_panel/media/foregrounds")
        foregrounds_dir.mkdir(parents=True, exist_ok=True)

        if await Ball.objects.filter(country=player_name).aexists():
            await ctx.send(
                f"❌ A cricketer named **{player_name}** already exists. Use a unique name.",
                ephemeral=True,
            )
            return

        regime = await Regime.objects.afirst()
        if not regime:
            await ctx.send("❌ No regime found in the database. Add one first.", ephemeral=True)
            return

        def _fetch_image(name_or_url: str, dest: str, max_bytes: int = 10 * 1024 * 1024) -> bool:
            """Copy preset (backgrounds or foregrounds) or download URL to dest."""
            import shutil
            name_or_url = name_or_url.strip()
            if not name_or_url.startswith(("http://", "https://")):
                # Search backgrounds then foregrounds for a matching preset file
                for search_dir in (backgrounds_dir, foregrounds_dir):
                    for ext in (".jpg", ".jpeg", ".png", ".webp", ""):
                        candidate = search_dir / f"{name_or_url}{ext}"
                        if candidate.exists():
                            shutil.copy2(str(candidate), dest)
                            return True
                return False
            try:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                req = urllib.request.Request(
                    name_or_url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"
                        ),
                        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                    },
                )
                with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
                    data = resp.read(max_bytes)
                with open(dest, "wb") as f:
                    f.write(data)
                return True
            except Exception as e:
                log.warning("_fetch_image failed for %r: %s", name_or_url[:80], e)
                return False

        with tempfile.TemporaryDirectory() as tmpdir:
            bg_path = os.path.join(tmpdir, "bg.img")
            fg_path = os.path.join(tmpdir, "fg.img")
            logo_path: str | None = None

            with ThreadPoolExecutor() as pool:
                bg_ok, fg_ok = await self.bot.loop.run_in_executor(
                    pool,
                    lambda: (
                        _fetch_image(background, bg_path),
                        _fetch_image(foreground, fg_path),
                    ),
                )

            if not bg_ok:
                await ctx.send(
                    f"❌ Could not load background `{background}`. "
                    f"Check the preset name or URL and try again.",
                    ephemeral=True,
                )
                return
            if not fg_ok:
                await ctx.send(
                    f"❌ Could not download foreground from the provided URL or preset name. "
                    f"Make sure it is a direct image link.",
                    ephemeral=True,
                )
                return

            # Save foreground as a preset named after this player for future reuse
            fg_preset = foregrounds_dir / slug
            import shutil as _shutil
            _shutil.copy2(fg_path, str(fg_preset))

            if logo_url.strip():
                logo_path = os.path.join(tmpdir, "logo.png")
                logo_ok = await self._resolve_logo(logo_url.strip(), logo_path)
                if not logo_ok:
                    logo_path = None

            def _generate() -> tuple:
                return draw_premade_card(
                    bg_path, fg_path, card_name, codename, description,
                    rarity, bat_score, ball_score, artwork_author, logo_path,
                    neon_color=get_neon_color(player_name),
                    foreground_border=foreground_border,
                    credit_stroke=credit_stroke,
                    credit_font=credit_font,
                )

            with ThreadPoolExecutor() as pool:
                image, img_kwargs = await self.bot.loop.run_in_executor(pool, _generate)

            card_path = media_dir / filename
            image.save(str(card_path), **img_kwargs)
            image.close()

            spawnable = (spawn_chance > 0)
            ball = await Ball.objects.acreate(
                country=player_name,
                health=bat_score,
                attack=ball_score,
                rarity=spawn_chance,
                emoji_id=0,
                wild_card=filename,
                collection_card=filename,
                credits=artwork_author,
                capacity_name=codename,
                capacity_description=description,
                capacity_logic={
                    "badge_rarity": rarity,
                    "bg_preset": background.strip(),
                    "foreground_border": foreground_border,
                    "credit_stroke": credit_stroke,
                    "only_spawn_in_event": only_spawn_in_event,
                    "credit_font": credit_font,
                    "weekly_chance": weekly_chance,
                    "daily_chance": daily_chance,
                    **({"display_name": card_name} if card_name != player_name else {}),
                },
                regime=regime,
                tradeable=tradeable,
                spawnable=spawnable,
                unobtainable=unobtainable,
                catch_names=catch_name.strip().lower() or None,
            )

            event_text = ""
            if event != "none":
                try:
                    special = await Special.objects.aget(name=event)
                    ball.capacity_logic = {**ball.capacity_logic, "forced_special": special.id}
                    await ball.asave(update_fields=["capacity_logic"])
                    specials_cache[special.id] = special
                    event_text = f" | Event: **{event}**"
                except Special.DoesNotExist:
                    # Roll back the ball creation and fail fast
                    await ball.adelete()
                    card_path.unlink(missing_ok=True)
                    await ctx.send(
                        f"❌ Special event **{event}** was not found in the database.\n"
                        f"Run `setup_specials` or create it in the admin panel first.",
                        ephemeral=True,
                    )
                    return

            balls_cache[ball.id] = ball

            event_id = ball.capacity_logic.get("forced_special") if event != "none" else None
            try:
                _export_card(
                    player_name=player_name,
                    card_name=card_name,
                    slug=slug,
                    codename=codename,
                    description=description,
                    bat_score=bat_score,
                    ball_score=ball_score,
                    rarity=rarity,
                    spawn_chance=spawn_chance,
                    artwork_author=artwork_author,
                    tradeable=tradeable,
                    spawnable=spawnable,
                    unobtainable=unobtainable,
                    catch_name=catch_name.strip().lower() or None,
                    event_id=event_id,
                    event_name=event if event != "none" else "",
                    filename=filename,
                    wild_card_filename=filename,
                    capacity_logic=ball.capacity_logic,
                )
            except Exception as export_err:
                log.warning("card_sync export failed (non-critical): %s", export_err)

            preview_file = discord.File(str(card_path), filename=filename)
            try:
                await ctx.send(
                    f"✅ **{player_name}** card created!{event_text}\n"
                    f"`{filename}` | Badge Rarity: `{rarity}` | Spawn: `{spawn_chance}/1000` | Weekly: `{weekly_chance}/1000` | Daily: `{daily_chance}/1000` | Tradeable: `{tradeable}` | Spawnable: `{spawnable}` | Unobtainable: `{unobtainable}`\n"
                    f"BAT: `{bat_score}` | BALL: `{ball_score}` | Artwork: {artwork_author}\n"
                    f"Foreground saved as preset `{slug}` for future reuse.",
                    file=preview_file,
                )
            except Exception as send_err:
                log.error(f"cardmaker: preview send failed for {filename}: {send_err}")
                await ctx.send(
                    f"✅ **{player_name}** card created and saved as `{filename}`, "
                    f"but preview upload failed: {send_err}"
                )
                
    @commands.hybrid_command(name="editcard")
    @app_commands.default_permissions()
    @app_commands.guilds(checks.ADMIN_GUILD_ID)
    @app_commands.check(checks.is_developer)
    @checks.is_superuser()
    @app_commands.describe(
        player_name="Exact name of the cricketer to edit (use the name stored in DB)",
        display_name="Change the name shown on the card header (leave blank to keep existing)",
        background="Preset name or URL (leave blank to keep / use custom_bg)",
        foreground="Player image URL or preset name (leave blank to use saved preset)",
        codename="New codename shown on card (leave blank to keep existing)",
        description="New description text, max 256 characters (leave blank to keep existing)",
        bat_score="New bat / health score (leave blank to keep existing)",
        ball_score="New ball / attack score (leave blank to keep existing)",
        rarity="New badge display value — cosmetic only (leave blank to keep existing)",
        spawn_chance="New spawn weight from 0 to 1000 — set to 0 to disable spawning",
        artwork_author="New artwork author name (leave blank to keep existing)",
        logo_url="Logo name (e.g. ICONS) or URL — leave blank to keep existing",
        tradeable="Change tradeability (leave blank to keep existing)",
        unobtainable="Change unobtainable status. True blocks spawn, daily, and weekly rewards.",
        catch_name="Name(s) players must type to catch this card. Separate multiples with semicolons (e.g. virat;vk;king)",
        only_spawn_in_event="If True, card only spawns while its event is active. Set False to allow spawning anytime.",
        credit_font="Font for credit lines. Leave blank to keep existing. 'Default (Arial)' or 'Optimus Bold'.",
        event="Assign or change the special event for this card. Choose 'none' to remove the current event.",
        weekly_chance="New weekly weight from 0 to 1000 — leave blank to keep existing. 0 disables weekly spawning.",
        daily_chance="New daily weight from 0 to 1000 — leave blank to keep existing. 0 disables daily spawning.",
    )
    @app_commands.autocomplete(background=_background_autocomplete, event=_event_autocomplete)
    @app_commands.choices(credit_font=[
        app_commands.Choice(name="Keep existing", value=""),
        app_commands.Choice(name="Default (Arial)", value="default"),
        app_commands.Choice(name="Optimus Bold", value="optimus"),
    ])
    async def editcard(
        self,
        ctx: commands.Context["CricStarBot"],
        player_name: str,
        display_name: str = "",
        background: str = "",
        foreground: str = "",
        codename: str = "",
        description: str = "",
        bat_score: int | None = None,
        ball_score: int | None = None,
        rarity: float | None = None,
        spawn_chance: app_commands.Range[float, 0.0, 1000.0] | None = None,
        artwork_author: str = "",
        logo_url: str = "",
        tradeable: bool | None = None,
        unobtainable: bool | None = None,
        catch_name: str = "",
        only_spawn_in_event: bool | None = None,
        credit_font: str = "",
        event: str = "",
        weekly_chance: app_commands.Range[float, 0.0, 1000.0] | None = None,
        daily_chance: app_commands.Range[float, 0.0, 1000.0] | None = None,
    ):
        """
        Edit an existing cricket card. Only supply the fields you want to change.
        Provide background/foreground to regenerate the card image.
        background: preset name or URL. foreground: URL or saved preset slug.
        Set spawn_chance to 0 to disable random spawning.
        """
        await ctx.defer()

        if description.strip() and len(description) > 256:
            await ctx.send(
                f"❌ Description is too long — **{len(description)} characters**. "
                f"Maximum is **256 characters**. Please shorten it and try again.",
                ephemeral=True,
            )
            return

        slug = re.sub(r"[^a-z0-9]+", "_", player_name.lower().strip()).strip("_")
        media_dir = Path("admin_panel/media")
        backgrounds_dir = Path("admin_panel/media/backgrounds")
        foregrounds_dir = Path("admin_panel/media/foregrounds")
        foregrounds_dir.mkdir(parents=True, exist_ok=True)

        ball = None
        try:
            ball = await Ball.objects.aget(country=player_name)
        except Ball.DoesNotExist:
            pass

        if ball is None:
            premade_slug_file = f"premade_{slug}.png"
            try:
                ball = await Ball.objects.aget(wild_card=premade_slug_file)
            except Ball.DoesNotExist:
                pass

        if ball is None:
            await ctx.send(
                f"❌ No cricketer named **{player_name}** found. "
                f"Check the exact name (try the display name or the original player_name) and try again.",
                ephemeral=True,
            )
            return

        # --- Determine whether to regenerate the card image ---
        # Regenerate when background/foreground is explicitly given, OR when any
        # visual field (things drawn on the card image) is changing.
        _visual_fields_changing = bool(
            codename.strip() or description.strip() or display_name.strip()
            or bat_score is not None or ball_score is not None
            or rarity is not None or artwork_author.strip() or logo_url.strip()
            or credit_font
        )
        wild_card_name = ball.wild_card.name if ball.wild_card else ""
        is_premade = wild_card_name.startswith("premade_")
        regen = bool(background.strip() or foreground.strip()) or (is_premade and _visual_fields_changing)

        logic = dict(ball.capacity_logic or {})

        # Use the stored background preset from when the card was created (via cardmaker),
        # so changing only codename/text never accidentally swaps the background.
        _stored_bg = logic.get("bg_preset", "custom_bg") or "custom_bg"
        bg_source = background.strip() or _stored_bg
        fg_source = foreground.strip() or slug

        # Retrieve stored foreground_border / credit_stroke / credit_font so they are preserved on regen
        _stored_fg_border = logic.get("foreground_border", True)
        _stored_credit_stroke = logic.get("credit_stroke", True)
        _stored_credit_font = logic.get("credit_font", "default")

        existing_collection_name = ball.collection_card.name if ball.collection_card else ""
        existing_wild_name = ball.wild_card.name if ball.wild_card else ""
        filename = existing_collection_name or f"premade_{slug}.png"
        output_filename = filename if filename.startswith("premade_") else f"premade_{slug}.png"
        changed_fields: list[str] = []

        def _fetch_image(
            name_or_url: str,
            dest: str,
            search_dirs: tuple[Path, ...],
            max_bytes: int = 10 * 1024 * 1024,
        ) -> bool:
            import shutil
            name_or_url = name_or_url.strip()
            if not name_or_url.startswith(("http://", "https://")):
                for search_dir in search_dirs:
                    for ext in (".jpg", ".jpeg", ".png", ".webp", ""):
                        candidate = search_dir / f"{name_or_url}{ext}"
                        if candidate.exists():
                            shutil.copy2(str(candidate), dest)
                            return True
                return False
            try:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                req = urllib.request.Request(
                    name_or_url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"
                        ),
                        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                    },
                )
                with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
                    data = resp.read(max_bytes)
                with open(dest, "wb") as f:
                    f.write(data)
                return True
            except Exception as e:
                log.warning("_fetch_image failed for %r: %s", name_or_url[:80], e)
                return False

        if regen:
            with tempfile.TemporaryDirectory() as tmpdir:
                bg_path = os.path.join(tmpdir, "bg.img")
                fg_path = os.path.join(tmpdir, "fg.img")
                logo_path: str | None = None

                with ThreadPoolExecutor() as pool:
                    bg_ok, fg_ok = await self.bot.loop.run_in_executor(
                        pool,
                        lambda: (
                            _fetch_image(bg_source, bg_path, (backgrounds_dir,)),
                            _fetch_image(fg_source, fg_path, (foregrounds_dir,)),
                        ),
                    )

                if not bg_ok:
                    await ctx.send(
                        f"❌ Could not load background `{bg_source}`. "
                        f"Check the preset name or URL.",
                        ephemeral=True,
                    )
                    return
                if not fg_ok:
                    await ctx.send(
                        f"❌ Could not load foreground `{fg_source}`. "
                        f"Provide a direct image URL or make sure the preset exists.",
                        ephemeral=True,
                    )
                    return

                if foreground.strip():
                    fg_preset = foregrounds_dir / slug
                    import shutil as _shutil
                    _shutil.copy2(fg_path, str(fg_preset))

                # Resolve display values (use new value or fall back to existing DB value)
                _card_name = display_name.strip() or logic.get("display_name") or ball.country
                _codename = codename.strip() or ball.capacity_name or ""
                _description = description.strip() or ball.capacity_description or ""
                _rarity = rarity if rarity is not None else logic.get("badge_rarity", ball.rarity)
                _bat = bat_score if bat_score is not None else ball.health
                _ball = ball_score if ball_score is not None else ball.attack
                _author = artwork_author.strip() or ball.credits or ""

                # Optional logo
                logo_url_clean = logo_url.strip()
                if logo_url_clean:
                    logo_path = os.path.join(tmpdir, "logo.png")
                    logo_ok = await self._resolve_logo(logo_url_clean, logo_path)
                    if not logo_ok:
                        logo_path = None

                _resolved_credit_font = credit_font if credit_font else _stored_credit_font

                def _generate() -> tuple:
                    return draw_premade_card(
                        bg_path, fg_path, _card_name, _codename, _description,
                        _rarity, _bat, _ball, _author, logo_path,
                        neon_color=get_neon_color(player_name),
                        foreground_border=_stored_fg_border,
                        credit_stroke=_stored_credit_stroke,
                        credit_font=_resolved_credit_font,
                    )

                with ThreadPoolExecutor() as pool:
                    image, img_kwargs = await self.bot.loop.run_in_executor(pool, _generate)

                card_path = media_dir / output_filename
                image.save(str(card_path), **img_kwargs)
                image.close()

                ball.collection_card = output_filename
                changed_fields.append("collection_card")

        # --- Apply text / stat field changes ---
        if display_name.strip():
            logic["display_name"] = display_name.strip()
            ball.capacity_logic = logic
            if "capacity_logic" not in changed_fields:
                changed_fields.append("capacity_logic")
        if codename.strip():
            ball.capacity_name = codename.strip()
            changed_fields.append("capacity_name")
        if description.strip():
            ball.capacity_description = description.strip()
            changed_fields.append("capacity_description")
        if bat_score is not None:
            ball.health = bat_score
            changed_fields.append("health")
        if ball_score is not None:
            ball.attack = ball_score
            changed_fields.append("attack")
        if spawn_chance is not None:
            ball.rarity = spawn_chance
            changed_fields.append("rarity")
        if rarity is not None:
            logic["badge_rarity"] = rarity
            ball.capacity_logic = logic
            if "capacity_logic" not in changed_fields:
                changed_fields.append("capacity_logic")
        if background.strip():
            logic["bg_preset"] = background.strip()
            ball.capacity_logic = logic
            if "capacity_logic" not in changed_fields:
                changed_fields.append("capacity_logic")
        if artwork_author.strip():
            ball.credits = artwork_author.strip()
            changed_fields.append("credits")
        if tradeable is not None:
            ball.tradeable = tradeable
            changed_fields.append("tradeable")
        if unobtainable is not None:
            ball.unobtainable = unobtainable
            changed_fields.append("unobtainable")
        if spawn_chance is not None:
            ball.spawnable = (spawn_chance > 0)
            changed_fields.append("spawnable")
        if catch_name.strip():
            ball.catch_names = catch_name.strip().lower()
            changed_fields.append("catch_names")
        if only_spawn_in_event is not None:
            logic["only_spawn_in_event"] = only_spawn_in_event
            ball.capacity_logic = logic
            if "capacity_logic" not in changed_fields:
                changed_fields.append("capacity_logic")
        if credit_font:
            logic["credit_font"] = credit_font
            ball.capacity_logic = logic
            if "capacity_logic" not in changed_fields:
                changed_fields.append("capacity_logic")
        if weekly_chance is not None:
            logic["weekly_chance"] = weekly_chance
            ball.capacity_logic = logic
            if "capacity_logic" not in changed_fields:
                changed_fields.append("capacity_logic")
        if daily_chance is not None:
            logic["daily_chance"] = daily_chance
            ball.capacity_logic = logic
            if "capacity_logic" not in changed_fields:
                changed_fields.append("capacity_logic")

        event_summary = ""
        if event.strip():
            if event.strip().lower() == "none":
                # Remove forced_special from capacity_logic
                logic = dict(ball.capacity_logic or {})
                if "forced_special" in logic:
                    del logic["forced_special"]
                    ball.capacity_logic = logic
                    if "capacity_logic" not in changed_fields:
                        changed_fields.append("capacity_logic")
                    event_summary = "Event → `removed`"
            else:
                try:
                    special = await Special.objects.aget(name=event.strip())
                    logic["forced_special"] = special.id
                    ball.capacity_logic = logic
                    if "capacity_logic" not in changed_fields:
                        changed_fields.append("capacity_logic")
                    specials_cache[special.id] = special
                    event_summary = f"Event → `{special.name}`"
                except Special.DoesNotExist:
                    await ctx.send(
                        f"❌ Special event **{event.strip()}** was not found in the database.\n"
                        f"Use autocomplete to pick a valid event, or type `none` to remove the current one.",
                        ephemeral=True,
                    )
                    return

        if not changed_fields:
            await ctx.send(
                "⚠️ Nothing to change — you didn't supply any new values.",
                ephemeral=True,
            )
            return

        await ball.asave(update_fields=list(set(changed_fields)))
        balls_cache[ball.id] = ball

        try:
            _ev_id = (ball.capacity_logic or {}).get("forced_special")
            _ev_name = specials_cache[_ev_id].name if _ev_id and _ev_id in specials_cache else ""
            _export_card(
                player_name=ball.country,
                card_name=(ball.capacity_logic or {}).get("display_name") or ball.country,
                slug=slug,
                codename=ball.capacity_name or "",
                description=ball.capacity_description or "",
                bat_score=ball.health,
                ball_score=ball.attack,
                rarity=(ball.capacity_logic or {}).get("badge_rarity", 1.0),
                spawn_chance=ball.rarity,
                artwork_author=ball.credits or "",
                tradeable=ball.tradeable,
                spawnable=ball.spawnable,
                unobtainable=ball.unobtainable,
                catch_name=ball.catch_names or None,
                event_id=_ev_id,
                event_name=_ev_name,
                filename=ball.collection_card.name if ball.collection_card else output_filename,
                wild_card_filename=existing_wild_name or (ball.wild_card.name if ball.wild_card else output_filename),
                capacity_logic=ball.capacity_logic,
            )
        except Exception as _export_err:
            log.warning("editcard: card_sync export failed (non-critical): %s", _export_err)

        summary_parts = []
        if regen:
            summary_parts.append("Card image regenerated")
        if display_name.strip():
            summary_parts.append(f"Display name → `{display_name.strip()}` *(image only)*")
        if codename.strip():
            summary_parts.append(f"Codename → `{ball.capacity_name}`")
        if description.strip():
            summary_parts.append(f"Description updated")
        if bat_score is not None:
            summary_parts.append(f"BAT → `{ball.health}`")
        if ball_score is not None:
            summary_parts.append(f"BALL → `{ball.attack}`")
        if spawn_chance is not None:
            spawn_label = f"Spawn chance → `{spawn_chance}/1000`" + (" *(spawning disabled)*" if spawn_chance == 0 else "")
            summary_parts.append(spawn_label)
        if artwork_author.strip():
            summary_parts.append(f"Author → `{ball.credits}`")
        if background.strip():
            summary_parts.append(f"Background → `{background.strip()}`")
        if foreground.strip():
            summary_parts.append(f"Foreground → `{foreground.strip()}`")
        if rarity is not None:
            summary_parts.append(f"Badge rarity → `{rarity}`")
        if credit_font:
            summary_parts.append(f"Credit font → `{credit_font}`")
        if logo_url.strip():
            summary_parts.append("Logo updated on regenerated image")
        if catch_name.strip():
            summary_parts.append("Catch name updated")
        if only_spawn_in_event is not None:
            summary_parts.append(f"Only spawn in event → `{only_spawn_in_event}`")
        if tradeable is not None:
            summary_parts.append(f"Tradeable → `{ball.tradeable}`")
        if unobtainable is not None:
            summary_parts.append(f"Unobtainable → `{ball.unobtainable}`")
        if weekly_chance is not None:
            weekly_label = f"Weekly chance → `{weekly_chance}/1000`" + (" *(disabled)*" if weekly_chance == 0 else "")
            summary_parts.append(weekly_label)
        if daily_chance is not None:
            daily_label = f"Daily chance → `{daily_chance}/1000`" + (" *(disabled)*" if daily_chance == 0 else "")
            summary_parts.append(daily_label)
        if event_summary:
            summary_parts.append(event_summary)

        summary = " | ".join(summary_parts)
        card_path = media_dir / output_filename

        if regen and card_path.exists():
            preview_file = discord.File(str(card_path), filename=output_filename)
            try:
                await ctx.send(
                    f"✅ **{player_name}** updated!\n{summary}",
                    file=preview_file,
                )
            except discord.HTTPException as send_err:
                log.error(f"editcard: preview send failed: {send_err}")
                # Do NOT send a fallback success message — the card may have already
                # been delivered to Discord and a second ctx.send would create a duplicate.
        else:
            await ctx.send(f"✅ **{player_name}** updated!\n{summary}")

    async def _player_name_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Return up to 25 player names matching the current input."""
        current_lower = current.lower()
        matches = [
            app_commands.Choice(name=ball.country, value=ball.country)
            for ball in balls_cache.values()
            if current_lower in ball.country.lower()
        ]
        return matches[:25]

    @commands.hybrid_command(name="setspawnimg")
    @app_commands.default_permissions()
    @app_commands.guilds(checks.ADMIN_GUILD_ID)
    @app_commands.check(checks.is_developer)
    @checks.is_superuser()
    @app_commands.describe(
        player_name="Select the cricketer to update",
        url="Direct image URL to download (http/https). Required if path_name file doesn't already exist.",
        path_name="Preset name to save/load the image as (e.g. ViratKohlispawn, dhoni_spawn). No extension needed.",
    )
    @app_commands.autocomplete(player_name=_player_name_autocomplete)
    async def setspawnimg(
        self,
        ctx: commands.Context["CricStarBot"],
        player_name: str,
        url: str = "",
        path_name: str = "",
    ):
        """
        Set the spawn image for a cricketer.
        - url only          → download and assign (saved as spawn_<slug>.<ext>)
        - path_name only    → use an existing preset file from the media folder
        - url + path_name   → download from URL and ALSO save under that path_name as a reusable preset
        """
        await ctx.defer()

        url = url.strip()
        path_name = path_name.strip()

        if not url and not path_name:
            await ctx.send(
                "❌ Provide at least one of: `url` (image link) or `path_name` (existing preset name).",
                ephemeral=True,
            )
            return

        # Lookup the ball
        ball: Ball | None = None
        try:
            ball = await Ball.objects.aget(country=player_name)
        except Ball.DoesNotExist:
            pass

        if ball is None:
            slug_fallback = re.sub(r"[^a-z0-9]+", "_", player_name.lower().strip()).strip("_")
            try:
                ball = await Ball.objects.aget(wild_card=f"premade_{slug_fallback}.png")
            except Ball.DoesNotExist:
                pass

        if ball is None:
            close = [
                b.country for b in balls_cache.values()
                if player_name.lower() in b.country.lower()
            ][:5]
            hint = f"\nDid you mean: {', '.join(close)}?" if close else ""
            await ctx.send(
                f"❌ No cricketer named **{player_name}** found.{hint}",
                ephemeral=True,
            )
            return

        import shutil as _shutil
        media_dir = Path("admin_panel/media")
        slug = re.sub(r"[^a-z0-9]+", "_", player_name.lower().strip()).strip("_")

        # Block if a custom spawn image is already set for this cricketer
        if _has_custom_spawn_image(slug):
            await ctx.send(
                f"❌ **{ball.country}** already has a custom spawn image set.\n"
                f"Use `/removespawnpath` to remove it first before setting a new one.",
                ephemeral=True,
            )
            return

        def _download(src_url: str, dest: str) -> bool:
            try:
                req = urllib.request.Request(src_url, headers={"User-Agent": "CricStar-Bot/1.0"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = resp.read(15 * 1024 * 1024)
                with open(dest, "wb") as f:
                    f.write(data)
                return True
            except Exception as exc:
                log.error(f"setspawnimg: download failed: {exc}")
                return False

        def _ext_from_url(u: str) -> str:
            base = u.split("?")[0].lower()
            for candidate in (".png", ".webp", ".gif", ".jpeg", ".jpg"):
                if base.endswith(candidate):
                    return candidate
            return ".jpg"

        dest_path: str
        filename: str
        saved_preset: str | None = None      # path_name preset saved to disk
        source_label: str

        if url and path_name:
            # ── URL + path_name: download AND save as named preset ────────
            ext = _ext_from_url(url)
            preset_filename = f"{path_name}{ext}"
            preset_path = str(media_dir / preset_filename)

            with ThreadPoolExecutor() as pool:
                ok = await self.bot.loop.run_in_executor(pool, _download, url, preset_path)

            if not ok:
                await ctx.send(
                    "❌ Could not download the image. Make sure it's a direct link to an image file.",
                    ephemeral=True,
                )
                return

            # Also keep a spawn_<slug> copy as the wild_card entry
            filename = f"spawn_{slug}{ext}"
            dest_path = str(media_dir / filename)
            _shutil.copy2(preset_path, dest_path)

            saved_preset = preset_filename
            source_label = f"URL → saved as preset `{preset_filename}`"

        elif url:
            # ── URL only: download and save as spawn_<slug> ───────────────
            ext = _ext_from_url(url)
            filename = f"spawn_{slug}{ext}"
            dest_path = str(media_dir / filename)

            with ThreadPoolExecutor() as pool:
                ok = await self.bot.loop.run_in_executor(pool, _download, url, dest_path)

            if not ok:
                await ctx.send(
                    "❌ Could not download the image. Make sure it's a direct link to an image file.",
                    ephemeral=True,
                )
                return

            source_label = "URL"

        else:
            # ── path_name only: look up existing preset in media folder ───
            found: Path | None = None
            for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ""):
                candidate = media_dir / f"{path_name}{ext}"
                if candidate.exists():
                    found = candidate
                    break

            if found is None:
                await ctx.send(
                    f"❌ No file named **`{path_name}`** found in the media folder.\n"
                    f"Upload the file first, or provide a `url` along with the `path_name` to download it.",
                    ephemeral=True,
                )
                return

            ext_found = found.suffix or ".jpg"
            filename = f"spawn_{slug}{ext_found}"
            dest_path = str(media_dir / filename)

            if str(found) != dest_path:
                _shutil.copy2(str(found), dest_path)

            source_label = f"preset `{path_name}`"

        ball.wild_card = filename
        await ball.asave(update_fields=["wild_card"])
        balls_cache[ball.id] = ball

        # Persist to card_exports/spawns/ so image survives restarts/remixes
        _export_spawn_image(ball.country, slug, dest_path)

        extra = f"\n• Preset saved as `{saved_preset}` — reuse with `path_name:{path_name}`" if saved_preset else ""
        preview_file = discord.File(dest_path, filename=filename)
        try:
            await ctx.send(
                f"✅ Spawn image permanently set for **{ball.country}** (from {source_label})!\n"
                f"• Assigned file: `{filename}`{extra}\n"
                f"• Use `/removespawnpath` to remove it if you ever want to change it.",
                file=preview_file,
            )
        except Exception as send_err:
            log.error(f"setspawnimg: preview send failed: {send_err}")
            await ctx.send(
                f"✅ Spawn image permanently set for **{ball.country}**! Saved as `{filename}`.\n"
                f"• Use `/removespawnpath` to remove it if you ever want to change it."
            )

    async def _spawn_path_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete listing all custom spawn images from card_exports/spawns/."""
        all_spawns = _list_spawn_images()
        current_lower = current.lower()
        matches = [
            app_commands.Choice(name=name, value=name)
            for name in all_spawns
            if current_lower in name.lower()
        ]
        return matches[:25]

    @commands.hybrid_command(name="removespawnpath")
    @app_commands.default_permissions()
    @app_commands.guilds(checks.ADMIN_GUILD_ID)
    @app_commands.check(checks.is_developer)
    @checks.is_superuser()
    @app_commands.describe(
        path_name="Select the spawn image path to remove (all existing paths are listed here)",
    )
    @app_commands.autocomplete(path_name=_spawn_path_autocomplete)
    async def removespawnpath(
        self,
        ctx: commands.Context["CricStarBot"],
        path_name: str,
    ):
        """
        Remove a custom spawn image. All existing spawn paths are shown in the dropdown.
        After removal the cricketer will use its collection card image when spawning.
        """
        await ctx.defer(ephemeral=True)

        path_name = path_name.strip()
        if not path_name:
            await ctx.send("❌ Please select a spawn path from the dropdown.", ephemeral=True)
            return

        # Validate the file exists in card_exports/spawns/
        all_spawns = _list_spawn_images()
        if path_name not in all_spawns:
            if all_spawns:
                listing = "\n".join(f"• `{n}`" for n in all_spawns)
                await ctx.send(
                    f"❌ `{path_name}` not found in spawn paths. Currently saved paths:\n{listing}",
                    ephemeral=True,
                )
            else:
                await ctx.send("❌ No custom spawn images are currently saved.", ephemeral=True)
            return

        # Derive slug from filename: "spawn_{slug}.ext" → "{slug}"
        stem = Path(path_name).stem  # e.g. "spawn_virat_kohli"
        if stem.startswith("spawn_"):
            slug = stem[len("spawn_"):]
        else:
            slug = stem

        # Find matching ball in DB
        ball: Ball | None = None
        for b in balls_cache.values():
            b_slug = re.sub(r"[^a-z0-9]+", "_", b.country.lower().strip()).strip("_")
            if b_slug == slug:
                ball = b
                break

        media_dir = Path("admin_panel/media")

        # Remove from card_exports/spawns/ and update cards.json
        player_name_key = ball.country if ball else slug
        removed = _remove_spawn_image(player_name_key, slug)

        # Reset the ball's wild_card in DB to the collection_card
        if ball:
            collection_filename = str(ball.collection_card)
            ball.wild_card = collection_filename
            await ball.asave(update_fields=["wild_card"])
            balls_cache[ball.id] = ball

            # Also delete from media/ if it exists there
            media_spawn = media_dir / path_name
            if media_spawn.exists():
                media_spawn.unlink()

            await ctx.send(
                f"✅ Spawn image `{path_name}` removed for **{ball.country}**.\n"
                f"The cricketer will now use its collection card image when spawning.\n"
                f"You can now set a new spawn image with `/setspawnimg`.",
                ephemeral=True,
            )
        else:
            # Ball not found in cache but file was removed from card_exports
            if removed:
                await ctx.send(
                    f"✅ Spawn path `{path_name}` removed from permanent storage.\n"
                    f"⚠️ Could not find matching cricketer in cache — the DB wild_card was not updated.\n"
                    f"Restart the bot to resync if needed.",
                    ephemeral=True,
                )
            else:
                await ctx.send(
                    f"❌ Could not remove `{path_name}` — file may have already been deleted.",
                    ephemeral=True,
                )

        log.info(f"removespawnpath: removed '{path_name}' by {ctx.author}")

    @commands.hybrid_command(name="createevent")
    @app_commands.default_permissions()
    @app_commands.guilds(checks.ADMIN_GUILD_ID)
    @app_commands.check(checks.is_developer)
    @checks.is_superuser()
    @app_commands.describe(
        name="Event name (e.g. IPL 2026, T20 World Cup) — must be unique",
        emoji="An emoji or Discord emoji ID shown next to the event name",
        catch_phrase="Message shown when catching a card with this event (max 128 chars)",
        start_date="When the event starts (YYYY-MM-DD or YYYY-MM-DD HH:MM). Leave blank to start immediately",
        end_date="When the event ends (YYYY-MM-DD or YYYY-MM-DD HH:MM). Leave blank to never expire",
        tradeable="Whether cards with this event can be traded (default True)",
        hidden="If True, hides this event from player-facing commands. False means everyone can see it.",
    )
    async def createevent(
        self,
        ctx: commands.Context["CricStarBot"],
        name: str,
        emoji: str = "",
        catch_phrase: str = "",
        start_date: str = "",
        end_date: str = "",
        tradeable: bool = True,
        hidden: bool = False,
    ):
        """
        Create a new special event. Cards assigned to this event via /cardmaker can be set to
        only spawn during the event's active period. Dates are optional — leave blank to
        start immediately / never expire.
        """
        import datetime
        from django.utils import timezone as tz

        await ctx.defer(ephemeral=True)

        name = name.strip()
        if not name:
            await ctx.send("❌ Event name cannot be blank.", ephemeral=True)
            return

        if len(name) > 64:
            await ctx.send(
                f"❌ Event name is too long ({len(name)} chars). Maximum is 64 characters.",
                ephemeral=True,
            )
            return

        if catch_phrase and len(catch_phrase) > 128:
            await ctx.send(
                f"❌ Catch phrase is too long ({len(catch_phrase)} chars). Maximum is 128 characters.",
                ephemeral=True,
            )
            return

        # Check for duplicate name
        if await Special.objects.filter(name=name).aexists():
            await ctx.send(
                f"❌ An event named **{name}** already exists. Use a different name.",
                ephemeral=True,
            )
            return

        def _parse_date(s: str) -> datetime.datetime | None:
            s = s.strip()
            if not s:
                return None
            for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    naive = datetime.datetime.strptime(s, fmt)
                    return tz.make_aware(naive)
                except ValueError:
                    continue
            return "invalid"

        parsed_start: datetime.datetime | None = None
        parsed_end: datetime.datetime | None = None

        if start_date.strip():
            parsed_start = _parse_date(start_date)
            if parsed_start == "invalid":
                await ctx.send(
                    f"❌ Invalid start date `{start_date}`. Use format `YYYY-MM-DD` or `YYYY-MM-DD HH:MM`.",
                    ephemeral=True,
                )
                return

        if end_date.strip():
            parsed_end = _parse_date(end_date)
            if parsed_end == "invalid":
                await ctx.send(
                    f"❌ Invalid end date `{end_date}`. Use format `YYYY-MM-DD` or `YYYY-MM-DD HH:MM`.",
                    ephemeral=True,
                )
                return

        if parsed_start and parsed_end and parsed_end <= parsed_start:
            await ctx.send("❌ End date must be after start date.", ephemeral=True)
            return

        special = await Special.objects.acreate(
            name=name,
            rarity=0.0,
            emoji=emoji.strip() or None,
            catch_phrase=catch_phrase.strip() or None,
            start_date=parsed_start,
            end_date=parsed_end,
            tradeable=tradeable,
            hidden=hidden,
        )
            
        specials_cache[special.id] = special

        try:
            _export_event(special)
        except Exception as export_err:
            log.warning("card_sync event export failed (non-critical): %s", export_err)

        log.info(f"createevent: '{name}' (id={special.id}) created by {ctx.author}")

        # Build summary
        start_label = parsed_start.strftime("%Y-%m-%d %H:%M") if parsed_start else "immediately"
        end_label = parsed_end.strftime("%Y-%m-%d %H:%M") if parsed_end else "never (permanent)"
        status_label = "Hidden" if hidden else "Visible"

        summary_lines = [
            f"✅ **Event `{name}` created!** (ID: `{special.id}`)\n",
            f"• **Emoji:** {emoji.strip() or '*(none)*'}",
            f"• **Catch phrase:** {catch_phrase.strip() or '*(none)*'}",
            f"• **Starts:** {start_label}",
            f"• **Ends:** {end_label}",
            f"• **Tradeable:** {tradeable}",
            f"• **Status:** {status_label}",
        ]

        summary_lines.append(
            f"\nThe event is now **live** in the bot cache. "
            f"Use `/cardmaker` → event field to assign cards to **{name}**."
        )

        await ctx.send("\n".join(summary_lines), ephemeral=True)

    async def _event_name_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Return up to 25 event names matching the current input."""
        current_lower = current.lower()
        matches = [
            app_commands.Choice(name=special.name, value=special.name)
            for special in specials_cache.values()
            if current_lower in special.name.lower()
        ]
        return matches[:25]

    @commands.hybrid_command(name="evmake")
    @app_commands.default_permissions()
    @app_commands.guilds(checks.ADMIN_GUILD_ID)
    @app_commands.check(checks.is_developer)
    @checks.is_superuser()
    @app_commands.describe(
        event_name="Name of the event (must be unique)",
        catch_phrase="Message shown when a player catches a card from this event (max 128 chars)",
    )
    async def evmake(
        self,
        ctx: commands.Context["CricStarBot"],
        event_name: str,
        catch_phrase: str = "",
    ):
        """
        Create an event with a name and optional catch phrase.
        """
        await ctx.defer(ephemeral=True)

        event_name = event_name.strip()
        if not event_name:
            await ctx.send("❌ Event name cannot be blank.", ephemeral=True)
            return

        if len(event_name) > 64:
            await ctx.send(
                f"❌ Event name is too long ({len(event_name)} chars). Maximum is 64 characters.",
                ephemeral=True,
            )
            return

        if catch_phrase and len(catch_phrase) > 128:
            await ctx.send(
                f"❌ Catch phrase is too long ({len(catch_phrase)} chars). Maximum is 128 characters.",
                ephemeral=True,
            )
            return

        if await Special.objects.filter(name=event_name).aexists():
            await ctx.send(
                f"❌ An event named **{event_name}** already exists. Choose a different name.",
                ephemeral=True,
            )
            return

        special = await Special.objects.acreate(
            name=event_name,
            rarity=0.1,
            catch_phrase=catch_phrase.strip() or None,
            tradeable=True,
            hidden=False,
        )

        specials_cache[special.id] = special
        log.info(f"evmake: '{event_name}' (id={special.id}) created by {ctx.author}")

        lines = [
            f"✅ **Event `{event_name}` created!** (ID: `{special.id}`)",
            f"• **Catch phrase:** {catch_phrase.strip() or '*(none)*'}",
            f"\nUse `/cardmaker` → event field to assign cards to **{event_name}**.",
        ]
        await ctx.send("\n".join(lines), ephemeral=True)

    @commands.hybrid_command(name="evremover")
    @app_commands.default_permissions()
    @app_commands.guilds(checks.ADMIN_GUILD_ID)
    @app_commands.check(checks.is_developer)
    @checks.is_superuser()
    @app_commands.describe(
        event_name="Name of the event to remove (autocomplete available)",
    )
    @app_commands.autocomplete(event_name=_event_name_autocomplete)
    async def evremover(
        self,
        ctx: commands.Context["CricStarBot"],
        event_name: str,
    ):
        """
        Remove an event by name. This deletes the event from the database and live cache.
        """
        await ctx.defer(ephemeral=True)

        event_name = event_name.strip()
        try:
            special = await Special.objects.aget(name=event_name)
        except Special.DoesNotExist:
            await ctx.send(
                f"❌ No event named **{event_name}** was found.", ephemeral=True
            )
            return

        special_id = special.id
        await special.adelete()
        specials_cache.pop(special_id, None)

        log.info(f"evremover: '{event_name}' (id={special_id}) removed by {ctx.author}")
        await ctx.send(
            f"✅ Event **{event_name}** (ID: `{special_id}`) has been removed.",
            ephemeral=True,
        )

    @commands.hybrid_command(name="removecard")
    @app_commands.default_permissions()
    @app_commands.guilds(checks.ADMIN_GUILD_ID)
    @app_commands.check(checks.is_developer)
    @commands.is_owner()
    @app_commands.describe(
        player_name="Name of the cricketer to permanently delete (autocomplete available)",
    )
    @app_commands.autocomplete(player_name=_player_name_autocomplete)
    async def removecard(
        self,
        ctx: commands.Context["CricStarBot"],
        player_name: str,
    ):
        """
        Permanently delete a cricketer card — removes the DB record, all player instances, and image files.
        ONLY the bot owner can run this. Action is irreversible.
        """
        await ctx.defer(ephemeral=True)

        # Look up by display name first, then by slug-based file as fallback
        ball: Ball | None = None
        try:
            ball = await Ball.objects.aget(country=player_name)
        except Ball.DoesNotExist:
            pass

        if ball is None:
            slug = re.sub(r"[^a-z0-9]+", "_", player_name.lower().strip()).strip("_")
            premade_slug_file = f"premade_{slug}.png"
            try:
                ball = await Ball.objects.aget(wild_card=premade_slug_file)
            except Ball.DoesNotExist:
                pass

        if ball is None:
            close = [
                b.country for b in balls_cache.values()
                if player_name.lower() in b.country.lower()
            ][:5]
            hint = f"\nDid you mean: {', '.join(close)}?" if close else ""
            await ctx.send(
                f"❌ No cricketer named **{player_name}** found.{hint}",
                ephemeral=True,
            )
            return

        # Count how many player instances will also be deleted
        instance_count = await BallInstance.objects.filter(ball=ball).acount()

        # Collect the image file paths before deleting
        media_dir = Path("admin_panel/media")
        files_to_delete: list[Path] = []
        for field_name in ("wild_card", "collection_card"):
            field_val = getattr(ball, field_name, None)
            if field_val:
                fname = field_val.name if hasattr(field_val, "name") else str(field_val)
                if fname:
                    p = media_dir / fname
                    if p.exists() and p not in files_to_delete:
                        files_to_delete.append(p)

        # Confirmation prompt
        confirm_view = ConfirmChoiceView(
            ctx,
            user=ctx.author,
            accept_message="Proceeding with deletion...",
            cancel_message="Deletion cancelled.",
        )
        warning = (
            f"⚠️ **Are you sure you want to delete `{ball.country}`?**\n\n"
            f"This will permanently remove:\n"
            f"• The card from the database\n"
            f"• **{instance_count}** player instance(s) that own this card\n"
            f"• {len(files_to_delete)} image file(s) from disk\n\n"
            f"**This cannot be undone.**"
        )
        await ctx.send(warning, view=confirm_view, ephemeral=True)
        await confirm_view.wait()

        if not confirm_view.value:
            await ctx.send("❌ Deletion cancelled.", ephemeral=True)
            return

        ball_name = ball.country
        ball_id = ball.id
        ball_slug = re.sub(r"[^a-z0-9]+", "_", ball_name.lower().strip()).strip("_")
        ball_filename = str(ball.collection_card) if ball.collection_card else f"premade_{ball_slug}.png"
        ball_wc_filename = str(ball.wild_card) if ball.wild_card else ball_filename

        # Delete all BallInstance records for this ball
        deleted_instances, _ = await BallInstance.objects.filter(ball=ball).adelete()

        # Delete the Ball record itself
        await ball.adelete()

        # Remove from in-memory cache
        balls_cache.pop(ball_id, None)

        # Delete image files from disk
        deleted_files: list[str] = []
        for p in files_to_delete:
            try:
                p.unlink()
                deleted_files.append(p.name)
            except Exception as exc:
                log.warning(f"removecard: could not delete file {p}: {exc}")

        # Clean up card_exports/ so the card won't be re-imported on next restart
        try:
            export_removed = _delete_card_export(
                player_name=ball_name,
                filename=ball_filename,
                wild_card_filename=ball_wc_filename if ball_wc_filename != ball_filename else None,
                slug=ball_slug,
            )
            if export_removed:
                deleted_files.extend(export_removed)
        except Exception as sync_exc:
            log.warning(f"removecard: card_sync cleanup failed (non-critical): {sync_exc}")

        files_summary = ", ".join(f"`{f}`" for f in deleted_files) if deleted_files else "none"
        log.info(
            f"removecard: '{ball_name}' (id={ball_id}) deleted by {ctx.author} — "
            f"{deleted_instances} instances removed, files: {files_summary}"
        )

        await ctx.send(
            f"✅ **{ball_name}** has been permanently deleted.\n"
            f"• `{deleted_instances}` player instance(s) removed\n"
            f"• Files deleted: {files_summary}",
            ephemeral=True,
        )

    @admin.command()
    @checks.is_superuser()
    async def impersonate(self, ctx: commands.Context["CricStarBot"], user: discord.Member | None = None):
        """
        Impersonate a user on your next slash commands.

        Run this command without parameters to clear impersonation.

        Parameters
        ----------
        user: discord.Member
            The user to impersonate
        """
        if user is None:
            if ctx.author.id not in impersonations:
                await ctx.send_help(ctx.command)
                return
            del impersonations[ctx.author.id]
            await ctx.send("You are not impersonating anymore.")
        else:
            impersonations[ctx.author.id] = user
            await ctx.send(
                f"Your next commands will be run as if {user.display_name} ran it.\n"
                "Avoid running the commands in a different server, this can lead to weird issues.\n"
                f"To clear impersonation, run `{ctx.prefix}admin impersonate` again.",
                ephemeral=True,
            )

    # ── 15 neon colours available for /admincolorset ──────────────────────────
    _NEON_COLOR_CHOICES = [
        app_commands.Choice(name="⚡ Electric Blue",   value="0064FF"),
        app_commands.Choice(name="💎 Neon Cyan",       value="00D2FF"),
        app_commands.Choice(name="☘️ Neon Green",       value="00FF64"),
        app_commands.Choice(name="⭐ Neon Yellow",      value="FFFF00"),
        app_commands.Choice(name="🔥 Neon Orange",     value="FF8C00"),
        app_commands.Choice(name="❤️ Neon Red",         value="FF0050"),
        app_commands.Choice(name="🌸 Hot Pink",         value="FF1493"),
        app_commands.Choice(name="🔮 Neon Purple",      value="B400FF"),
        app_commands.Choice(name="💜 Neon Magenta",     value="FF00C8"),
        app_commands.Choice(name="🌟 Electric Violet",  value="8A2BE2"),
        app_commands.Choice(name="🍀 Neon Lime",        value="96FF00"),
        app_commands.Choice(name="🌊 Neon Teal",        value="00FFC8"),
        app_commands.Choice(name="🏆 Gold",             value="FFC800"),
        app_commands.Choice(name="🌺 Neon Coral",       value="FF5050"),
        app_commands.Choice(name="❄️ Ice Blue",          value="64C8FF"),
    ]

    @commands.hybrid_command(name="admincolorset")
    @app_commands.default_permissions()
    @app_commands.guilds(checks.ADMIN_GUILD_ID)
    @app_commands.check(checks.is_developer)
    @checks.is_superuser()
    @app_commands.describe(
        player_name="Select the cricketer to set the neon glow for",
        neon_color="Choose one of 15 neon colours — applies as a glow around the card foreground",
    )
    @app_commands.autocomplete(player_name=_player_name_autocomplete)
    @app_commands.choices(neon_color=_NEON_COLOR_CHOICES)
    async def admincolorset(
        self,
        ctx: commands.Context["CricStarBot"],
        player_name: str,
        neon_color: app_commands.Choice[str],
    ):
        """
        Set a neon glow colour around a cricketer's card foreground.

        The glow is sized automatically to match the foreground frame and is
        applied the next time the card is regenerated via /editcard or /cardmaker.

        Parameters
        ----------
        player_name: str
            The cricketer's name (autocomplete from the database).
        neon_color: app_commands.Choice[str]
            One of 15 neon colours to apply as the foreground glow.
        """
        # Find the ball in the database
        ball = None
        for b in balls_cache.values():
            if b.country.lower() == player_name.lower():
                ball = b
                break

        if ball is None:
            await ctx.send(
                f"❌ No cricketer found with the name **{player_name}**. "
                "Use the autocomplete to pick a valid name.",
                ephemeral=True,
            )
            return

        # Parse hex value → RGB tuple
        hex_val = neon_color.value
        r = int(hex_val[0:2], 16)
        g = int(hex_val[2:4], 16)
        b_val = int(hex_val[4:6], 16)
        color = (r, g, b_val)

        # Persist to disk
        save_neon_color(ball.country, color)

        await ctx.send(
            f"✅ Neon colour **{neon_color.name}** set for **{ball.country}**.\n"
            f"Run `/editcard` on this player to bake the glow into their premade card.",
            ephemeral=True,
        )

    @commands.hybrid_command(name="addemoji")
    @app_commands.default_permissions()
    @app_commands.guilds(checks.ADMIN_GUILD_ID)
    @app_commands.check(checks.is_developer)
    @checks.is_superuser()
    @app_commands.describe(
        emoji="Paste the emoji directly (e.g. <:dhoni:123456>) or give a raw numeric ID / unicode char",
        emoji_pathname="The emoji's name/pathname (e.g. 'dhoni'). Used to display it correctly. Auto-detected if you paste the emoji directly.",
        player_name="Cricketer name to link this emoji to — autocomplete shows existing cards",
        show_in_list="Also show this emoji randomly in /list output (default True)",
        show_in_bet="Also show this emoji randomly in bet proposals (default True)",
    )
    @app_commands.autocomplete(player_name=_ball_name_autocomplete)
    async def addemoji(
        self,
        ctx: commands.Context["CricStarBot"],
        emoji: str,
        emoji_pathname: str = "",
        player_name: str = "",
        show_in_list: bool = True,
        show_in_bet: bool = True,
    ):
        """
        Register an emoji, optionally tied to a specific player.

        Parameters
        ----------
        emoji: str
            Paste the Discord emoji directly (<:name:id>), or provide a raw numeric ID or unicode char.
        emoji_pathname: str
            The emoji's name (e.g. dhoni). If you paste the emoji directly, this is detected automatically.
            This name is shown when users hover over or click the emoji.
        player_name: str
            Cricketer name (e.g. Ms Dhoni). Autocomplete lists existing cards.
            The emoji will show next to that player everywhere their card appears.
        show_in_list: bool
            If True, this emoji may also appear randomly in /list output. Default True.
        show_in_bet: bool
            If True, this emoji may also appear randomly next to cards in bet proposals. Default True.
        """
        parsed_name, emoji_id = parse_emoji_input(emoji)
        if not emoji_id:
            await ctx.send("❌ Please provide a valid emoji — paste it directly or give a numeric ID.", ephemeral=True)
            return

        # Validate: must be a digit ID or a short unicode emoji
        if not emoji_id.isdigit() and len(emoji_id) > 8:
            await ctx.send(
                "❌ Could not parse emoji. Paste the emoji directly (e.g. `<:dhoni:123456789>`) "
                "or provide just the numeric ID.",
                ephemeral=True,
            )
            return

        # Prefer the explicitly provided emoji_pathname, fall back to auto-detected name
        final_name = emoji_pathname.strip() if emoji_pathname.strip() else parsed_name

        add_emoji(
            emoji_id,
            show_in_list=show_in_list,
            show_in_bet=show_in_bet,
            player_name=player_name.strip(),
            emoji_name=final_name,
        )

        flags = []
        if show_in_list:
            flags.append("list")
        if show_in_bet:
            flags.append("bet")
        flags_str = " + ".join(flags) if flags else "none"

        name_for_display = final_name if final_name else "e"
        display = f"<:{name_for_display}:{emoji_id}>" if emoji_id.isdigit() else emoji_id
        player_line = f"\nLinked to player: **{player_name.strip()}**" if player_name.strip() else ""

        all_emojis = list_emojis()
        registry_lines_all = [
            f"• {format_emoji(e)} `{e['id']}`"
            + (f" [{e['name']}]" if e.get("name") else "")
            + (f" → **{e['player']}**" if e.get("player") else "")
            + f" — list: {'✅' if e.get('list') else '❌'} | bet: {'✅' if e.get('bet') else '❌'}"
            for e in all_emojis
        ]

        header = (
            f"✅ Emoji {display} (`{emoji_id}`) registered.{player_line}\n"
            f"Also shown in: **{flags_str}**\n\n"
            f"**Current emoji registry ({len(all_emojis)} total):**\n"
        )

        # Build registry text, truncating if it would exceed Discord's 2000 char limit
        max_body = 1990 - len(header)
        registry_text = ""
        for line in registry_lines_all:
            if len(registry_text) + len(line) + 1 > max_body:
                remaining = len(registry_lines_all) - registry_lines_all.index(line)
                registry_text += f"*… and {remaining} more*"
                break
            registry_text += line + "\n"

        await ctx.send(
            header + (registry_text.strip() or "*Empty*"),
            ephemeral=True,
        )

    @app_commands.command(name="removeemoji")
    @app_commands.default_permissions()
    @app_commands.guilds(checks.ADMIN_GUILD_ID)
    @app_commands.check(checks.is_developer)
    @checks.is_superuser()
    @app_commands.describe(
        id="The emoji ID (numbers) or unicode character to remove from the registry",
    )
    async def removeemoji(
        self,
        interaction: discord.Interaction["CricStarBot"],
        id: str,
    ):
        """Remove a registered emoji from the registry by its ID."""
        emoji_id = id.strip()
        if not emoji_id:
            await interaction.response.send_message("❌ Please provide a valid emoji ID or character.", ephemeral=True)
            return

        found = remove_emoji(emoji_id)
        if found:
            await interaction.response.send_message(
                f"✅ Emoji `{emoji_id}` removed from the registry.\n\n"
                f"**Remaining registry ({len(list_emojis())} total):**\n"
                + (
                    "\n".join(
                        f"• {format_emoji(e)} `{e['id']}`"
                        + (f" → **{e['player']}**" if e.get("player") else "")
                        for e in list_emojis()
                    ) or "*Empty*"
                ),
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"❌ Emoji `{emoji_id}` was not found in the registry.", ephemeral=True
            )

    @commands.hybrid_command(name="imageadd")
    @app_commands.default_permissions()
    @app_commands.guilds(checks.ADMIN_GUILD_ID)
    @app_commands.check(checks.is_developer)
    @checks.is_superuser()
    @app_commands.describe(
        url="Direct image URL to download (must start with http:// or https://)",
        pathname="Short name to reference this image later in /cardmaker and /editcard (e.g. blue_stadium)",
    )
    async def imageadd(
        self,
        ctx: commands.Context["CricStarBot"],
        url: str,
        pathname: str,
    ):
        """
        Save a background image from a URL so you can reuse it by name in /cardmaker and /editcard.

        Parameters
        ----------
        url: str
            Direct link to the image (jpg, png, webp supported).
        pathname: str
            Short name you'll type in the background field of /cardmaker (e.g. blue_stadium).
        """
        url = url.strip()
        pathname = pathname.strip().replace(" ", "_")

        if not url.startswith(("http://", "https://")):
            await ctx.send("❌ URL must start with `http://` or `https://`.", ephemeral=True)
            return
        if not pathname:
            await ctx.send("❌ Pathname cannot be empty.", ephemeral=True)
            return

        await ctx.defer(ephemeral=True)

        bg_dir = Path("admin_panel/media/backgrounds")
        bg_dir.mkdir(parents=True, exist_ok=True)

        # Detect extension from URL path
        from urllib.parse import urlparse
        url_path = urlparse(url).path.lower()
        ext = ""
        for candidate_ext in (".jpg", ".jpeg", ".png", ".webp"):
            if url_path.endswith(candidate_ext):
                ext = candidate_ext
                break
        if not ext:
            ext = ".jpg"  # default fallback

        save_path = bg_dir / f"{pathname}{ext}"

        def _download() -> bool:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "CricStar-Bot/1.0"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = resp.read(20 * 1024 * 1024)  # 20 MB limit
                with open(save_path, "wb") as f:
                    f.write(data)
                return True
            except Exception:
                return False

        with ThreadPoolExecutor() as pool:
            ok = await self.bot.loop.run_in_executor(pool, _download)

        if not ok:
            await ctx.send(
                f"❌ Failed to download image from the URL. Check the link and try again.",
                ephemeral=True,
            )
            return

        # List current presets for reference
        all_presets = _list_background_presets()
        presets_display = ", ".join(f"`{p}`" for p in all_presets) if all_presets else "*none yet*"

        await ctx.send(
            f"✅ Background image saved as **`{pathname}`**.\n\n"
            f"Use `{pathname}` in the `background` field of `/cardmaker` or `/editcard`.\n\n"
            f"**All saved backgrounds:** {presets_display}",
            ephemeral=True,
        )

    # ── /csdeletepath ─────────────────────────────────────────────────────────
    @app_commands.command(
        name="csdeletepath",
        description="[Owner] Delete a saved background image file.",
    )
    @app_commands.default_permissions()
    @app_commands.guilds(checks.ADMIN_GUILD_ID)
    @app_commands.check(checks.is_developer)
    @app_commands.describe(background="Background name to delete, or 'none' to cancel")
    @app_commands.autocomplete(background=_background_autocomplete)
    async def csdeletepath(
        self,
        interaction: discord.Interaction["CricStarBot"],
        background: str = "none",
    ):
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message(
                "❌ Only the bot owner can use this command.", ephemeral=True
            )
            return

        if background.strip().lower() in ("none", ""):
            await interaction.response.send_message(
                "Nothing to delete — you selected **none**.", ephemeral=True
            )
            return

        bg_dir = Path("admin_panel/media/backgrounds")
        # Match with or without extension
        target: Path | None = None
        for ext in (".png", ".jpg", ".jpeg", ".webp", ""):
            candidate = bg_dir / f"{background}{ext}"
            if candidate.exists():
                target = candidate
                break

        if target is None:
            all_presets = _list_background_presets()
            presets_display = ", ".join(f"`{p}`" for p in all_presets) or "*none*"
            await interaction.response.send_message(
                f"❌ No background named **`{background}`** was found.\n\n"
                f"**Available backgrounds:** {presets_display}",
                ephemeral=True,
            )
            return

        target.unlink()
        await interaction.response.send_message(
            f"✅ Background **`{target.name}`** has been deleted.",
            ephemeral=True,
        )

    # ── /cscooldown ───────────────────────────────────────────────────────────
    @app_commands.command(
        name="cscooldown",
        description="[Owner] Set the global spawn message cooldown (min/max messages between spawns).",
    )
    @app_commands.default_permissions()
    @app_commands.guilds(checks.ADMIN_GUILD_ID)
    @app_commands.check(checks.is_developer)
    @app_commands.describe(
        min_messages="Minimum messages needed before a cricketer can spawn.",
        max_messages="Maximum messages (threshold is random between min and max).",
    )
    async def cscooldown(
        self,
        interaction: discord.Interaction["CricStarBot"],
        min_messages: int,
        max_messages: int,
    ):
        if min_messages < 1:
            await interaction.response.send_message(
                "❌ Minimum must be at least 1.", ephemeral=True
            )
            return
        if max_messages <= min_messages:
            await interaction.response.send_message(
                "❌ Maximum must be greater than minimum.", ephemeral=True
            )
            return

        old_min = settings.spawn_chance_min
        old_max = settings.spawn_chance_max

        settings.spawn_chance_min = min_messages  # type: ignore
        settings.spawn_chance_max = max_messages  # type: ignore
        await settings.asave(update_fields=["spawn_chance_min", "spawn_chance_max"])

        await interaction.response.send_message(
            f"✅ Global spawn cooldown updated.\n"
            f"**Before:** {old_min}–{old_max} messages\n"
            f"**After:** {min_messages}–{max_messages} messages\n\n"
            f"-# New thresholds apply to every server on the next spawn cycle.",
            ephemeral=True,
        )
        log.info(
            f"Spawn cooldown changed from {old_min}-{old_max} to "
            f"{min_messages}-{max_messages} by {interaction.user}"
        )

    # ── /csresetcooldown ──────────────────────────────────────────────────────
    @app_commands.command(
        name="csresetcooldown",
        description="[Owner] Reset all daily, weekly, and upgrade cooldowns.",
    )
    @app_commands.default_permissions()
    @app_commands.guilds(checks.ADMIN_GUILD_ID)
    @app_commands.check(checks.is_developer)
    async def csresetcooldown(self, interaction: discord.Interaction["CricStarBot"]):
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message(
                "❌ Only the bot owner can use this command.", ephemeral=True
            )
            return

        # Reset daily + weekly claims
        from cricstar.packages.daily.cog import reset_all_cooldowns
        reset_all_cooldowns()

        # Reset upgrade global cooldown
        from cricstar.packages.upgrade.cog import reset_upgrade_cooldown
        reset_upgrade_cooldown()

        await interaction.response.send_message(
            "✅ All cooldowns reset!\n"
            "• **Daily** — everyone can claim again\n"
            "• **Weekly** — everyone can claim again\n"
            "• **Upgrade** — upgrade is available immediately",
            ephemeral=True,
        )

    # ── /AdminSync ────────────────────────────────────────────────────────────
    @app_commands.command(
        name="adminsync",
        description="[Owner] Force-sync all global slash commands.",
    )
    @app_commands.default_permissions()
    @app_commands.guilds(checks.ADMIN_GUILD_ID)
    @app_commands.check(checks.is_developer)
    async def adminsync(self, interaction: discord.Interaction["CricStarBot"]):
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message(
                "❌ Only the bot owner can use this command.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            synced_global = await self.bot.tree.sync()
            admin_guild = discord.Object(id=checks.ADMIN_GUILD_ID)
            synced_guild = await self.bot.tree.sync(guild=admin_guild)
            await interaction.followup.send(
                f"✅ Synced **{len(synced_global)} global commands** and "
                f"**{len(synced_guild)} admin guild commands** successfully.",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(
                f"❌ Sync failed: `{e}`", ephemeral=True
            )

    # ── /csrarity ─────────────────────────────────────────────────────────────
    async def _badge_rarity_autocomplete(
        self, interaction: discord.Interaction["CricStarBot"], current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete with distinct badge_rarity tiers from the DB."""
        seen: set[str] = set()
        choices: list[app_commands.Choice[str]] = []
        async for ball in Ball.objects.filter(enabled=True):
            raw = (ball.capacity_logic or {}).get("badge_rarity")
            if raw is None:
                continue
            try:
                val = float(raw)
            except (TypeError, ValueError):
                continue
            key = self._format_badge_rarity(val)
            if key in seen:
                continue
            if current and current not in key:
                continue
            seen.add(key)
            choices.append(app_commands.Choice(name=f"Rarity {key}", value=key))
            if len(choices) >= 25:
                break
        choices.sort(key=lambda c: float(c.value))
        return choices

    @staticmethod
    def _format_badge_rarity(value: float | int | str | None) -> str:
        if value is None:
            return "?"
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return "?"
        if numeric.is_integer():
            return f"{numeric:.1f}"
        return f"{numeric:g}"

    @app_commands.command(
        name="csrarity",
        description="View rarity tiers for all cricketer cards.",
    )
    @app_commands.describe(
        badge_rarity="Rarity tier to inspect (e.g. 0.2, 1.0). Leave blank for the full list.",
    )
    @app_commands.autocomplete(badge_rarity=_badge_rarity_autocomplete)
    async def csrarity(
        self,
        interaction: discord.Interaction["CricStarBot"],
        badge_rarity: str = "",
    ):
        await interaction.response.defer(ephemeral=True)

        def _get_badge(b: Ball) -> float | None:
            raw = (b.capacity_logic or {}).get("badge_rarity")
            if raw is None:
                return None
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None

        all_balls: list[Ball] = [b async for b in Ball.objects.filter(enabled=True)]

        if not badge_rarity.strip():
            all_balls.sort(key=lambda b: (_get_badge(b) if _get_badge(b) is not None else 9999, b.country.lower()))
            lines: list[str] = []
            for ball in all_balls:
                br = self._format_badge_rarity(_get_badge(ball))
                suffix = " (Unobtainable)" if getattr(ball, "unobtainable", False) else ""
                lines.append(f"{br} - {ball.country}{suffix}")
            text = "\n".join(lines) or "No cards found."
            view = discord.ui.LayoutView()
            text_display = discord.ui.TextDisplay("")
            view.add_item(text_display)
            menu = Menu(
                self.bot, view,
                TextSource(text, prefix="```\n", suffix="```"),
                TextFormatter(text_display),
            )
            await menu.init()
            await interaction.followup.send(view=view, ephemeral=True)
            return

        try:
            target_br = float(badge_rarity.strip())
        except ValueError:
            await interaction.followup.send(
                f"❌ Invalid badge rarity `{badge_rarity}` — enter a number like `0.2` or `5.0`.",
                ephemeral=True,
            )
            return

        tier_balls = [b for b in all_balls if _get_badge(b) == target_br]

        if not tier_balls:
            await interaction.followup.send(
                f"❌ No cards found with badge rarity `{self._format_badge_rarity(target_br)}`.",
                ephemeral=True,
            )
            return

        lines = [f"Cards with rarity {self._format_badge_rarity(target_br)}:\n"]
        for ball in sorted(tier_balls, key=lambda b: b.country.lower()):
            status = "true" if getattr(ball, "unobtainable", False) else "false"
            lines.append(f"{self._format_badge_rarity(target_br)} - {ball.country} | unobtainable: {status}")
        await interaction.followup.send("```" + "\n".join(lines) + "```", ephemeral=True)

    @app_commands.command(
        name="csspawnchance",
        description="[Admin] View or change one player's spawn chance.",
    )
    @app_commands.default_permissions()
    @app_commands.guilds(checks.ADMIN_GUILD_ID)
    @app_commands.check(checks.is_developer)
    @app_commands.describe(
        player_name="Player/card to inspect or update.",
        action="What to do with this player's spawn chance.",
        amount="Amount to set, add, or subtract on the 0–1000 scale. Not needed for view/disable.",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="View current spawn chance", value="view"),
        app_commands.Choice(name="Set to amount", value="set"),
        app_commands.Choice(name="Increase by amount", value="increase"),
        app_commands.Choice(name="Decrease by amount", value="decrease"),
        app_commands.Choice(name="Disable spawning", value="disable"),
    ])
    @app_commands.autocomplete(player_name=_player_name_autocomplete)
    async def csspawnchance(
        self,
        interaction: discord.Interaction["CricStarBot"],
        player_name: str,
        action: str = "view",
        amount: float = 0.0,
    ):
        await interaction.response.defer(ephemeral=True)

        try:
            ball = await Ball.objects.aget(country=player_name)
        except Ball.DoesNotExist:
            close = [
                b.country for b in balls_cache.values()
                if player_name.lower() in b.country.lower()
            ][:5]
            hint = f"\nDid you mean: {', '.join(close)}?" if close else ""
            await interaction.followup.send(
                f"❌ No cricketer named **{player_name}** found.{hint}",
                ephemeral=True,
            )
            return

        action = action.lower().strip()
        old_chance = float(ball.rarity or 0)

        if action not in {"view", "set", "increase", "decrease", "disable"}:
            await interaction.followup.send(
                "❌ Invalid action. Use view, set, increase, decrease, or disable.",
                ephemeral=True,
            )
            return

        if action in {"set", "increase", "decrease"} and amount < 0:
            await interaction.followup.send("❌ Amount cannot be negative.", ephemeral=True)
            return

        if action == "view":
            new_chance = old_chance
        elif action == "set":
            new_chance = amount
        elif action == "increase":
            new_chance = old_chance + amount
        elif action == "decrease":
            new_chance = max(0.0, old_chance - amount)
        else:
            new_chance = 0.0

        if new_chance > 1000:
            await interaction.followup.send(
                "❌ Spawn chance cannot be higher than `1000`.",
                ephemeral=True,
            )
            return

        changed = action != "view" and new_chance != old_chance
        if action != "view":
            ball.rarity = new_chance
            ball.spawnable = new_chance > 0
            await ball.asave(update_fields=["rarity", "spawnable"])
            if ball.pk in balls_cache:
                balls_cache[ball.pk].rarity = new_chance
                balls_cache[ball.pk].spawnable = new_chance > 0

            import json as _json
            from pathlib import Path as _Path
            cards_path = _Path("card_exports/cards.json")
            if cards_path.exists():
                try:
                    records: dict = _json.loads(cards_path.read_text())
                    data = records.get(ball.country)
                    if data is not None:
                        data["spawn_chance"] = new_chance
                        data["spawnable"] = new_chance > 0
                        cards_path.write_text(_json.dumps(records, indent=2, ensure_ascii=False))
                except Exception as exc:
                    log.warning("csspawnchance: could not update cards.json: %s", exc)

        all_spawnable = [b async for b in Ball.objects.filter(enabled=True, spawnable=True, rarity__gt=0)]
        total_weight = sum(float(b.rarity or 0) for b in all_spawnable)
        current_percent = (new_chance / total_weight * 100) if total_weight and new_chance > 0 else 0.0
        badge_rarity = (ball.capacity_logic or {}).get("badge_rarity", "?")
        status = "disabled" if new_chance <= 0 or not ball.spawnable else "enabled"

        if action == "view":
            title = f"ℹ️ **{ball.country}** spawn chance"
        elif changed:
            title = f"✅ Updated **{ball.country}** spawn chance"
        else:
            title = f"ℹ️ **{ball.country}** spawn chance unchanged"

        await interaction.followup.send(
            f"{title}\n"
            f"Badge rarity: `{badge_rarity}`\n"
            f"Before: `{old_chance:.4g}`\n"
            f"Now: `{new_chance:.4g}`\n"
            f"Status: `{status}`\n"
            f"Total spawn pool weight: `{total_weight:.4g}`\n"
            f"Current random-spawn share: `{current_percent:.2f}%`",
            ephemeral=True,
        )

    # ── /cscleanup ────────────────────────────────────────────────────────────
    @app_commands.command(
        name="cscleanup",
        description="[Owner] Delete all card instances obtained via trade/bet older than 15 days.",
    )
    @app_commands.default_permissions()
    @app_commands.guilds(checks.ADMIN_GUILD_ID)
    @app_commands.check(checks.is_developer)
    async def cscleanup(self, interaction: discord.Interaction["CricStarBot"]):
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message(
                "❌ Only the bot owner can use this command.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=15)

        # Find BallInstance IDs whose most recent trade is older than 15 days
        old_traded_ids = [
            row async for row in (
                TradeObject.objects
                .values("ballinstance_id")
                .annotate(latest_trade=Max("trade__date"))
                .filter(latest_trade__lt=cutoff)
                .values_list("ballinstance_id", flat=True)
            )
        ]

        if not old_traded_ids:
            await interaction.followup.send(
                "✅ Nothing to clean up — no traded cards older than 15 days found.",
                ephemeral=True,
            )
            return

        # Soft-delete: only instances that were actually transferred (trade_player set)
        deleted_count = await (
            BallInstance.objects
            .filter(id__in=old_traded_ids, trade_player__isnull=False, deleted=False)
            .aupdate(deleted=True)
        )

        await interaction.followup.send(
            f"✅ Cleanup complete!\n"
            f"• **{deleted_count}** card instance(s) from trades older than **15 days** have been removed.\n"
            f"-# Original cards caught by players are unaffected.",
            ephemeral=True,
        )

    # ── /csmultispawn ──────────────────────────────────────────────────────────
    @app_commands.command(
        name="csmultispawn",
        description="[Owner] Select up to 20 cricketers from a menu and spawn them all at once.",
    )
    @app_commands.default_permissions()
    @app_commands.guilds(checks.ADMIN_GUILD_ID)
    @app_commands.check(checks.is_developer)
    async def csmultispawn(self, interaction: discord.Interaction["CricStarBot"]):
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message(
                "❌ Only the bot owner can use this command.", ephemeral=True
            )
            return

        assert interaction.guild

        config = await GuildConfig.objects.aget_or_none(guild_id=interaction.guild_id)
        if not config or not config.spawn_channel:
            await interaction.response.send_message(
                "❌ No spawn channel configured for this server. Use `/config channel` first.",
                ephemeral=True,
            )
            return

        channel = interaction.guild.get_channel(config.spawn_channel)
        if not channel or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "❌ The configured spawn channel could not be found.", ephemeral=True
            )
            return

        all_balls = [
            b async for b in Ball.objects.filter(enabled=True, spawnable=True, rarity__gt=0).order_by("country")
        ]
        if not all_balls:
            await interaction.response.send_message(
                "❌ No spawnable cricketers found in the database.", ephemeral=True
            )
            return

        options = [
            discord.SelectOption(label=b.country[:100], value=str(b.pk))
            for b in all_balls[:25]
        ]

        view = MultiSpawnView(options, channel, self.bot)
        total = len(all_balls)
        note = f" (showing first 25 of {total})" if total > 25 else ""
        await interaction.response.send_message(
            f"## 🏏 Admin Multi-Spawn\n"
            f"Select up to **20 cricketers** to spawn in {channel.mention}.{note}\n"
            f"-# They will appear one by one as catchable cards.",
            view=view,
            ephemeral=True,
        )