"""/request: ask Seerr for a film or show, and get a DM when it's ready.

The answer is ephemeral: poster, overview, and whether the title is in the
library, requested, or can be requested, with a button to ask for it (for a
show, all missing seasons or the latest one). Requests are made as the caller's
Seerr user when /link names one, else with the API key's account.

Every five minutes the bot asks Seerr about the requests made from Discord that
are still waiting (see media_requests.py) and DMs whoever asked once one can be
watched. Anyone whose DMs are closed is mentioned in the activity channel.
"""
import asyncio
import io
from datetime import datetime, timezone

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import media_requests as mr
import watch_data
from cogs.movie_night import details_link
from seerr_client import SeerrClient, SeerrError

COLOUR = discord.Colour(0x6366F1)      # Seerr indigo
READY_COLOUR = discord.Colour(0x00A4DC)  # Jellyfin blue
POLL_MINUTES = 5
VIEW_TIMEOUT = 600                       # seconds the buttons stay live


# --- formatting --------------------------------------------------------------

def runtime(minutes: int | None) -> str:
    if not minutes:
        return "—"
    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def jellyfin_link(details_or_media: dict) -> str | None:
    """The Jellyfin page, once Seerr knows the item id. Never an image."""
    media = details_or_media.get("mediaInfo") or details_or_media
    item_id = media.get("jellyfinMediaId")
    return details_link(item_id) if item_id else None


# Requests go in as the member's own Seerr user, so they follow that user's approval
# rules. Made as the API key's user (the admin) they would skip approval.
NOT_LINKED = ("To request it, link your Jellyfin user first: the 👤 button in #roles or /link. "
              "Linked already? Sign in to Seerr once so it knows you.")


def request_card(media_type: str, details: dict, seerr_name: str | None) -> discord.Embed:
    """The answer to /request. seerr_name is whom a request would be made as."""
    is_tv = media_type == "tv"
    overview = (details.get("overview") or "").strip()
    embed = discord.Embed(
        title=f"{'📺' if is_tv else '🎬'} {mr.label(details)}"[:256],
        url=jellyfin_link(details),
        description=(overview[:400] + ("…" if len(overview) > 400 else "")) or None,
        colour=COLOUR,
    )
    embed.add_field(name="Status", value=mr.status_line(media_type, details) or "—", inline=False)
    if is_tv:
        seasons = mr.tv_seasons(details)
        show = f"{len(seasons)} season{'s' if len(seasons) != 1 else ''}" if seasons else "—"
        if details.get("status"):
            show += f" · {details['status'].lower()}"
        embed.add_field(name="Show", value=show, inline=True)
    else:
        embed.add_field(name="Runtime", value=runtime(details.get("runtime")), inline=True)
    genres = ", ".join(g.get("name", "") for g in details.get("genres") or [])
    embed.add_field(name="Genre", value=(genres or "—")[:100], inline=True)
    if mr.can_request(media_type, details):
        embed.set_footer(text=f"Requests are made as your Seerr user, {seerr_name}." if seerr_name else NOT_LINKED)
    return embed


def ready_embed(row: dict, request: dict) -> discord.Embed:
    """The DM once a request can be watched."""
    link = jellyfin_link(request.get("media") or {})
    if row["media_type"] == "tv" and row["seasons"]:
        verb = "is" if len(row["seasons"]) == 1 else "are"
        line = f"{mr.seasons_phrase(row['seasons']).capitalize()}, which you asked for, {verb} on Jellyfin."
    else:
        line = "The film you asked for is on Jellyfin." if row["media_type"] == "movie" else \
            "The show you asked for is on Jellyfin."
    embed = discord.Embed(
        title=f"🍿 {row['title']} is ready"[:256],
        url=link,
        description=line + (f"\n\n[Open it in Jellyfin]({link})" if link else ""),
        colour=READY_COLOUR,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="You asked for it with /request")
    return embed


def open_view(link: str | None) -> discord.ui.View | None:
    if not link:
        return None
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Open in Jellyfin", url=link, emoji="▶️"))
    return view


# --- the buttons -------------------------------------------------------------

