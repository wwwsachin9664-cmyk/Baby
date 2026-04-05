import logging
import os
import re
import tempfile
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, cast

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import ActionRow, Button, Container, Section, TextDisplay

from cricstar.core.bot import impersonations
from cricstar.core.discord import LayoutView
from cricstar.core.image_generator.image_gen import draw_premade_card
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
from bd_models.models import Ball, BallInstance, GuildConfig, Regime, Special
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

    async def cog_load(self):
        guilds = [
            discord.Object(guild_id)
            async for guild_id in GuildConfig.objects.filter(admin_command_synced=True).values_list(
                "guild_id", flat=True
            )
        ]
        self.bot.tree.add_command(self.admin.app_command, guilds=guilds)

    @commands.hybrid_group()
    @app_commands.guilds(0)
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
    @checks.has_permissions("bd_models.view_ball")
    async def rarity(self, ctx: commands.Context["CricStarBot"], *, flags: RarityFlags):
        """
        Generate a list of cricketers ranked by rarity.
        """
        text = ""
        balls_queryset = Ball.objects.all().order_by("rarity")
        if not flags.include_disabled:
            balls_queryset = balls_queryset.filter(rarity__gt=0, enabled=True)
        sorted_balls = [x async for x in balls_queryset]

        if flags.chunked:
            indexes: dict[float, list[Ball]] = defaultdict(list)
            for ball in sorted_balls:
                indexes[ball.rarity].append(ball)
            i = 1
            for chunk in indexes.values():
                for ball in chunk:
                    text += f"{i}. {ball.country}\n"
                i += len(chunk)
        else:
            for i, ball in enumerate(sorted_balls, start=1):
                text += f"{i}. {ball.country}\n"

        view = discord.ui.LayoutView()
        text_display = discord.ui.TextDisplay("")
        view.add_item(text_display)
        menu = Menu(self.bot, view, TextSource(text, prefix="```md\n", suffix="```"), TextFormatter(text_display))
        await menu.init()
        await ctx.send(view=view)

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
    @checks.is_superuser()
    @app_commands.describe(
        player_name="Unique identifier name of the cricketer (used for DB/file lookup)",
        display_name="Name shown on the card header — if blank, player_name is used",
        codename="Codename shown on the card (e.g. KING KOHLI)",
        description="Description text shown on the card (max 256 characters)",
        bat_score="Bat / health score shown on left (e.g. 342)",
        ball_score="Ball / attack score shown on right (e.g. 327)",
        rarity="Value shown on card badge (e.g. 50.0) — cosmetic only",
        spawn_chance="Spawn probability 0–100% — set to 0 to disable random spawning",
        artwork_author="Name of the artwork creator",
        background="Preset name (e.g. base_background) or image URL",
        foreground="Player image URL or preset name (saved by player name after first use)",
        logo_url="Optional team/event logo URL shown on the card",
        event="Assign card to a special event (always spawns with it)",
        tradeable="Whether this card can be traded (default True)",
        catch_name="Name(s) players must type to catch this card. Separate multiples with semicolons (e.g. virat;vk;king)",
    )
    @app_commands.autocomplete(event=_event_autocomplete)
    async def cardmaker(
        self,
        ctx: commands.Context["CricStarBot"],
        player_name: str,
        codename: str,
        description: str,
        bat_score: int,
        ball_score: int,
        rarity: float,
        spawn_chance: app_commands.Range[int, 0, 100],
        artwork_author: str,
        background: str,
        foreground: str,
        logo_url: str = "",
        event: str = "none",
        tradeable: bool = True,
        display_name: str = "",
        catch_name: str = "",
    ):
        """
        Generate a Dembele-style cricket card and add it to the database.
        rarity: badge display value (any number, e.g. 50.0).
        spawn_chance: 0-100 percent chance to spawn. Set to 0 to disable spawning.
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
                req = urllib.request.Request(
                    name_or_url,
                    headers={"User-Agent": "CricStar-Bot/1.0"},
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = resp.read(max_bytes)
                with open(dest, "wb") as f:
                    f.write(data)
                return True
            except Exception:
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
                try:
                    req = urllib.request.Request(
                        logo_url.strip(),
                        headers={"User-Agent": "CricStar-Bot/1.0"},
                    )
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        data = resp.read(2 * 1024 * 1024)  # max 2 MB
                    with open(logo_path, "wb") as f:
                        f.write(data)
                except Exception:
                    logo_path = None

            def _generate() -> tuple:
                return draw_premade_card(
                    bg_path, fg_path, card_name, codename, description,
                    rarity, bat_score, ball_score, artwork_author, logo_path,
                )

            with ThreadPoolExecutor() as pool:
                image, img_kwargs = await self.bot.loop.run_in_executor(pool, _generate)

            card_path = media_dir / filename
            image.save(str(card_path), **img_kwargs)
            image.close()

            ball = await Ball.objects.acreate(
                country=card_name,
                health=bat_score,
                attack=ball_score,
                rarity=spawn_chance / 100,
                emoji_id=0,
                wild_card=filename,
                collection_card=filename,
                credits=artwork_author,
                capacity_name=codename,
                capacity_description=description,
                capacity_logic={},
                regime=regime,
                tradeable=tradeable,
                spawnable=(spawn_chance > 0),
                catch_names=catch_name.strip().lower() or None,
            )

            event_text = ""
            if event != "none":
                try:
                    special = await Special.objects.aget(name=event)
                    ball.capacity_logic = {"forced_special": special.id}
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

            preview_file = discord.File(str(card_path), filename=filename)
            try:
                await ctx.send(
                    f"✅ **{player_name}** card created!{event_text}\n"
                    f"`{filename}` | Badge Rarity: `{rarity}` | Spawn Chance: `{spawn_chance}%` | Tradeable: `{tradeable}` | Spawnable: `{spawnable}`\n"
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
    @checks.is_superuser()
    @app_commands.describe(
        player_name="Exact name of the cricketer to edit (use the name stored in DB)",
        display_name="Change the name shown on the card header (leave blank to keep existing)",
        background="Preset name or URL (leave blank to keep / use base_background)",
        foreground="Player image URL or preset name (leave blank to use saved preset)",
        codename="New codename shown on card (leave blank to keep existing)",
        description="New description text, max 256 characters (leave blank to keep existing)",
        bat_score="New bat / health score (leave blank to keep existing)",
        ball_score="New ball / attack score (leave blank to keep existing)",
        rarity="New badge display value — cosmetic only (leave blank to keep existing)",
        spawn_chance="New spawn probability 0–100% — set to 0 to disable spawning",
        artwork_author="New artwork author name (leave blank to keep existing)",
        logo_url="New team/event logo URL (leave blank to keep existing)",
        tradeable="Change tradeability (leave blank to keep existing)",
        catch_name="Name(s) players must type to catch this card. Separate multiples with semicolons (e.g. virat;vk;king)",
    )
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
        spawn_chance: app_commands.Range[int, 0, 100] | None = None,
        artwork_author: str = "",
        logo_url: str = "",
        tradeable: bool | None = None,
        catch_name: str = "",
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
        want_image = bool(background.strip() or foreground.strip())
        # Also regenerate if the card is already a premade card (keeps it up-to-date)
        wild_card_name = ball.wild_card.name if ball.wild_card else ""
        is_premade = wild_card_name.startswith("premade_")
        regen = want_image or is_premade

        bg_source = background.strip() or "base_background"
        fg_source = foreground.strip() or slug

        filename = f"premade_{slug}.png"
        changed_fields: list[str] = []

        def _fetch_image(name_or_url: str, dest: str, max_bytes: int = 10 * 1024 * 1024) -> bool:
            import shutil
            name_or_url = name_or_url.strip()
            if not name_or_url.startswith(("http://", "https://")):
                for search_dir in (backgrounds_dir, foregrounds_dir):
                    for ext in (".jpg", ".jpeg", ".png", ".webp", ""):
                        candidate = search_dir / f"{name_or_url}{ext}"
                        if candidate.exists():
                            shutil.copy2(str(candidate), dest)
                            return True
                return False
            try:
                req = urllib.request.Request(
                    name_or_url, headers={"User-Agent": "CricStar-Bot/1.0"}
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = resp.read(max_bytes)
                with open(dest, "wb") as f:
                    f.write(data)
                return True
            except Exception:
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
                            _fetch_image(bg_source, bg_path),
                            _fetch_image(fg_source, fg_path),
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

                # Save/update foreground preset for this player
                fg_preset = foregrounds_dir / slug
                import shutil as _shutil
                _shutil.copy2(fg_path, str(fg_preset))

                # Resolve display values (use new value or fall back to existing DB value)
                _card_name = display_name.strip() or ball.country
                _codename = codename.strip() or ball.capacity_name or ""
                _description = description.strip() or ball.capacity_description or ""
                _rarity = rarity if rarity is not None else ball.rarity * 100
                _bat = bat_score if bat_score is not None else ball.health
                _ball = ball_score if ball_score is not None else ball.attack
                _author = artwork_author.strip() or ball.credits or ""

                # Optional logo
                logo_url_clean = logo_url.strip()
                if logo_url_clean:
                    logo_path = os.path.join(tmpdir, "logo.png")
                    try:
                        req = urllib.request.Request(
                            logo_url_clean, headers={"User-Agent": "CricStar-Bot/1.0"}
                        )
                        with urllib.request.urlopen(req, timeout=10) as resp:
                            data = resp.read(2 * 1024 * 1024)
                        with open(logo_path, "wb") as f:
                            f.write(data)
                    except Exception:
                        logo_path = None

                def _generate() -> tuple:
                    return draw_premade_card(
                        bg_path, fg_path, _card_name, _codename, _description,
                        _rarity, _bat, _ball, _author, logo_path,
                    )

                with ThreadPoolExecutor() as pool:
                    image, img_kwargs = await self.bot.loop.run_in_executor(pool, _generate)

                card_path = media_dir / filename
                image.save(str(card_path), **img_kwargs)
                image.close()

                ball.wild_card = filename
                ball.collection_card = filename
                changed_fields += ["wild_card", "collection_card"]

        # --- Apply text / stat field changes ---
        if display_name.strip():
            ball.country = display_name.strip()
            changed_fields.append("country")
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
            ball.rarity = spawn_chance / 100
            changed_fields.append("rarity")
        if artwork_author.strip():
            ball.credits = artwork_author.strip()
            changed_fields.append("credits")
        if tradeable is not None:
            ball.tradeable = tradeable
            changed_fields.append("tradeable")
        if spawn_chance is not None:
            ball.spawnable = (spawn_chance > 0)
            changed_fields.append("spawnable")
        if catch_name.strip():
            ball.catch_names = catch_name.strip().lower()
            changed_fields.append("catch_names")

        if not changed_fields:
            await ctx.send(
                "⚠️ Nothing to change — you didn't supply any new values.",
                ephemeral=True,
            )
            return

        await ball.asave(update_fields=list(set(changed_fields)))
        balls_cache[ball.id] = ball

        summary_parts = []
        if regen:
            summary_parts.append("Card image regenerated")
        if display_name.strip():
            summary_parts.append(f"Display name → `{ball.country}`")
        if codename.strip():
            summary_parts.append(f"Codename → `{ball.capacity_name}`")
        if description.strip():
            summary_parts.append(f"Description updated")
        if bat_score is not None:
            summary_parts.append(f"BAT → `{ball.health}`")
        if ball_score is not None:
            summary_parts.append(f"BALL → `{ball.attack}`")
        if spawn_chance is not None:
            spawn_label = f"Spawn chance → `{spawn_chance}%`" + (" *(spawning disabled)*" if spawn_chance == 0 else "")
            summary_parts.append(spawn_label)
        if artwork_author.strip():
            summary_parts.append(f"Author → `{ball.credits}`")
        if tradeable is not None:
            summary_parts.append(f"Tradeable → `{ball.tradeable}`")

        summary = " | ".join(summary_parts)
        card_path = media_dir / filename

        if regen and card_path.exists():
            preview_file = discord.File(str(card_path), filename=filename)
            try:
                await ctx.send(
                    f"✅ **{player_name}** updated!\n{summary}",
                    file=preview_file,
                )
            except Exception as send_err:
                log.error(f"editcard: preview send failed: {send_err}")
                await ctx.send(f"✅ **{player_name}** updated!\n{summary}")
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

        extra = f"\n• Preset saved as `{saved_preset}` — reuse with `path_name:{path_name}`" if saved_preset else ""
        preview_file = discord.File(dest_path, filename=filename)
        try:
            await ctx.send(
                f"✅ Spawn image updated for **{ball.country}** (from {source_label})!\n"
                f"• Assigned file: `{filename}`{extra}",
                file=preview_file,
            )
        except Exception as send_err:
            log.error(f"setspawnimg: preview send failed: {send_err}")
            await ctx.send(
                f"✅ Spawn image updated for **{ball.country}**! Saved as `{filename}`."
            )

    @commands.hybrid_command(name="createevent")
    @checks.is_superuser()
    @app_commands.describe(
        name="Event name (e.g. IPL 2026, T20 World Cup) — must be unique",
        rarity="Spawn weight 0.0–1.0 — how often this event's background appears (e.g. 0.05 = 5%)",
        emoji="An emoji or Discord emoji ID shown next to the event name",
        catch_phrase="Message shown when catching a card with this event (max 128 chars)",
        start_date="When the event starts (YYYY-MM-DD or YYYY-MM-DD HH:MM). Leave blank to start immediately",
        end_date="When the event ends (YYYY-MM-DD or YYYY-MM-DD HH:MM). Leave blank to never expire",
        tradeable="Whether cards with this event can be traded (default True)",
        hidden="If True, hides this event from user-facing commands like /cricketers list",
        credits="Author/credit for this event's artwork",
    )
    async def createevent(
        self,
        ctx: commands.Context["CricStarBot"],
        name: str,
        rarity: app_commands.Range[float, 0.0, 1.0],
        emoji: str = "",
        catch_phrase: str = "",
        start_date: str = "",
        end_date: str = "",
        tradeable: bool = True,
        hidden: bool = False,
        credits: str = "",
    ):
        """
        Create a new special event. Events appear as coloured backgrounds on cards when they spawn.
        rarity: 0.0–1.0 weight (0.05 = 5% chance the event background activates during a spawn).
        Dates are optional — leave blank to start immediately / never expire.
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
            rarity=rarity,
            emoji=emoji.strip() or None,
            catch_phrase=catch_phrase.strip() or None,
            start_date=parsed_start,
            end_date=parsed_end,
            tradeable=tradeable,
            hidden=hidden,
            credits=credits.strip() or None,
        )

        # Add to live cache so it's usable immediately without restart
        specials_cache[special.id] = special

        log.info(f"createevent: '{name}' (id={special.id}) created by {ctx.author}")

        # Build summary
        start_label = parsed_start.strftime("%Y-%m-%d %H:%M") if parsed_start else "immediately"
        end_label = parsed_end.strftime("%Y-%m-%d %H:%M") if parsed_end else "never (permanent)"
        status_label = "Hidden" if hidden else "Visible"

        summary_lines = [
            f"✅ **Event `{name}` created!** (ID: `{special.id}`)\n",
            f"• **Rarity:** `{rarity}` ({rarity * 100:.1f}% spawn weight)",
            f"• **Emoji:** {emoji.strip() or '*(none)*'}",
            f"• **Catch phrase:** {catch_phrase.strip() or '*(none)*'}",
            f"• **Starts:** {start_label}",
            f"• **Ends:** {end_label}",
            f"• **Tradeable:** {tradeable}",
            f"• **Status:** {status_label}",
        ]
        if credits.strip():
            summary_lines.append(f"• **Credits:** {credits.strip()}")

        summary_lines.append(
            f"\nThe event is now **live** in the bot cache. "
            f"Use `/cardmaker` → event field to assign cards to **{name}**."
        )

        await ctx.send("\n".join(summary_lines), ephemeral=True)

    @commands.hybrid_command(name="removecard")
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
