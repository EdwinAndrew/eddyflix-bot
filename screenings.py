"""Movie night timing and attendance: when to remind, start and end, who came.

The movie night cog asks these once a minute for every night that is not over
yet. They only decide; the cog does the Discord calls and keeps the state in
the database (watch_data), so a restart picks up where it left off.
"""
from datetime import datetime, timedelta

import discord

REMIND_BEFORE = timedelta(minutes=15)   # the Ticket Holder ping before the start
EMPTY_END = timedelta(minutes=15)       # the Theatre empty this long ends the night early
ATTEND_MINUTES = 15                     # minutes in the Theatre that count as attending
COLOUR = 0xF4C430


def before_start(now: datetime, start: datetime, end: datetime, created: datetime,
                 reminded: bool) -> str | None:
    """What a night that is not live yet needs: "remind", "start", "missed" or nothing.

    No reminder when the announcement itself went out less than half an hour
    before the start: it would ping people twice in a few minutes. "missed"
    means the bot was down from before the start until after the end.
    """
    if now >= end:
        return "missed"
    if now >= start:
        return "start"
    if not reminded and now >= start - REMIND_BEFORE and created <= start - 2 * REMIND_BEFORE:
        return "remind"
    return None


def time_to_end(now: datetime, start: datetime, end: datetime, empty_since: datetime | None) -> bool:
    """A live night ends at its end time (runtime + 15 minutes), or earlier once
    the Theatre has been empty for EMPTY_END after people were in it. The early
    end only counts in the second half, so a slow start or a break does not end it."""
    if now >= end:
        return True
    halfway = start + (end - start) / 2
    return empty_since is not None and now >= halfway and now - empty_since >= EMPTY_END


def split_attendance(minutes: dict[int, int]) -> tuple[list[int], list[int]]:
    """(attended, dropped by): Discord ids, most minutes first. Attending means
    ATTEND_MINUTES in the Theatre while the night was live, not in one go."""
    order = sorted(minutes, key=lambda uid: (-minutes[uid], uid))
    return ([uid for uid in order if minutes[uid] >= ATTEND_MINUTES],
            [uid for uid in order if 0 < minutes[uid] < ATTEND_MINUTES])


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def reminder_embed(title: str, link: str | None, steps: str) -> discord.Embed:
    return discord.Embed(title=f"🍿 Starting soon: {title}", url=link,
                         description=f"**How to watch together**\n{steps}", colour=discord.Colour(COLOUR))


def recap_embed(title: str, attended: list[tuple[int, int]], dropped: list[int]) -> discord.Embed:
    """attended is (Discord id, movie nights attended so far) pairs."""
    embed = discord.Embed(title=f"🎬 That's a wrap: {title}", colour=discord.Colour(COLOUR))
    lines = [f"<@{uid}> · {ordinal(count)} movie night" for uid, count in attended[:30]]
    embed.add_field(name=f"🎥 In the Theatre ({len(attended)})", value="\n".join(lines) or "Nobody stayed",
                    inline=False)
    if dropped:
        embed.add_field(name="Dropped by", value=" ".join(f"<@{uid}>" for uid in dropped[:30]), inline=False)
    embed.set_footer(text=f"{ATTEND_MINUTES} minutes in the Theatre counts as attending. "
                          "The tally is on /achievements")
    return embed