class RequestView(discord.ui.View):
    """Request buttons under the card, and "DM me" for something already asked for."""

    def __init__(self, cog: "Requests", owner_id: int, media_type: str, details: dict,
                 seerr_id: int | None, seerr_name: str | None):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.cog, self.owner_id, self.media_type, self.details = cog, owner_id, media_type, details
        self.seerr_id, self.seerr_name = seerr_id, seerr_name

        if seerr_id is not None and mr.can_request(media_type, details):
            choices = mr.season_choices(details) if media_type == "tv" else [("Request", None)]
            for text, seasons in choices:
                self.add_button(text, "➕", discord.ButtonStyle.success, self.requester(seasons))
        if mr.followable_requests(details):
            self.add_button("DM me when it's ready", "🔔", discord.ButtonStyle.secondary, self.follow)
        if link := jellyfin_link(details):
            self.add_item(discord.ui.Button(label="Open in Jellyfin", url=link, emoji="▶️"))

    def add_button(self, text, emoji, style, callback):
        button = discord.ui.Button(label=text[:80], emoji=emoji, style=style)
        button.callback = callback
        self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    async def freeze(self, interaction: discord.Interaction):
        """Grey the buttons out first, so a second click does nothing."""
        for item in self.children:
            if isinstance(item, discord.ui.Button) and not item.url:
                item.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    def requester(self, seasons: list[int] | None):
        async def callback(interaction: discord.Interaction):
            await self.freeze(interaction)
            text = await self.cog.place_request(interaction.user, self.media_type, self.details, seasons,
                                                self.seerr_id, self.seerr_name)
            await interaction.followup.send(text, ephemeral=True)
        return callback

    async def follow(self, interaction: discord.Interaction):
        await self.freeze(interaction)
        await interaction.followup.send(await self.cog.follow(interaction.user, self.media_type, self.details),
                                        ephemeral=True)


# --- the cog -----------------------------------------------------------------

