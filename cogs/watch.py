"""/watch, /pick and /continue: finding something to watch, and getting back to it.

/watch finds a film or show by name, /pick picks one at random (with a reroll),
and /continue lists what you were in the middle of. Every answer is ephemeral, so
only the person asking sees it. For someone linked with /jf-link the card also
says where they left off.
"""
import asyncio
import io
import random
import time

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


def jellyfin_button(item: dict) -> discord.ui.Button | None:
    link = details_link(item["Id"])
    return discord.ui.Button(label="Open in Jellyfin", url=link, emoji="▶️") if link else None


def open_button(item: dict) -> discord.ui.View | None:
    button = jellyfin_button(item)
    if not button:
        return None
    view = discord.ui.View()
    view.add_item(button)
    return view


# --- /pick -------------------------------------------------------------------

PICK_TYPES = {"film": "Movie", "show": "Series"}
LENGTHS = [("Under 30 minutes", 30), ("Under 45 minutes", 45), ("Under 1 hour", 60),
           ("Under 1½ hours", 90), ("Under 2 hours", 120), ("Under 2½ hours", 150)]
REROLL_MINUTES = 10                    # the 🎲 button goes after this long unused
GENRE_CACHE_SECONDS = 600


def pick_types(kind: str | None) -> str:
    """The type option as Jellyfin's IncludeItemTypes: films, shows, or both."""
    return PICK_TYPES.get(kind or "", "Movie,Series")


def short_enough(item: dict, max_minutes: int | None) -> bool:
    """Within the length asked for. For a show that is its episode length. Jellyfin
    has no runtime for many shows; they are left out once a length is asked for."""
    if not max_minutes:
        return True
    ticks = item.get("RunTimeTicks") or 0
    return 0 < ticks <= max_minutes * 60 * TICKS_PER_SECOND


def match_genre(typed: str, genres: list[str]) -> str:
    """The library's spelling of a typed genre: "sci-fi & fantasy" -> "Sci-Fi & Fantasy"."""
    typed = typed.strip()
    return next((g for g in genres if g.lower() == typed.lower()), typed)


def genres_matching(genres: list[str], current: str) -> list[str]:
    """Genres for the autocomplete: those containing what has been typed, at most 25."""
    return [g for g in genres if current.strip().lower() in g.lower()][:25]


def pool_footer(count: int, types: str, unwatched: bool, linked: bool) -> str:
    """'Picked from 77 unwatched films', and a /jf-link hint for someone not linked."""
    nouns = {"Movie": ("film", "films"), "Series": ("show", "shows")}.get(types, ("title", "films and shows"))
    text = f"Picked from {count} {'unwatched ' if unwatched else ''}{nouns[0] if count == 1 else nouns[1]}"
    return text + ("" if linked else ". Use /jf-link to see where you left off.")


def draw(queue: list[dict], pool: list[dict], current_id: str | None) -> dict:
    """The next pick. Takes from a shuffled queue so rerolls do not repeat, and
    reshuffles the pool when it runs out, without starting on the one on screen."""
    if not queue:
        queue.extend(random.sample(pool, len(pool)))
        if len(queue) > 1 and queue[-1]["Id"] == current_id:
            queue[0], queue[-1] = queue[-1], queue[0]
    return queue.pop()


