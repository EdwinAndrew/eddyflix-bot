"""Telling a new film or episode from a better copy of one the library had.

Sonarr and Radarr upgrade files: the old one (a WEB-DL, say) is deleted and the
better one (a Bluray) imported under a new name. Jellyfin then lists a new item
with a new id and a fresh DateCreated, so anything that goes by DateCreated
alone calls it new. The weekly "New on EddyFlix" card showed 15 upgraded
episodes of The Pitt as "+15 episodes".

So the bot keeps its own record, library_seen, of every film and episode it has
seen in the library, by identities that survive a new file, and the oldest
DateCreated it saw for each:

- an episode: its show (TVDB, TMDB and IMDb ids, and its name) with the season
  and episode number, "episode:tvdb:403245:1:5"; a two-part file is both numbers
- a film: its TMDB and IMDb ids, and its name with the year, "movie:tmdb:693134"

An item has several, one row each, and matches if any of them is known, so a
show that gets a TVDB id after its first episodes still matches. Never Jellyfin
item ids: an upgrade gets a new one.

An item added in a window is an upgrade when its identity was first seen before
the window started; one first seen inside it is new, whatever happened to its
file since. sync() keeps the record up to date: the whole library once, on the
first run (paged, 500 at a time), then what was added since the last look.

On the first run the record cannot know which of this week's files replaced
older ones, except for episodes: the new-episode DMs have been noting episodes
in known_episodes as they arrived, in order (see backfill).
"""
import asyncio
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone

from new_episodes import episode_key, parse_date

SEED_PAGE = 500                   # items per request on the first run
PAGE = 100                        # ...and afterwards
MAX_PAGES = 200                   # 100,000 items: a broken listing should not page forever
LOOKBACK = timedelta(hours=3)     # Jellyfin stamps DateCreated before an item is listed (new_episodes.py)
GAP = timedelta(hours=1)          # a file this long after its identity was first seen is a replacement
HOLD = timedelta(hours=2)         # the most a new episode waited before the DM poll noted it (see backfill)
LOOKUP_CHUNK = 500                # identities per SQL query (SQLite allows 32,766 parameters)

MARK_KEY = "library_seen_until"   # bot_state: the newest DateCreated synced
HISTORY_KEY = "library_seen_history"   # bot_state: known_episodes' last rowid before the first sync

PROVIDERS = ("tvdb", "tmdb", "imdb")

_lock = asyncio.Lock()            # one sync at a time: the hourly one, the weekly post's, the summary's


# --- identities ------------------------------------------------------------------

def name_key(name: str | None) -> str:
    """A name for matching: no case, accents or punctuation. Unlike normalize(), a
    trailing "(US)" or "(2019)" stays, so Ghosts and Ghosts (US) are two shows."""
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


def provider_keys(item: dict) -> list[str]:
    """"tvdb:403245", "tmdb:…", "imdb:tt…" for the ids Jellyfin has for it."""
    ids = {str(k).casefold(): str(v).strip() for k, v in (item.get("ProviderIds") or {}).items() if v}
    return [f"{p}:{ids[p]}" for p in PROVIDERS if ids.get(p)]


def series_keys(series: dict) -> list[str]:
    """A show's identities: its provider ids, and its name."""
    keys = provider_keys(series)
    if name_key(series.get("Name")):
        keys.append(f"name:{name_key(series.get('Name'))}")
    return keys


def episode_identities(ep: dict, shows: dict[str, list[str]]) -> list[str]:
    """An episode's identities. shows maps series ids to series_keys(); a show not
    in it is known by the episode's SeriesName. None for an episode without a
    number: there is nothing to know it by but the item id, which an upgrade changes."""
    number = ep.get("IndexNumber")
    if number is None:
        return []
    show = shows.get(ep.get("SeriesId") or "")
    if not show:
        show = [f"name:{name_key(ep.get('SeriesName'))}"] if name_key(ep.get("SeriesName")) else []
    season = ep.get("ParentIndexNumber") or 0
    end = max(ep.get("IndexNumberEnd") or number, number)
    return [f"episode:{s}:{season}:{n}" for s in show for n in range(number, end + 1)]


def movie_identities(movie: dict) -> list[str]:
    keys = [f"movie:{k}" for k in provider_keys(movie)]
    if name_key(movie.get("Name")):
        keys.append(f"movie:name:{name_key(movie.get('Name'))}:{movie.get('ProductionYear') or ''}")
    return keys


def identities(item: dict, shows: dict[str, list[str]]) -> list[str]:
    """Identities of a film or an episode (by Type, or by its having a SeriesId
    when Type is missing). Nothing for anything else."""
    kind = item.get("Type")
    if kind == "Episode" or (kind is None and "SeriesName" in item):
        return episode_identities(item, shows)
    if kind == "Movie" or kind is None:
        return movie_identities(item)
    return []