class Requests(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.seerr = SeerrClient(config.SEERR_URL, config.SEERR_API_KEY) \
            if config.SEERR_URL and config.SEERR_API_KEY else None

    def cog_unload(self):
        self.check_requests.cancel()

    async def aclose(self):
        if self.seerr is not None:
            await self.seerr.close()

    @commands.Cog.listener()
    async def on_services_ready(self):
        if self.seerr and not self.check_requests.is_running():
            self.check_requests.start()

    # --- making requests -----------------------------------------------------

    async def seerr_user(self, discord_user_id: int) -> tuple[int | None, str | None]:
        """(Seerr user id, name) for a member linked with /link, or (None, None)."""
        name = await watch_data.get_link(discord_user_id)
        if not name:
            return None, None
        try:
            users = await self.seerr.users()
        except Exception as e:
            print(f"Could not list Seerr users: {e}")
            return None, None
        user_id = mr.seerr_user_id(users, name)
        return (user_id, name) if user_id else (None, None)

    async def place_request(self, user, media_type: str, details: dict, seasons: list[int] | None,
                            seerr_id: int | None, seerr_name: str | None) -> str:
        title = mr.label(details)
        if seerr_id is None:
            return NOT_LINKED
        try:
            request = await self.seerr.create_request(media_type, details["id"], seasons, seerr_id)
        except SeerrError as e:
            return mr.refusal(e.status, e.message)
        except Exception as e:
            return f"❌ Couldn't reach Seerr: {e}"
        # Seerr has no note field, so whoever asked is recorded here and in the log
        print(f"/request: {user} ({user.id}) asked for {title} {seasons or ''} "
              f"as {seerr_name or 'the API key user'}: request {request.get('id')}")
        try:
            async with aiosqlite.connect(config.DB_PATH) as db:
                await mr.track(db, request["id"], user.id, media_type, details["id"], title,
                               details.get("posterPath"), seasons, seerr_id)
                await db.commit()
        except Exception as e:
            print(f"Could not note request {request.get('id')}: {e}")
            return f"✅ Requested **{title}**, but I couldn't note it, so there won't be a DM when it's ready."
        return mr.placed_message(request, title, seasons, seerr_name)

    async def follow(self, user, media_type: str, details: dict) -> str:
        """Wait for requests someone else made, as if this member had made them."""
        requests = mr.followable_requests(details)
        try:
            async with aiosqlite.connect(config.DB_PATH) as db:
                for request in requests:
                    seasons = sorted(s["seasonNumber"] for s in request.get("seasons") or []) or None
                    await mr.track(db, request["id"], user.id, media_type, details["id"], mr.label(details),
                                   details.get("posterPath"), seasons, None)
                await db.commit()
        except Exception as e:
            return f"❌ Error: {e}"
        return f"🔔 I'll DM you when **{mr.label(details)}** is ready to watch."

    # --- the command ---------------------------------------------------------

    async def resolve(self, value: str) -> tuple[str, int] | None:
        """The autocomplete gives "movie:123"; typed text without picking is searched instead."""
        if picked := mr.parse_choice(value):
            return picked
        hit = mr.best_result(value, await self.seerr.search(value))
        return (hit["mediaType"], hit["id"]) if hit else None

    @app_commands.command(name="request", description="Ask for a film or show to be added, and get a DM when it's ready")
    @app_commands.describe(title="A film or show to search for")
    async def request(self, interaction: discord.Interaction, title: str):
        if self.seerr is None:
            await interaction.response.send_message("Requests aren't set up on this server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            picked = await self.resolve(title)
            if not picked:
                await interaction.followup.send(f"Couldn't find a film or show called “{title}”.", ephemeral=True)
                return
            media_type, tmdb_id = picked
            details = await self.seerr.details(media_type, tmdb_id)
        except Exception as e:
            await interaction.followup.send(f"❌ Couldn't reach Seerr: {e}", ephemeral=True)
            return

        seerr_id, seerr_name = await self.seerr_user(interaction.user.id)
        embed = request_card(media_type, details, seerr_name)
        view = RequestView(self, interaction.user.id, media_type, details, seerr_id, seerr_name)
        extra = {"view": view} if view.children else {}
        if poster := await self.seerr.poster(details.get("posterPath")):
            extra["file"] = discord.File(io.BytesIO(poster), filename="poster.jpg")
            embed.set_thumbnail(url="attachment://poster.jpg")
        await interaction.followup.send(embed=embed, ephemeral=True, **extra)

    @request.autocomplete("title")
    async def request_title(self, interaction: discord.Interaction, current: str):
        if self.seerr is None or not current.strip():
            return []
        try:
            # Discord drops an autocomplete answer after three seconds
            results = await asyncio.wait_for(self.seerr.search(current), timeout=2.5)
        except Exception:
            return []
        return [app_commands.Choice(name=mr.choice_label(r)[:100], value=mr.choice_value(r)) for r in results[:25]]

    # --- the poll ------------------------------------------------------------

    @tasks.loop(minutes=POLL_MINUTES)
    async def check_requests(self):
        try:
            await self.poll()
        except Exception as e:
            print(f"Error checking Seerr requests: {e}")

    async def poll(self):
        async with aiosqlite.connect(config.DB_PATH) as db:
            for request_id, rows in (await mr.waiting(db)).items():
                try:
                    request = await self.seerr.get_request(request_id)
                    seasons = None
                    if request and mr.needs_seasons(request):
                        seasons = mr.season_status(await self.seerr.details("tv", rows[0]["tmdb_id"]))
                except Exception as e:
                    print(f"Could not check Seerr request {request_id}: {e}")
                    continue
                result = mr.outcome(request, seasons)
                if result is None:
                    continue
                poster = None
                if result == mr.READY:
                    poster = await self.seerr.poster(rows[0]["poster_path"], "w300")
                for row in rows:
                    await self.deliver(result, row, request, poster)
                    # Marked per person, so a crash part-way repeats at most one DM.
                    await mr.settle(db, request_id, row["discord_user_id"], result)
                    await db.commit()

    async def deliver(self, result: str, row: dict, request: dict | None, poster: bytes | None):
        user_id = row["discord_user_id"]
        if result == mr.GONE:
            print(f"Seerr request {row['request_id']} ({row['title']}) is gone; nothing to announce")
            return
        if result == mr.DECLINED:
            # A DM only: a closed DM is not worth a public mention for a no.
            try:
                user = self.bot.get_user(int(user_id)) or await self.bot.fetch_user(int(user_id))
                await user.send(f"Your request for **{row['title']}** was declined in Seerr.")
            except (discord.HTTPException, ValueError) as e:
                print(f"Could not DM {user_id} that {row['title']} was declined: {e}")
            return

        embed = ready_embed(row, request)
        link = jellyfin_link(request.get("media") or {})
        if poster:
            embed.set_thumbnail(url="attachment://poster.jpg")

        def extra():
            # A discord.File is used up by one send, so each message gets its own.
            kwargs = {"files": [discord.File(io.BytesIO(poster), filename="poster.jpg")] if poster else []}
            if view := open_view(link):
                kwargs["view"] = view
            return kwargs

        try:
            user = self.bot.get_user(int(user_id)) or await self.bot.fetch_user(int(user_id))
            await user.send(embed=embed, **extra())
            return
        except (discord.HTTPException, ValueError) as e:
            print(f"Could not DM {user_id} that {row['title']} is ready: {e}")

        channel = self.bot.get_channel(config.ACTIVITY_CHANNEL_ID) if config.ACTIVITY_CHANNEL_ID else None
        if channel:
            try:
                await channel.send(
                    content=f"<@{user_id}> what you asked for is in (your DMs are closed)",
                    embed=embed, **extra(),
                    allowed_mentions=discord.AllowedMentions(users=[discord.Object(int(user_id))],
                                                             roles=False, everyone=False))
            except (discord.HTTPException, ValueError) as e:
                print(f"Could not post the request fallback for {row['title']}: {e}")


async def setup(bot):
    await bot.add_cog(Requests(bot))
