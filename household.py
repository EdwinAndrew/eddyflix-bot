"""Watching as a household: /pick for two people, and where the other linked members
are in a show ("Sarah is 3 episodes ahead of you").

Plain functions on Jellyfin's answers; cogs/watch.py makes the calls.

Where someone is in a show is the episode they are on: the one they stopped part-way
through, or Jellyfin's next-up one. Their position is how many episodes come before it
in the show's order (season, then episode), or every episode once they have seen them
all. Two people's positions differ by the number of episodes between them.
"""

MAX_OTHERS = 10        # other linked members looked up per command, to keep Jellyfin calls bounded

LINK_HOW = "`/link` or the 👤 button in #roles"


# --- who else is linked -----------------------------------------------------------

def other_members(links: list[tuple[int, str]], my_id: str | None, user_ids: dict[str, str],
                  names: dict[int, str]) -> list[tuple[str, str]]:
    """The linked members other than the caller, as (name, Jellyfin user id).

    links are (Discord id, Jellyfin user name) from /link; user_ids maps Jellyfin
    names in lower case to ids; names are Discord display names where known, else the
    Jellyfin name is shown. Someone on the caller's own Jellyfin user is not "other",
    and two Discord accounts on one Jellyfin user count once.
    """
    seen = {my_id} if my_id else set()
    others = []
    for discord_id, jellyfin_name in links:
        user_id = user_ids.get(jellyfin_name.lower())
        if not user_id or user_id in seen:
            continue
        seen.add(user_id)
        others.append((names.get(discord_id) or jellyfin_name, user_id))
    return sorted(others, key=lambda o: o[0].lower())[:MAX_OTHERS]


# --- /pick with someone -----------------------------------------------------------

def not_linked(me_linked: bool, partner: str, partner_linked: bool) -> str | None:
    """Why /pick with:someone cannot filter, and how to fix it; None when both are linked."""
    if me_linked and partner_linked:
        return None
    if not me_linked and not partner_linked:
        return (f"To pick something neither of you has seen, you both need to be linked to your "
                f"Jellyfin users. Use {LINK_HOW}, and ask {partner} to do the same.")
    if not me_linked:
        return f"To pick something neither of you has seen, link your Jellyfin user first: {LINK_HOW}."
    return (f"{partner} isn't linked to a Jellyfin user yet, so I can't tell what they've watched. "
            f"They can use {LINK_HOW}.")


def unwatched_by_both(mine: list[dict], theirs: list[dict]) -> list[dict]:
    """The titles in both people's unwatched pools, in the order of the first."""
    ids = {i.get("Id") for i in theirs}
    return [i for i in mine if i.get("Id") in ids]


def together_note(item: dict) -> str:
    """The line on a /pick card for two. A show is in the pool when neither has finished it."""
    return "Neither of you has finished this" if item.get("Type") == "Series" else "Neither of you has seen this"


# --- where everyone is in a show ----------------------------------------------------

def episode_key(episode: dict | None) -> tuple[int, int] | None:
    """(season, episode), or None for a special (season 0) or an unnumbered one."""
    if not episode:
        return None
    season, number = episode.get("ParentIndexNumber"), episode.get("IndexNumber")
    if season is None or number is None or season == 0:
        return None
    return season, number


def episode_order(episodes: list[dict]) -> list[tuple[int, int]]:
    """A show's episodes in order as (season, episode). Specials, unnumbered and missing
    (virtual) episodes are left out; a second copy of an episode counts once."""
    return sorted({key for e in episodes
                   if e.get("LocationType") != "Virtual" and (key := episode_key(e))})


def index_of(order: list[tuple[int, int]], episode: dict | None) -> int | None:
    """How many episodes come before this one, or None if it is not in the order."""
    key = episode_key(episode)
    try:
        return order.index(key) if key else None
    except ValueError:
        return None


def started(series: dict, next_episode: dict | None) -> bool:
    """Whether this user has watched any of the show, from their UserData on it."""
    data = series.get("UserData") or {}
    if data.get("Played") or (data.get("PlayedPercentage") or 0) > 0:
        return True
    total, unplayed = series.get("RecursiveItemCount"), data.get("UnplayedItemCount")
    if total and unplayed is not None and unplayed < total:
        return True
    return bool(((next_episode or {}).get("UserData") or {}).get("PlaybackPositionTicks"))


def position(order: list[tuple[int, int]], series: dict, next_episode: dict | None) -> int | None:
    """How far into the show this user is, or None when they have not started it or
    are somewhere that cannot be placed (a special, or no next-up episode for a show
    they have not finished)."""
    if not order or not started(series, next_episode):
        return None
    if (series.get("UserData") or {}).get("Played"):
        return len(order)
    return index_of(order, next_episode)


def my_position(order: list[tuple[int, int]], series: dict, next_episode: dict | None) -> int | None:
    """The caller's position: like position(), but not started is the start of the show."""
    if order and not started(series, next_episode):
        return 0
    return position(order, series, next_episode)


def ahead_line(name: str, theirs: int, mine: int, total: int) -> str:
    """'Sarah is 3 episodes ahead of you', '... behind you', or '... at the same episode'."""
    gap = theirs - mine
    if gap == 0:
        return f"{name} has finished it too" if theirs >= total else f"{name} is at the same episode"
    count = f"{abs(gap)} episode{'s' if abs(gap) != 1 else ''}"
    return f"{name} is {count} ahead of you" if gap > 0 else f"{name} is {count} behind you"


def ahead_lines(mine: int | None, theirs: list[tuple[str, int]], total: int) -> list[str]:
    """One line per other member who has watched the show, furthest ahead first."""
    if mine is None:
        return []
    ordered = sorted(theirs, key=lambda t: (-t[1], t[0].lower()))
    return [ahead_line(name, where, mine, total) for name, where in ordered]