class PickView(discord.ui.View):
    """🎲 Reroll and Open in Jellyfin under a /pick answer. Rerolling edits the
    same message. Not persistent: after REROLL_MINUTES unused the 🎲 goes."""

    def __init__(self, cog: "Watch", pool: list[dict], user_id: str | None, footer: str):
        super().__init__(timeout=REROLL_MINUTES * 60)
        self.cog, self.pool, self.user_id, self.footer = cog, pool, user_id, footer
        self.queue: list[dict] = []
        self.current: dict | None = None
        self.link: discord.ui.Button | None = None
        self.last: discord.Interaction | None = None     # the newest, to edit at the timeout

    async def show(self, interaction: discord.Interaction, first: bool = False):
        found = draw(self.queue, self.pool, self.current["Id"] if self.current else None)
        self.current, embed, poster = await self.cog.answer(found, self.user_id)
        embed.set_footer(text=self.footer)
        if self.link:
            self.remove_item(self.link)
        self.link = jellyfin_button(self.current)
        if self.link:
            self.add_item(self.link)
        if first:
            extra = {"file": poster} if poster else {}
            await interaction.followup.send(embed=embed, view=self, ephemeral=True, **extra)
        else:
            await interaction.edit_original_response(embed=embed, attachments=[poster] if poster else [], view=self)
        self.last = interaction

    @discord.ui.button(label="Reroll", emoji="🎲", style=discord.ButtonStyle.primary)
    async def reroll(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self.show(interaction)

    async def on_timeout(self):
        self.remove_item(self.reroll)
        try:
            await self.last.edit_original_response(view=self)
        except (AttributeError, discord.HTTPException):
            pass

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        print(f"/pick reroll failed: {error}")
        await interaction.followup.send("Couldn't reach Jellyfin for another pick.", ephemeral=True)


# --- /continue ---------------------------------------------------------------

CONTINUE_LIMIT = 8


def played_at(item: dict) -> str:
    """When the user last played it, sortable ("" if never). Seconds only: Jellyfin
    writes fractions of different lengths."""
    return ((item.get("UserData") or {}).get("LastPlayedDate") or "")[:19]


def last_finished(episodes: list[dict]) -> dict[str, str]:
    """Each show's most recently finished episode, as show id -> played_at."""
    finished: dict[str, str] = {}
    for episode in episodes:
        if series := episode.get("SeriesId"):
            finished[series] = max(finished.get(series, ""), played_at(episode))
    return finished


def in_progress(resume: list[dict], next_up: list[dict], finished: dict[str, str],
                limit: int = CONTINUE_LIMIT) -> list[dict]:
    """What /continue lists: one entry per film or show, most recent first.

    For a show, a half-watched episode beats Jellyfin's next-up one, unless an
    episode was finished after it: then it is an old one left part-way."""
    entries: dict[str, tuple[str, dict]] = {}      # film or show id -> (date, item)
    for item in resume:
        if item.get("Type") == "Movie":
            key = item["Id"]
        elif item.get("Type") == "Episode" and item.get("SeriesId"):
            key = item["SeriesId"]
            if (item.get("UserData") or {}).get("Played") or played_at(item) < finished.get(key, ""):
                continue
        else:
            continue        # Jellyfin lists whole seasons and shows here too
        if key not in entries or played_at(item) > entries[key][0]:
            entries[key] = (played_at(item), item)
    for episode in next_up:
        key = episode.get("SeriesId")
        if key and key not in entries:
            # Dated by the show's last finished episode, which is what makes it next
            entries[key] = (max(finished.get(key, ""), played_at(episode)), episode)
    ordered = sorted(entries.values(), key=lambda entry: entry[0], reverse=True)
    return [item for _, item in ordered[:limit]]


def continue_line(item: dict) -> str:
    """'▶️ Rush Hour · stopped at 6:20 of 1:37:49' or '⏭️ The Simpsons · S02E10 · Bart Gets Hit by a Car'."""
    if item.get("Type") == "Episode":
        text = f"{linked_title(item.get('SeriesName') or 'Unknown', item.get('Id'))} · {episode_label(item)}"
    else:
        text = linked_title(item.get("Name") or "Unknown", item.get("Id"))
    position = (item.get("UserData") or {}).get("PlaybackPositionTicks") or 0
    if not position:
        return f"⏭️ {text}"
    total = item.get("RunTimeTicks")
    return f"▶️ {text} · stopped at {clock(position)}" + (f" of {clock(total)}" if total else "")


# --- the command -------------------------------------------------------------

class Watch(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._genres: dict[str, tuple[float, list[str]]] = {}      # types -> (when, names)

    async def resolve(self, value: str) -> dict | None:
        """The autocomplete gives an item id; typed text without picking is searched instead."""
        if JELLYFIN_ID.match(value):
            item = await self.bot.jf.get_item(value)
            if item.get("Type") in ("Movie", "Series"):
                return item
        return best_match(value, await self.bot.jf.search_titles(value, limit=10))

    async def jellyfin_user(self, discord_user_id: int) -> str | None:
        """The caller's Jellyfin user id, or None when they are not linked or the
        link names no Jellyfin user."""
        name = await watch_data.get_link(discord_user_id)
        return await self.bot.jf.get_user_id(name) if name else None

    async def progress(self, user_id: str | None, item_id: str) -> tuple[dict | None, str | None]:
        """The item as this Jellyfin user sees it, and where they left off.
        (None, None) without a user."""
        if not user_id:
            return None, None
        item = await self.bot.jf.get_item(item_id, user_id)
        if not item:
            return None, None
        if item.get("Type") == "Series":
            return item, series_progress(item, await self.bot.jf.next_up(user_id, item_id))
        return item, film_progress(item)

    async def answer(self, found: dict, user_id: str | None) -> tuple[dict, discord.Embed, discord.File | None]:
        """The card for a film or show: the full item, the embed, and its poster."""
        item, progress = await self.progress(user_id, found["Id"])
        if item is None:
            item = await self.bot.jf.get_item(found["Id"]) or found
            # Jellyfin counts a show's episodes only when asked as a user
            if item.get("Type") == "Series" and not item.get("RecursiveItemCount"):
                item = {**item, "RecursiveItemCount": await self.bot.jf.get_series_episode_count(item["Id"])}
        embed = watch_card(item, progress)
        poster = None
        if image := await self.bot.jf.get_image(item["Id"], "Primary", max_width=600):
            poster = discord.File(io.BytesIO(image), filename="poster.jpg")
            embed.set_thumbnail(url="attachment://poster.jpg")
        return item, embed, poster

    async def genres(self, types: str) -> list[str]:
        """The library's genres, kept for a while: the autocomplete asks on every keystroke."""
        cached = self._genres.get(types)
        if cached and time.monotonic() - cached[0] < GENRE_CACHE_SECONDS:
            return cached[1]
        names = await self.bot.jf.get_genres(types)
        self._genres[types] = (time.monotonic(), names)
        return names

    # --- /watch ---------------------------------------------------------------

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
        item, embed, poster = await self.answer(found, await self.jellyfin_user(interaction.user.id))
        extra = {}
        if view := open_button(item):
            extra["view"] = view
        if poster:
            extra["file"] = poster
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

    # --- /pick ----------------------------------------------------------------

    @app_commands.command(name="pick", description="Something to watch, picked at random, with a reroll")
    @app_commands.rename(kind="type")
    @app_commands.describe(
        kind="A film or a show (either, if left out)",
        genre="A genre from the library",
        max_length="How long it can be. For a show, the length of an episode",
        include_watched="Also pick what you've already seen (once you've used /jf-link)",
    )
    @app_commands.choices(
        kind=[app_commands.Choice(name="Film", value="film"), app_commands.Choice(name="Show", value="show")],
        max_length=[app_commands.Choice(name=name, value=minutes) for name, minutes in LENGTHS],
    )
    async def pick(self, interaction: discord.Interaction, kind: app_commands.Choice[str] | None = None,
                   genre: str | None = None, max_length: app_commands.Choice[int] | None = None,
                   include_watched: bool = False):
        if self.bot.jf is None:
            await interaction.response.send_message("Jellyfin isn't configured.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        types = pick_types(kind.value if kind else None)
        if genre:
            genre = match_genre(genre, await self.genres(types))
        user_id = await self.jellyfin_user(interaction.user.id)
        unwatched = bool(user_id) and not include_watched
        items = await self.bot.jf.pick_pool(types, genre, user_id, unwatched)
        pool = [i for i in items if short_enough(i, max_length.value if max_length else None)]
        if not pool:
            hint = " Try `include_watched`, or fewer filters." if unwatched else " Try fewer filters."
            await interaction.followup.send("Nothing in the library matches that." + hint, ephemeral=True)
            return
        view = PickView(self, pool, user_id, pool_footer(len(pool), types, unwatched, linked=bool(user_id)))
        await view.show(interaction, first=True)

    @pick.autocomplete("genre")
    async def pick_genre(self, interaction: discord.Interaction, current: str):
        if self.bot.jf is None:
            return []
        try:
            names = await self.genres(pick_types(getattr(interaction.namespace, "type", None)))
        except Exception:
            return []
        return [app_commands.Choice(name=g, value=g) for g in genres_matching(names, current)]

    # --- /continue ------------------------------------------------------------

    @app_commands.command(name="continue", description="What you were in the middle of, with links")
    async def continue_watching(self, interaction: discord.Interaction):
        if self.bot.jf is None:
            await interaction.response.send_message("Jellyfin isn't configured.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        user_id = await self.jellyfin_user(interaction.user.id)
        if not user_id:
            await interaction.followup.send(
                "Link your Jellyfin user with `/jf-link` first, then `/continue` shows what you were in the middle of.",
                ephemeral=True)
            return
        # More than we list: seasons, finished episodes and repeats of a show drop out
        resume, next_up, finished = await asyncio.gather(
            self.bot.jf.resume_items(user_id, limit=20),
            self.bot.jf.next_up_items(user_id, limit=20),
            self.bot.jf.played_episodes(user_id),
        )
        items = in_progress(resume, next_up, last_finished(finished))
        if not items:
            await interaction.followup.send("Nothing in progress. `/pick` can find you something.", ephemeral=True)
            return
        embed = discord.Embed(title="Continue watching", colour=COLOUR,
                              description="\n".join(continue_line(i) for i in items))
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Watch(bot))
