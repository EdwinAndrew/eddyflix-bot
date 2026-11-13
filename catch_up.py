"""Offering new-episode DMs when someone catches up on a show.

Hardly anyone found /subscribe. So when a linked member finishes the latest
episode the library has of a show they are not subscribed to, the bot asks them
once, in a DM, with a 🔔 button that subscribes them. Once per member per show,
ever: pressing it, ignoring it and closing the DM all count. Shows Jellyfin
marks as Ended are not offered.

The activity cog notices the end of each play (cogs/activity.py, record) and
hands over the session as it last saw it; the subscriptions cog does the asking.
Finished means the last poll had 90% of the episode played, or Jellyfin marks
it played.

The button is a DynamicItem with the show and the member in its custom_id,
registered in the bot's setup_hook, so it keeps working after a restart. The
show's name comes from the offer's row, so pressing it needs no Jellyfin.

Pure apart from the database helpers and the button, for the tests.
"""
import re

import aiosqlite
import discord

import config
import new_episodes

FINISHED = 0.9                  # this much of an episode played counts as watched
LABEL = "Tell me when new episodes land"
COLOUR = discord.Colour(0x00A4DC)      # Jellyfin blue


# --- the rules -----------------------------------------------------------------

def progress(session: dict) -> float | None:
    """How far into its item a session was, 0 to 1, or None if Jellyfin did not say."""
    total = (session.get("NowPlayingItem") or {}).get("RunTimeTicks") or 0
    position = (session.get("PlayState") or {}).get("PositionTicks")
    if not total or position is None:
        return None
    return position / total


def reached_end(session: dict) -> bool:
    p = progress(session)
    return p is not None and p >= FINISHED


def _numbers(ep: dict) -> tuple[int, int] | None:
    """(season, last episode number in the file), or None without numbers."""
    season, number = ep.get("ParentIndexNumber"), ep.get("IndexNumber")
    if season is None or number is None:
        return None
    return season, max(ep.get("IndexNumberEnd") or number, number)


def latest(episodes: list[dict]) -> dict | None:
    """The newest episode the library has: highest season and number, specials
    (season 0) and missing episodes left out."""
    real = [(n, e) for e in episodes
            if e.get("LocationType") != "Virtual" and (n := _numbers(e)) and n[0] > 0]
    return max(real, key=lambda pair: pair[0])[1] if real else None


def newest_first(episodes: list[dict]) -> bool:
    """Whether Jellyfin listed them highest season and number first, as asked.
    If not, the first page proves nothing and the caller lists them all."""
    keys = [(e["ParentIndexNumber"], e["IndexNumber"]) for e in episodes
            if e.get("ParentIndexNumber") is not None and e.get("IndexNumber") is not None]
    return all(a >= b for a, b in zip(keys, keys[1:]))


def caught_up(played: dict, newest: dict | None) -> bool:
    """Whether the episode just played is the newest one the library has."""
    if not newest:
        return False
    if played.get("Id") and played.get("Id") == newest.get("Id"):
        return True
    p, n = _numbers(played), _numbers(newest)
    return bool(p and n and p[0] > 0 and p >= n)


# --- the messages --------------------------------------------------------------

def offer_embed(name: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"🔔 You're caught up on {name}"[:256],
        description=("That was the newest episode on EddyFlix. Want a DM when the next ones land?\n\n"
                     "I'll only ask once about this show."),
        colour=COLOUR)
    embed.set_footer(text="/subscribe does this for any show")
    return embed


def fallback_text(member_id, name: str) -> str:
    """The mention in the activity channel when the member's DMs are closed."""
    return f"<@{member_id}> you're caught up on **{name}** (your DMs are closed)"


def confirmation(name: str, added: bool) -> str:
    if added:
        return f"✅ Done. I'll DM you when new episodes of **{name}** land. `/unsubscribe` to stop."
    return f"✅ You're already subscribed to **{name}**. `/unsubscribe` to stop."


# --- the database --------------------------------------------------------------

async def linked_members(db, jellyfin_user: str) -> list[str]:
    """The Discord members linked to this Jellyfin user (any case), as stored."""
    async with db.execute("SELECT discord_user_id FROM user_links WHERE lower(jellyfin_user) = lower(?)",
                          (jellyfin_user,)) as cursor:
        return [r[0] for r in await cursor.fetchall()]


async def offered(db, member_id: str, series_id: str, series_name: str | None) -> bool:
    """Whether this member was offered this show before. By id, and by name for
    a show added to the library again under a new id."""
    async with db.execute("SELECT series_id, series_name FROM subscription_offers WHERE discord_user_id = ?",
                          (member_id,)) as cursor:
        rows = await cursor.fetchall()
    name = new_episodes.normalize(series_name)
    return any(sid == series_id or (name and new_episodes.normalize(sname) == name) for sid, sname in rows)


async def mark_offered(db, member_id: str, series_id: str, series_name: str) -> bool:
    """Note the offer. False if it was noted already, e.g. by a second session
    ending at the same moment, and then it is not sent."""
    cursor = await db.execute("INSERT OR IGNORE INTO subscription_offers (discord_user_id, series_id, series_name) "
                              "VALUES (?, ?, ?)", (member_id, series_id, series_name))
    return cursor.rowcount == 1


async def offered_name(db, series_id: str) -> str | None:
    async with db.execute("SELECT series_name FROM subscription_offers WHERE series_id = ? LIMIT 1",
                          (series_id,)) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else None


# --- the button ----------------------------------------------------------------

class OfferButton(discord.ui.DynamicItem[discord.ui.Button],
                  template=r"subscribe:offer:(?P<series>[0-9A-Za-z]+):(?P<member>[0-9]+)"):
    """Subscribes whoever presses it, exactly as /subscribe would. For the
    member it was offered to, the message is edited to say so; anyone else (it
    can be in the activity channel) gets the answer only they can see."""

    def __init__(self, series_id: str, member_id: int):
        super().__init__(discord.ui.Button(label=LABEL, emoji="🔔", style=discord.ButtonStyle.primary,
                                           custom_id=f"subscribe:offer:{series_id}:{member_id}"))
        self.series_id, self.member_id = series_id, member_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["series"], int(match["member"]))

    async def callback(self, interaction: discord.Interaction):
        user = interaction.user
        try:
            async with aiosqlite.connect(config.DB_PATH) as db:
                name = await offered_name(db, self.series_id)
                if name is None:
                    jf = getattr(interaction.client, "jf", None)
                    name = (await jf.get_item(self.series_id)).get("Name") if jf else None
                if not name:
                    await interaction.response.send_message("That show isn't in Jellyfin any more.", ephemeral=True)
                    return
                added = await new_episodes.subscribe(db, str(user.id), user.name, name, self.series_id)
        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)
            return

        text = confirmation(name, added)
        message = interaction.message
        if user.id == self.member_id and message is not None and message.embeds:
            embed = message.embeds[0]
            embed.title = f"🔔 Subscribed to {name}"[:256]
            embed.description = text
            embed.remove_footer()
            await interaction.response.edit_message(embed=embed, view=done_view())
        else:
            await interaction.response.send_message(text, ephemeral=True)


def offer_view(series_id: str, member_id) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(OfferButton(series_id, int(member_id)))
    return view


def done_view() -> discord.ui.View:
    """What the button turns into once pressed."""
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Subscribed", emoji="✅", style=discord.ButtonStyle.secondary,
                                    custom_id="subscribe:offer:done", disabled=True))
    return view