def stamp(when: datetime) -> str:
    """first_seen as stored: UTC, always to the microsecond, so text order is time order."""
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def first_seen(ids: list[str], seen: dict[str, datetime]) -> datetime | None:
    """When any of these identities was first seen, or None if none was."""
    known = [seen[i] for i in ids if i in seen]
    return min(known) if known else None


def upgraded_since(ids: list[str], seen: dict[str, datetime], since: datetime) -> bool:
    """For a window starting at since: an item whose identity the library had
    before that is a better copy, not something new. An item with no identity
    is new, as before."""
    first = first_seen(ids, seen)
    return first is not None and first < since


def is_replacement(item: dict, ids: list[str], seen: dict[str, datetime]) -> bool:
    """Whether this file replaced an earlier one: its identity was first seen
    well (GAP) before the file was added. Two files of one episode added
    together are not replacements of each other."""
    added = parse_date(item.get("DateCreated"))
    first = first_seen(ids, seen)
    return added is not None and first is not None and first < added - GAP


def backfill(episodes: list[dict], rowids: dict[str, int], hold: timedelta = HOLD) -> dict[str, datetime]:
    """For the first sync: the latest each episode can have first been in the
    library, from known_episodes, by episode_key.

    The new-episode poll adds an episode to known_episodes within about an hour
    (new_episodes.MAX_HOLD) of its file arriving, so a row's rowid gives its
    order of arrival. A row added after a file that is still in the library came
    no later than that file's DateCreated plus hold, so neither did any row
    before it. An upgraded episode keeps its old row, among old files, and comes
    out old; an episode new this week sits among this week's and stays new.
    Only rows from before this version ran count (HISTORY_KEY), and episodes
    without a row keep their own DateCreated.
    """
    added: dict[str, datetime] = {}
    for ep in episodes:
        d = parse_date(ep.get("DateCreated"))
        if d is None or ep.get("IndexNumber") is None:
            continue
        key = episode_key(ep)
        added[key] = min(added.get(key, d), d)
    bound, out = None, {}
    for _, key in sorted(((rowids[k], k) for k in added if k in rowids), reverse=True):
        latest = added[key] + hold
        bound = latest if bound is None else min(bound, latest)
        out[key] = min(added[key], bound)
    return out


# --- the database ----------------------------------------------------------------

async def create_tables(db) -> None:
    """The table, and, before the first sync, how far known_episodes went in earlier
    runs: rows the DM poll adds from now on are no history. Idempotent; called by
    watch_data.init_database before any poll starts."""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS library_seen (
            identity TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            first_seen TEXT NOT NULL
        )
    """)
    await db.execute("""
        INSERT OR IGNORE INTO bot_state (key, value)
        SELECT ?, COALESCE(MAX(rowid), 0) FROM known_episodes
        WHERE NOT EXISTS (SELECT 1 FROM bot_state WHERE key = ?)
    """, (HISTORY_KEY, MARK_KEY))


async def lookup(db, ids) -> dict[str, datetime]:
    """first_seen for each of these identities the record has."""
    ids, found = list(dict.fromkeys(ids)), {}
    for i in range(0, len(ids), LOOKUP_CHUNK):
        chunk = ids[i:i + LOOKUP_CHUNK]
        marks = ",".join("?" * len(chunk))
        async with db.execute(f"SELECT identity, first_seen FROM library_seen WHERE identity IN ({marks})",
                              chunk) as cursor:
            for identity, when in await cursor.fetchall():
                if (parsed := parse_date(when)) is not None:
                    found[identity] = parsed
    return found


async def record(db, items: list[tuple[list[str], str, datetime]]) -> int:
    """Note items, each (identities, kind, when added). An item's identities all
    get the oldest date known for any of them, so they stay one item. Returns how
    many were not known before. Safe to repeat. Does not commit."""
    seen = await lookup(db, (i for ids, _, _ in items for i in ids))
    rows, fresh = {}, 0
    for ids, kind, added in items:
        if not ids:
            continue
        known = first_seen(ids, seen)
        if known is None:
            fresh += 1
        first = min(known, added) if known else added
        for i in ids:
            seen[i] = first
            rows[i] = (i, kind, stamp(first))
    await db.executemany("""
        INSERT INTO library_seen (identity, kind, first_seen) VALUES (?, ?, ?)
        ON CONFLICT(identity) DO UPDATE SET first_seen = MIN(first_seen, excluded.first_seen)
    """, list(rows.values()))
    return fresh


async def _state(db, key: str) -> str | None:
    async with db.execute("SELECT value FROM bot_state WHERE key = ?", (key,)) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else None


async def _save_mark(db, mark: datetime) -> None:
    await db.execute("INSERT INTO bot_state (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (MARK_KEY, stamp(mark)))


async def seeded(db) -> bool:
    return await _state(db, MARK_KEY) is not None


async def history(db) -> dict[str, int]:
    """known_episodes rows from before this version ran: episode_key -> rowid."""
    cap = await _state(db, HISTORY_KEY)
    if not cap or not str(cap).isdigit() or int(cap) <= 0:
        return {}
    async with db.execute("SELECT episode_key, rowid FROM known_episodes WHERE rowid <= ?", (int(cap),)) as cursor:
        return {k: r for k, r in await cursor.fetchall()}


# --- asking Jellyfin ---------------------------------------------------------------

async def _pages(fetch, page: int, since: datetime | None) -> list[dict]:
    """A newest-first listing, all of it (since None) or back to since."""
    found = []
    for n in range(MAX_PAGES):
        batch = await fetch(n * page, page)
        found += batch
        if len(batch) < page:
            break
        oldest = parse_date(batch[-1].get("DateCreated"))
        if since is not None and (oldest is None or oldest <= since):
            break
    if since is None:
        return found
    return [i for i in found if (d := parse_date(i.get("DateCreated"))) and d > since]


async def show_keys(jf, episodes: list[dict], shows: dict[str, list[str]] | None = None) -> dict[str, list[str]]:
    """series_keys() for the shows of these episodes: from shows where given,
    from Jellyfin (one request per 50 shows) for the rest."""
    shows = dict(shows or {})
    wanted = sorted({e["SeriesId"] for e in episodes if e.get("SeriesId") and e["SeriesId"] not in shows})
    for i in range(0, len(wanted), 50):
        for s in await jf.provider_ids(wanted[i:i + 50]):
            if s.get("Id"):
                shows[s["Id"]] = series_keys(s)
    return shows


async def identify(jf, items: list[dict], shows: dict[str, list[str]] | None = None) -> dict[str, list[str]]:
    """Identities of films and episodes, by item id. Films need ProviderIds in the
    listing (added_by_date has them); episodes' shows are asked for."""
    episodes = [i for i in items if i.get("Type") == "Episode" or (i.get("Type") is None and "SeriesName" in i)]
    known = await show_keys(jf, episodes, shows) if episodes else dict(shows or {})
    return {i["Id"]: identities(i, known) for i in items if i.get("Id")}


