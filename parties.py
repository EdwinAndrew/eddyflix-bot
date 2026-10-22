"""Watch parties: two or more people playing the same title at about the same point.

Jellyfin does not tell the bot about SyncPlay groups: /SyncPlay/List needs a
signed-in user and answers an API key with 400, and sessions carry no group id.
So a party is worked out from the sessions instead. SyncPlay keeps its members
within seconds of each other, and people who just press play together count too.
"""
from datetime import datetime

import discord

TICKS_PER_SECOND = 10_000_000
TOLERANCE_SECONDS = 120   # how far apart two people may be and still be "together"
COLOUR = 0xF4C430


def _position(session) -> float:
    return (session.get('PlayState') or {}).get('PositionTicks', 0) / TICKS_PER_SECOND


def find_parties(sessions) -> dict[str, dict]:
    """item id -> {"item", "users", "session_ids"} for every title that at least two
    different people are playing within TOLERANCE_SECONDS of each other."""
    by_item: dict[str, list] = {}
    for session in sessions:
        item, user = session.get('NowPlayingItem'), session.get('UserName')
        if item and item.get('Id') and user:
            by_item.setdefault(item['Id'], []).append(session)

    parties = {}
    for item_id, group in by_item.items():
        group.sort(key=_position)
        # Runs of sessions whose positions are each within the tolerance of the
        # previous one; the run with the most different people is the party.
        runs, run = [], [group[0]]
        for session in group[1:]:
            if _position(session) - _position(run[-1]) <= TOLERANCE_SECONDS:
                run.append(session)
            else:
                runs.append(run)
                run = [session]
        runs.append(run)
        best = max(runs, key=lambda r: len({s['UserName'].lower() for s in r}))
        users = sorted({s['UserName'] for s in best}, key=str.lower)
        if len({u.lower() for u in users}) >= 2:
            parties[item_id] = {
                "item": best[0]['NowPlayingItem'],
                "users": users,
                "session_ids": {s['Id'] for s in best},
            }
    return parties


def join_names(names) -> str:
    names = [f"**{n}**" for n in names]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def title_of(item) -> str:
    if item.get('Type') == 'Episode':
        return (f"{item.get('SeriesName', 'Unknown')} "
                f"S{item.get('ParentIndexNumber', '?')}E{item.get('IndexNumber', '?')}")
    year = item.get('ProductionYear')
    return f"{item.get('Name', 'Unknown')} ({year})" if year else item.get('Name', 'Unknown')


def live_embed(item, users, started: datetime) -> discord.Embed:
    embed = discord.Embed(
        title="🍿 Watching together",
        description=f"{join_names(users)} are watching **{title_of(item)}** together",
        colour=COLOUR,
    )
    embed.add_field(name="Started", value=f"<t:{int(started.timestamp())}:R>", inline=True)
    return embed


def ended_embed(item, users, started: datetime, ended: datetime) -> discord.Embed:
    minutes = max(1, int((ended - started).total_seconds() // 60))
    length = f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"
    return discord.Embed(
        title="🎬 Watched together",
        description=f"{join_names(users)} watched **{title_of(item)}** together, for {length}",
        colour=COLOUR,
    )
