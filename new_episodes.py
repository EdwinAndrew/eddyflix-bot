"""Finding newly added episodes and grouping them for /subscribe notifications.

The subscriptions cog polls Jellyfin for episodes sorted newest first. Two
things are kept in the database between polls:

- a high-water mark, the newest DateCreated seen. Each poll looks back a few
  hours from it, because Jellyfin stamps DateCreated when it starts on an
  item, which can be a while before the item shows up in a listing
- the episodes already handled, by show, season and episode number. A file
  upgraded by Sonarr or a rebuilt library gets new item ids, but not new
  numbers, so it is not announced again

Everything here except the last few database helpers is pure, for the tests.
Subscribing itself is here too, shared by /subscribe and the 🔔 button the
bot offers when someone catches up on a show (catch_up.py).
"""
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone

LOOKBACK = timedelta(hours=3)     # how far before the mark each poll looks
SETTLE = timedelta(minutes=10)    # wait this long after a show's newest episode...
MAX_HOLD = timedelta(hours=1)     # ...but no longer than this after its first
MAX_LISTED = 10                   # episodes listed by name in one notification


def parse_date(value: str | None) -> datetime | None:
    """Jellyfin's DateCreated, e.g. 2026-11-12T08:04:59.3266898Z, as an aware UTC datetime."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        # Older Pythons choke on seven decimal places; cut to six.
        m = re.fullmatch(r"(.*\.\d{6})\d*(Z|[+-].*)?", value)
        if not m:
            return None
        try:
            parsed = datetime.fromisoformat(m[1] + (m[2] or ""))
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def normalize(name: str | None) -> str:
    """A show name for comparing: no case, accents, punctuation or trailing "(2018)"/"(US)".

    Subscriptions from before the autocomplete were typed by hand ("the rookie").
    """
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    text = re.sub(r"\s*\([^)]*\)\s*$", "", text)
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


def episode_key(ep: dict) -> str:
    """Which episode this is, independent of the file: show, season, number."""
    if ep.get("IndexNumber") is None:
        return f"id:{ep.get('Id')}"
    return f"{normalize(ep.get('SeriesName'))}:{ep.get('ParentIndexNumber') or 0}:{ep['IndexNumber']}"


def newest_date(episodes: list[dict], mark: datetime | None) -> datetime | None:
    """The new high-water mark: the newest DateCreated seen, never moving back."""
    dates = [d for d in (parse_date(e.get("DateCreated")) for e in episodes) if d]
    if mark:
        dates.append(mark)
    return max(dates) if dates else None


def _sort_key(ep: dict):
    return (ep.get("ParentIndexNumber") or 0, ep.get("IndexNumber") or 0, ep.get("Name") or "")


def pick_new(episodes: list[dict], known: set[str], now: datetime) -> list[list[dict]]:
    """New episodes ready to announce, one list per show, each in episode order.

    A show still being imported (its newest episode under SETTLE old) is held
    back for a later poll, so a season imported over a few minutes is one
    notification. MAX_HOLD stops a slow trickle from holding it forever.
    """
    by_show: dict[str, dict[str, dict]] = {}
    for ep in episodes:
        if ep.get("LocationType") == "Virtual" or not parse_date(ep.get("DateCreated")):
            continue
        key = episode_key(ep)
        if key in known:
            continue
        show = ep.get("SeriesId") or normalize(ep.get("SeriesName"))
        by_show.setdefault(show, {}).setdefault(key, ep)   # two files of one episode: one entry

    ready = []
    for eps in by_show.values():
        dates = [parse_date(e["DateCreated"]) for e in eps.values()]
        if now - max(dates) < SETTLE and now - min(dates) < MAX_HOLD:
            continue
        ready.append(sorted(eps.values(), key=_sort_key))
    return sorted(ready, key=lambda g: normalize(g[0].get("SeriesName")))


def episode_code(ep: dict) -> str:
    """S02E05, or S02E05–E06 for a two-part file."""
    season, number, end = ep.get("ParentIndexNumber") or 0, ep.get("IndexNumber"), ep.get("IndexNumberEnd")
    if number is None:
        return ep.get("Name") or "?"
    code = f"S{season:02d}E{number:02d}"
    return f"{code}–E{end:02d}" if end and end > number else code


def episode_range(episodes: list[dict]) -> str:
    """S02E01–E06; gaps and other seasons are listed: S01E10, S02E01–E03, E05."""
    by_season: dict[int, set[int]] = {}
    for ep in episodes:
        number = ep.get("IndexNumber")
        if number is None:
            continue
        end = max(ep.get("IndexNumberEnd") or number, number)
        by_season.setdefault(ep.get("ParentIndexNumber") or 0, set()).update(range(number, end + 1))

    parts = []
    for season in sorted(by_season):
        numbers = sorted(by_season[season])
        runs, start = [], numbers[0]
        for prev, cur in zip(numbers, numbers[1:] + [None]):
            if cur != prev + 1:
                runs.append((start, prev))
                start = cur
        for i, (a, b) in enumerate(runs):
            text = f"E{a:02d}" + (f"–E{b:02d}" if b > a else "")
            parts.append(f"S{season:02d}{text}" if i == 0 else text)
    return ", ".join(parts)


def headline(series_name: str, count: int) -> str:
    return f"A new episode of {series_name}" if count == 1 else f"{count} new episodes of {series_name}"


def subscribers(rows, series_id: str | None, series_name: str | None) -> list[tuple]:
    """Subscription rows (id, discord_user_id, show_name, show_id) for this show.

    By id when the row has one, by name as well: rows from before the
    autocomplete have no id, and a show added again gets a new one.
    """
    name = normalize(series_name)
    return [r for r in rows if (series_id and r[3] == series_id) or (name and normalize(r[2]) == name)]


# --- the database ------------------------------------------------------------

MARK_KEY = "episodes_seen_until"


async def load_mark(db) -> datetime | None:
    async with db.execute("SELECT value FROM bot_state WHERE key = ?", (MARK_KEY,)) as cursor:
        row = await cursor.fetchone()
    return parse_date(row[0]) if row else None


async def save_mark(db, mark: datetime) -> None:
    await db.execute("INSERT INTO bot_state (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (MARK_KEY, mark.astimezone(timezone.utc).isoformat()))


async def known_keys(db) -> set[str]:
    async with db.execute("SELECT episode_key FROM known_episodes") as cursor:
        return {r[0] for r in await cursor.fetchall()}


async def add_known(db, episodes: list[dict]) -> None:
    await db.executemany("INSERT OR IGNORE INTO known_episodes (episode_key) VALUES (?)",
                         [(episode_key(e),) for e in episodes])


async def subscribed(db, user_id: str, series_id: str | None, series_name: str | None) -> bool:
    """Whether this Discord member gets new-episode DMs for this show already."""
    async with db.execute("SELECT id, discord_user_id, show_name, show_id FROM subscriptions "
                          "WHERE discord_user_id = ?", (user_id,)) as cursor:
        return bool(subscribers(await cursor.fetchall(), series_id, series_name))


async def subscribe(db, user_id: str, username: str, series_name: str, series_id: str | None) -> bool:
    """Subscribe a member to a show, for /subscribe and the 🔔 offer button.
    True if they were not subscribed before, False if they were. Commits."""
    if await subscribed(db, user_id, series_id, series_name):
        return False
    try:
        await db.execute("""
            INSERT INTO subscriptions (discord_user_id, discord_username, show_name, show_id)
            VALUES (?, ?, ?, ?)
        """, (user_id, username, series_name, series_id))
        await db.commit()
    except sqlite3.IntegrityError:
        return False
    return True