async def upgrades(db, jf, items: list[dict], since: datetime,
                   shows: dict[str, list[str]] | None = None) -> set[str]:
    """Ids of the items, all added after since, that are better copies of a film or
    episode the library had before since. Notes them too, so a film first seen
    here is known next week."""
    ids = await identify(jf, items, shows)
    seen = await lookup(db, (i for v in ids.values() for i in v))
    found = {item_id for item_id, v in ids.items() if upgraded_since(v, seen, since)}
    await record(db, [(ids[i["Id"]], _kind(i), parse_date(i.get("DateCreated")) or datetime.now(timezone.utc))
                      for i in items if i.get("Id") in ids])
    return found


def _kind(item: dict) -> str:
    return "episode" if item.get("Type") == "Episode" or "SeriesName" in item else "movie"


async def sync(db, jf, now: datetime | None = None) -> int:
    """Bring the record up to date: everything on the first run, what was added
    since the last look (less LOOKBACK) after that. Returns how many films and
    episodes were new to it. Commits."""
    async with _lock:
        now = now or datetime.now(timezone.utc)
        mark = parse_date(await _state(db, MARK_KEY))
        first_run = mark is None
        since = None if first_run else mark - LOOKBACK
        page = SEED_PAGE if first_run else PAGE
        started = time.monotonic()

        films = [f for f in await _pages(lambda s, n: jf.added_by_date("Movie", s, n), page, since)
                 if f.get("LocationType") != "Virtual"]
        episodes = [e for e in await _pages(jf.episodes_by_date_added, page, since)
                    if e.get("LocationType") != "Virtual"]
        if first_run:
            # Every show at once, rather than asking for them 50 at a time.
            series = await _pages(lambda s, n: jf.added_by_date("Series", s, n), page, None)
            shows = await show_keys(jf, episodes, {s["Id"]: series_keys(s) for s in series if s.get("Id")})
        else:
            shows = await show_keys(jf, episodes)

        earliest = backfill(episodes, await history(db)) if first_run else {}
        items = []
        for f in films:
            if added := parse_date(f.get("DateCreated")):
                items.append((movie_identities(f), "movie", added))
        for e in episodes:
            if added := parse_date(e.get("DateCreated")):
                if e.get("IndexNumber") is not None:
                    added = min(added, earliest.get(episode_key(e), added))
                items.append((episode_identities(e, shows), "episode", added))
        fresh = await record(db, items)

        dates = [d for d in (parse_date(i.get("DateCreated")) for i in films + episodes) if d]
        new_mark = max(dates + ([mark] if mark else [])) if dates or mark else now
        await _save_mark(db, new_mark)
        await db.commit()
        if first_run:
            older = sum(1 for e in episodes if (d := parse_date(e.get("DateCreated")))
                        and earliest.get(episode_key(e), d) < d)
            print(f"Library record: noted {len(films)} films and {len(episodes)} episodes in "
                  f"{time.monotonic() - started:.1f}s ({older} episodes dated earlier from known_episodes)")
        return fresh
