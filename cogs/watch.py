"""/watch: find a film or show in the library and get a link to it.

The answer is ephemeral, so only the person asking sees it. For someone linked
with /jf-link it also says where they left off.
"""
import io

import discord
from discord import app_commands
from discord.ext import commands

import watch_data
from cogs.movie_night import JELLYFIN_ID, details_link, film_label

TICKS_PER_SECOND = 10_000_000
COLOUR = discord.Colour(0x00A4DC)      # Jellyfin blue


# --- formatting --------------------------------------------------------------

def choice_label(item: dict) -> str:
    """How a film or show reads in the autocomplete list."""
    return f"{'📺' if item.get('Type') == 'Series' else '🎬'} {film_label(item)}"


def clock(ticks: int) -> str:
    """Ticks as 1:03:22, or 26:37 under an hour."""
    seconds = int(ticks // TICKS_PER_SECOND)
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def duration(ticks: int) -> str:
    """Ticks as 1h 37m, or 24m under an hour."""
    minutes = round(ticks / TICKS_PER_SECOND / 60)
    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def episode_label(episode: dict) -> str:
    season, number = episode.get("ParentIndexNumber"), episode.get("IndexNumber")
    code = f"S{season:02d}E{number:02d}" if season is not None and number is not None else ""
    return f"{code} · {episode.get('Name', '')}" if code else episode.get("Name", "Unknown")


def plain_title(text: str) -> str:
    """Lower case, without a leading article: "The Dark Knight" -> "dark knight"."""
    text = text.strip().lower()
    for article in ("the ", "a ", "an "):
        if text.startswith(article):
            return text[len(article):]
    return text


def best_match(typed: str, found: list[dict]) -> dict | None:
    """The exact title if Jellyfin found it, else its first hit. Its first hit is not
    always the exact one: "dark knight" gave The Dark Knight Rises."""
    exact = [i for i in found if plain_title(i.get("Name", "")) == plain_title(typed)]
    return (exact or found or [None])[0]


def linked_title(text: str, item_id: str | None) -> str:
    link = details_link(item_id) if item_id else None
    return f"[{text}]({link})" if link else f"**{text}**"


def film_progress(item: dict) -> str:
    data = item.get("UserData") or {}
    if data.get("Played"):
        return "✅ Watched"
    position = data.get("PlaybackPositionTicks") or 0
    if position:
        total = item.get("RunTimeTicks")
        return f"▶️ Stopped at **{clock(position)}**" + (f" of {clock(total)}" if total else "")
    return "Not watched yet"


def series_progress(item: dict, next_episode: dict | None) -> str:
    data = item.get("UserData") or {}
    total = item.get("RecursiveItemCount") or 0
    unplayed = data.get("UnplayedItemCount")
    watched = total - unplayed if total and unplayed is not None else 0
    if data.get("Played"):
        return "✅ All caught up"
    lines = []
    if next_episode:
        position = (next_episode.get("UserData") or {}).get("PlaybackPositionTicks") or 0
        episode = linked_title(episode_label(next_episode), next_episode.get("Id"))
        lines.append(f"▶️ Resume {episode} at **{clock(position)}**" if position else f"⏭️ Up next: {episode}")
    if watched:
        lines.append(f"{watched} of {total} episodes watched")
    return "\n".join(lines) or "Not started yet"


def details(item: dict) -> str:
    """Runtime for a film; seasons, episodes and status for a show."""
    if item.get("Type") != "Series":
        return duration(item["RunTimeTicks"]) if item.get("RunTimeTicks") else "—"
    parts = []
    if item.get("ChildCount"):
        parts.append(f"{item['ChildCount']} season{'s' if item['ChildCount'] != 1 else ''}")
    if item.get("RecursiveItemCount"):
        parts.append(f"{item['RecursiveItemCount']} episodes")
    if item.get("RunTimeTicks"):
        parts.append(f"~{duration(item['RunTimeTicks'])} each")
    if item.get("Status") == "Ended":
        parts.append("ended")
    return " · ".join(parts) or "—"


def watch_card(item: dict, progress: str | None) -> discord.Embed:
    """The answer to /watch. progress is None when the caller is not linked."""
    is_series = item.get("Type") == "Series"
    overview = (item.get("Overview") or "").strip()
    embed = discord.Embed(
        title=f"{'📺' if is_series else '🎬'} {film_label(item)}"[:256],
        url=details_link(item["Id"]),
        description=(overview[:400] + ("…" if len(overview) > 400 else "")) or None,
        colour=COLOUR,
    )
    embed.add_field(name="Show" if is_series else "Runtime", value=details(item), inline=True)
    embed.add_field(name="Genre", value=(", ".join(item.get("Genres") or []) or "—")[:100], inline=True)
    if progress is not None:
        embed.add_field(name="Where you left off", value=progress, inline=False)
    else:
        embed.set_footer(text="Use /jf-link to see where you left off.")
    return embed


def open_button(item: dict) -> discord.ui.View | None:
    link = details_link(item["Id"])
    if not link:
        return None
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Open in Jellyfin", url=link, emoji="▶️"))
    return view


# --- the command -------------------------------------------------------------

class Watch(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def resolve(self, value: str) -> dict | None:
        """The autocomplete gives an item id; typed text without picking is searched instead."""
        if JELLYFIN_ID.match(value):
            item = await self.bot.jf.get_item(value)
            if item.get("Type") in ("Movie", "Series"):
                return item
        return best_match(value, await self.bot.jf.search_titles(value, limit=10))

    async def progress(self, discord_user_id: int, item_id: str) -> tuple[dict | None, str | None]:
        """The item as the caller's Jellyfin user sees it, and where they left off.
        (None, None) when they are not linked or the link names no Jellyfin user."""
        name = await watch_data.get_link(discord_user_id)
        user_id = await self.bot.jf.get_user_id(name) if name else None
        if not user_id:
            return None, None
        item = await self.bot.jf.get_item(item_id, user_id)
        if not item:
            return None, None
        if item.get("Type") == "Series":
            return item, series_progress(item, await self.bot.jf.next_up(user_id, item_id))
        return item, film_progress(item)

    @app_commands.command(name="watch", description="Find a film or show and get a link to it, with where you left off")
    @app_commands.describe(title="A film or show from the Jellyfin library")
    async def watch(self, interaction: discord.Interaction, title: str):
        if self.bot.jf is None:
            await interaction.response.send_message("Jellyfin isn't configured.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        found = await self.resolve(title)
        if not found:
            await interaction.followup.send(f"Nothing called “{title}” in Jellyfin.", ephemeral=True)
            return
        item, progress = await self.progress(interaction.user.id, found["Id"])
        if item is None:
            item = await self.bot.jf.get_item(found["Id"]) or found
            # Jellyfin counts a show's episodes only when asked as a user
            if item.get("Type") == "Series" and not item.get("RecursiveItemCount"):
                item = {**item, "RecursiveItemCount": await self.bot.jf.get_series_episode_count(item["Id"])}

        embed = watch_card(item, progress)
        extra = {}
        if view := open_button(item):
            extra["view"] = view
        if poster := await self.bot.jf.get_image(item["Id"], "Primary", max_width=600):
            extra["file"] = discord.File(io.BytesIO(poster), filename="poster.jpg")
            embed.set_thumbnail(url="attachment://poster.jpg")
        await interaction.followup.send(embed=embed, ephemeral=True, **extra)

    @watch.autocomplete("title")
    async def watch_title(self, interaction: discord.Interaction, current: str):
        if self.bot.jf is None:
            return []
        try:
            items = await self.bot.jf.search_titles(current, limit=25)
        except Exception:
            return []
        return [app_commands.Choice(name=choice_label(i)[:100], value=i["Id"]) for i in items]


async def setup(bot):
    await bot.add_cog(Watch(bot))
