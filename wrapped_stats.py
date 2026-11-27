"""EddyFlix Wrapped: the stats for one person, or the whole server, over a period (#65).

    stats = await wrapped_stats(db, "eddy", *year(2026))     # one Jellyfin user
    stats = await wrapped_stats(db, None, *month(2027, 1))   # the server
    stats.to_dict()                                          # plain JSON types

Pure: it reads the bot's own database (an aiosqlite or sqlite3 connection) and
returns a WrappedStats. No Discord, no images, no Jellyfin calls; the cards
(#66, #68), delivery (#67) and scheduling (#69) build on it. The same database
and arguments always give the same result: every list has a stable order and
every tie a stated tie-break.

Time. watch_history holds naive Toronto wall-clock times (the container runs
with TZ=America/Toronto and records datetime.now()), so days, hours and months
are Toronto ones. movie_nights is UTC, library_seen is UTC with an offset, and
achievements come in both shapes (see cogs/achievements.earned_unix); all are
turned into Toronto time here. A play belongs to the period, day and month it
started in. Periods are [start, end): dates or datetimes, naive = Toronto.

From plays to watch time, in order:

1. Only films (Movie) and episodes (Episode) count. Other types, plays never
   closed (no end_time) and plays with no duration (0 or none: close_orphans
   closes those it could not size at their start) are left out and counted in
   coverage.excluded.
2. Plays of the same title by the same person that overlap are one session
   (an old bug recorded some twice). A title is its show, season and episode,
   or a film's name, not the Jellyfin item id, which an upgraded file changes.
3. A session counts at most the title's runtime when the caller passes it
   (runtimes, by item id, e.g. from RunTimeTicks); otherwise at most
   EPISODE_CEILING or FILM_CEILING, so a play left paused overnight is not a
   ten-hour episode. The database does not record runtimes.
4. Sessions shorter than SHORT_PLAY (3 minutes) are browsing, and left out.
5. Sessions of one title by one person make one viewing until it is complete:
   with a known runtime, once COMPLETE_SHARE of it has been watched; without,
   a session REWATCH_GAP or more after the last one starts a new viewing.
   A viewing counts at most the runtime. Viewings are "plays" in the top films
   and in most rewatched; stopping and resuming the next day is one play.

Every other stat is defined where it is computed below.

Genres: not available. watch_history does not record them and the engine makes
no Jellyfin calls. TODO(#65): add top genres once genres are stored, e.g. from
NowPlayingItem.Genres in watch_data.record_watch_start.
"""
import asyncio
import sqlite3
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from library_seen import name_key, stamp
from new_episodes import parse_date

TZ = ZoneInfo("America/Toronto")

MIN_HOURS = 2.0                          # below this in the period: eligible = False
SHORT_PLAY = timedelta(minutes=3)        # shorter sessions are browsing
SITTING_GAP = timedelta(minutes=30)      # binge: next episode within this of the last one ending
REWATCH_GAP = timedelta(days=3)          # runtime unknown: a new viewing after this long
COMPLETE_SHARE = 0.7                     # runtime known: a viewing is complete at this share
EPISODE_CEILING = timedelta(hours=2)     # the most one session counts, runtime unknown
FILM_CEILING = timedelta(hours=4)
ATTEND_MINUTES = 15                      # same as screenings.ATTEND_MINUTES (tested), no discord import
TOP_SHOWS = 5
TOP_FILMS = 3
FUN_FACTS = 3


@dataclass(frozen=True)
class Gap:
    start: date          # first day missing
    end: date            # last day missing, inclusive
    reason: str


# Days the bot recorded nothing. Add a line for any new outage.
KNOWN_GAPS = (
    Gap(date(2026, 10, 12), date(2026, 10, 18), "The bot was getting 401s from Jellyfin"),
)
# History before this day comes from the gaming PC, which recorded only while it was on.
PARTIAL_BEFORE = date(2026, 10, 12)
PARTIAL_NOTE = "Before 12 Oct 2026 EddyFlix ran on a gaming PC that only recorded plays while it was on"

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


# --- the result ------------------------------------------------------------------

@dataclass(frozen=True)
class Period:
    start: date          # first day
    end: date            # the day after the last
    kind: str            # "year", "month" or "custom"
    label: str           # "2026", "January 2027", or "2026-11-01 to 2026-11-14"


@dataclass(frozen=True)
class WatchTime:
    total_seconds: int
    film_seconds: int
    episode_seconds: int
    total_hours: float
    film_hours: float
    episode_hours: float
    plays: int           # viewings, all titles
    films: int           # distinct films
    episodes: int        # distinct episodes


@dataclass(frozen=True)
class ShowStat:
    show: str
    seconds: int
    hours: float
    episodes: int        # distinct episodes
    plays: int


@dataclass(frozen=True)
class FilmStat:
    title: str
    plays: int
    seconds: int
    hours: float


@dataclass(frozen=True)
class Rewatch:
    kind: str            # "film" or "episode"
    title: str           # the film, or the episode's name
    show: str | None
    season: int | None
    episode: int | None
    plays: int           # viewings; server: by everyone who watched it twice or more
    rewatches: int       # plays after the first; server: summed over people
    seconds: int


@dataclass(frozen=True)
class Binge:
    show: str
    episodes: int
    seconds: int
    kind: str            # "sitting" (gaps under 30 min) or "day" (one Toronto day)
    start: datetime
    end: datetime
    day: date            # the day it started
    crosses_midnight: bool


@dataclass(frozen=True)
class Busiest:
    weekday: str         # most watched-time
    hour: int            # 0-23
    slot: str            # e.g. "Sunday night"; night runs 21:00-05:00 and belongs to the evening it began
    by_weekday: list[int]    # seconds, Monday first
    by_hour: list[int]       # seconds, midnight first


@dataclass(frozen=True)
class Streak:
    days: int
    start: date
    end: date


@dataclass(frozen=True)
class ShowFinished:
    show: str
    episodes: int | None     # per person: episodes watched when the bot awarded it
    finished_on: date


@dataclass(frozen=True)
class NewInPeriod:
    films: list[str]         # films watched that the library first had in the period
    episodes: int            # episodes watched that the library first had in the period
    shows: list[str]         # the shows of those episodes


@dataclass(frozen=True)
class MovieNight:
    title: str
    day: date


@dataclass(frozen=True)
class MovieNights:
    count: int               # per person: attended; server: held
    films: list[MovieNight]


@dataclass(frozen=True)
class Persona:
    key: str
    label: str
    reason: str


@dataclass(frozen=True)
class FunFact:
    key: str
    text: str
    value: int | float | str


@dataclass(frozen=True)
class Coverage:
    first_day: date | None   # first and last day with counted plays in the period
    last_day: date | None
    days_with_data: int
    known_gaps: list[Gap]    # KNOWN_GAPS overlapping the period, clipped to it
    partial_before: date | None   # set when the period starts before PARTIAL_BEFORE
    notes: list[str]
    complete: bool           # no gap and nothing partial in the period
    excluded: dict[str, int]
    runtime_known_share: float    # share of counted sessions capped by a real runtime


@dataclass(frozen=True)
class ServerExtras:
    watchers: int                 # distinct Jellyfin users with a counted play
    library_added_films: int      # library_seen: first seen in the period
    library_added_episodes: int
    movie_nights_held: int
    movie_night_attendances: int  # people per night, summed
    shows_finished: int           # completions, a show finished by two people twice


@dataclass(frozen=True)
class WrappedStats:
    scope: str                    # "user" or "server"
    user: str | None              # the Jellyfin user; None for the server
    period: Period
    eligible: bool
    min_hours: float
    watch_time: WatchTime
    top_shows: list[ShowStat]
    top_films: list[FilmStat]
    most_rewatched: Rewatch | None
    longest_binge: Binge | None   # always None for the server: a binge is one person's
    busiest: Busiest | None
    longest_streak: Streak | None
    shows_finished: list[ShowFinished]
    shows_started: list[str]
    new_this_period: NewInPeriod
    movie_nights: MovieNights
    persona: Persona | None       # None for the server, and without plays
    fun_facts: list[FunFact]
    coverage: Coverage
    server: ServerExtras | None = None

    def to_dict(self) -> dict:
        """Plain JSON types: dates and times as ISO strings."""
        return _plain(asdict(self))


def _plain(value):
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if is_dataclass(value):
        return _plain(asdict(value))
    return value


# --- periods -----------------------------------------------------------------------

def year(y: int) -> tuple[date, date]:
    return date(y, 1, 1), date(y + 1, 1, 1)


def month(y: int, m: int) -> tuple[date, date]:
    return date(y, m, 1), date(y + (m == 12), m % 12 + 1, 1)


def local(value: date | datetime) -> datetime:
    """A naive Toronto datetime: a date is its midnight, an aware time is converted."""
    if isinstance(value, datetime):
        return value.astimezone(TZ).replace(tzinfo=None) if value.tzinfo else value
    return datetime.combine(value, time())


def _period(start: datetime, end: datetime) -> Period:
    s, e = start.date(), end.date()
    whole = start.time() == time() and end.time() == time()
    if whole and (s.month, s.day) == (1, 1) and e == date(s.year + 1, 1, 1):
        return Period(s, e, "year", str(s.year))
    if whole and s.day == 1 and (s, e) == month(s.year, s.month):
        return Period(s, e, "month", s.strftime("%B %Y"))
    last = (end - timedelta(microseconds=1)).date()
    return Period(s, e if whole else last + timedelta(days=1), "custom", f"{s} to {last}")


def _from_utc(value) -> datetime | None:
    """A stored UTC time (naive or with an offset) as naive Toronto time."""
    if value in (None, ""):
        return None
    parsed = parse_date(str(value))
    return parsed.astimezone(TZ).replace(tzinfo=None) if parsed else None


def _earned(value) -> datetime | None:
    """An achievement's earned_at: CURRENT_TIMESTAMP (UTC, a space) or the bot's
    isoformat() (Toronto, a "T"), as in cogs/achievements.earned_unix."""
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        if "T" in str(value):
            return parsed
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(TZ).replace(tzinfo=None)


def _hours(seconds: float) -> float:
    return round(seconds / 3600, 1)


# --- reading the database -------------------------------------------------------------

async def _rows(db, sql: str, params=()) -> list[tuple]:
    if isinstance(db, sqlite3.Connection):
        return db.execute(sql, params).fetchall()
    async with db.execute(sql, params) as cursor:
        return list(await cursor.fetchall())


def _text(when: datetime) -> str:
    return when.isoformat(" ")


PLAY_COLUMNS = ("id, user_name, item_id, item_name, item_type, series_name, season_number, "
                "episode_number, start_time, end_time, duration_seconds")


async def _load_plays(db, user: str | None, start: datetime, end: datetime) -> list[tuple]:
    # A day either side in SQL, in case a row was ever stored with a "T"; exact in Python.
    params = [_text(start - timedelta(days=1)), _text(end + timedelta(days=1))]
    who = ""
    if user is not None:
        who = " AND lower(user_name) = lower(?)"
        params.append(user)
    return await _rows(db, f"SELECT {PLAY_COLUMNS} FROM watch_history "
                           f"WHERE start_time >= ? AND start_time < ?{who}", params)


# --- plays into sessions and viewings -----------------------------------------------------

@dataclass
class _Session:
    user: str            # lower case; never in the server's output
    key: str
    kind: str            # "film" or "episode"
    item_id: str
    title: str
    show_key: str | None
    show: str | None
    season: int | None
    episode: int | None
    start: datetime
    end: datetime        # when it really stopped
    watched: int         # seconds counted
    runtime: int | None  # seconds, when known


@dataclass
class _Viewing:
    sessions: list[_Session] = field(default_factory=list)

    @property
    def watched(self) -> int:
        return sum(s.watched for s in self.sessions)


def title_key(item_type, item_name, series_name, season, episode, item_id) -> str:
    """A title that survives an upgraded file (see the module docstring)."""
    if item_type == "Episode":
        if name_key(series_name) and episode is not None:
            return f"episode:{name_key(series_name)}:{season or 0}:{episode}"
        return f"item:{item_id}"
    return f"film:{name_key(item_name)}" if name_key(item_name) else f"item:{item_id}"


def _parse_time(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def build_sessions(rows, start: datetime, end: datetime, runtimes: dict[str, int] | None,
                   excluded: Counter) -> tuple[list[_Session], dict[tuple[str, str], list[_Viewing]]]:
    """Steps 1-5 of the module docstring. Returns the counted sessions, in time
    order, and the viewings by (user, title)."""
    runtimes = runtimes or {}
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for (row_id, user, item_id, item_name, item_type, series, season, episode,
         started, ended, duration) in rows:
        began = _parse_time(started)
        if began is None or not start <= began < end:
            continue
        if item_type not in ("Movie", "Episode"):
            excluded["other_types"] += 1
            continue
        if ended is None:
            excluded["unclosed"] += 1
            continue
        stop = _parse_time(ended)
        seconds = duration if duration is not None else (
            int((stop - began).total_seconds()) if stop else 0)
        if not seconds or seconds <= 0:
            excluded["no_duration"] += 1
            continue
        key = title_key(item_type, item_name, series, season, episode, item_id)
        groups[(user.lower(), key)].append(
            (began, began + timedelta(seconds=seconds), row_id, item_id, item_type, item_name,
             series, season, episode))

    sessions: list[_Session] = []
    viewings: dict[tuple[str, str], list[_Viewing]] = {}
    for (user, key), plays in sorted(groups.items()):
        plays.sort(key=lambda p: (p[0], p[1], p[2]))
        merged: list[list] = []
        for p in plays:
            if merged and p[0] < merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], p[1])
                merged[-1][2].append(p[3])
                excluded["duplicates_merged"] += 1
            else:
                merged.append([p[0], p[1], [p[3]], p])
        counted = []
        for began, stopped, item_ids, first in merged:
            _, _, _, item_id, item_type, item_name, series, season, episode = first
            runtime = next((int(runtimes[i]) for i in item_ids if runtimes.get(i)), None)
            kind = "episode" if item_type == "Episode" else "film"
            ceiling = runtime or int((EPISODE_CEILING if kind == "episode" else FILM_CEILING).total_seconds())
            length = int((stopped - began).total_seconds())
            if length > ceiling:
                excluded["capped"] += 1
            watched = min(length, ceiling)
            if watched < SHORT_PLAY.total_seconds():
                excluded["short"] += 1
                continue
            counted.append(_Session(
                user=user, key=key, kind=kind, item_id=item_id,
                title=item_name or "Unknown", show_key=name_key(series) or None if kind == "episode" else None,
                show=series if kind == "episode" else None,
                season=season if kind == "episode" else None, episode=episode if kind == "episode" else None,
                start=began, end=stopped, watched=watched, runtime=runtime))

        views: list[_Viewing] = []
        for s in counted:
            current = views[-1] if views else None
            if current is None:
                new = True
            elif s.runtime:
                new = current.watched >= COMPLETE_SHARE * s.runtime
            else:
                new = s.start - current.sessions[-1].end >= REWATCH_GAP
            if new:
                views.append(_Viewing([s]))
                continue
            if s.runtime:
                s.watched = min(s.watched, max(0, s.runtime - current.watched))
                if s.watched <= 0:
                    excluded["beyond_runtime"] += 1
                    continue
            current.sessions.append(s)
        if views:
            viewings[(user, key)] = views
            sessions.extend(s for v in views for s in v.sessions)
    sessions.sort(key=lambda s: (s.start, s.user, s.key))
    return sessions, viewings


# --- the stats -------------------------------------------------------------------------

def _show_names(sessions: list[_Session]) -> dict[str, str]:
    """The name to show for each show: the one used most, then the latest, then A-Z."""
    seen: dict[str, Counter] = defaultdict(Counter)
    for s in sessions:
        if s.show_key:
            seen[s.show_key][s.show] += 1
    return {k: sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] for k, c in seen.items()}


def _film_names(sessions: list[_Session]) -> dict[str, str]:
    seen: dict[str, Counter] = defaultdict(Counter)
    for s in sessions:
        if s.kind == "film":
            seen[s.key][s.title] += 1
    return {k: sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] for k, c in seen.items()}


def watch_time(sessions, viewings) -> WatchTime:
    films = sum(s.watched for s in sessions if s.kind == "film")
    episodes = sum(s.watched for s in sessions if s.kind == "episode")
    return WatchTime(
        total_seconds=films + episodes, film_seconds=films, episode_seconds=episodes,
        total_hours=_hours(films + episodes), film_hours=_hours(films), episode_hours=_hours(episodes),
        plays=sum(len(v) for v in viewings.values()),
        films=len({s.key for s in sessions if s.kind == "film"}),
        episodes=len({s.key for s in sessions if s.kind == "episode"}))


def top_shows(sessions, viewings, names) -> list[ShowStat]:
    """By time watched, then distinct episodes, then name."""
    seconds, episodes, plays = Counter(), defaultdict(set), Counter()
    for s in sessions:
        if s.show_key:
            seconds[s.show_key] += s.watched
            episodes[s.show_key].add(s.key)
    for (_, key), views in viewings.items():
        if views[0].sessions[0].show_key:
            plays[views[0].sessions[0].show_key] += len(views)
    order = sorted(seconds, key=lambda k: (-seconds[k], -len(episodes[k]), names[k], k))
    return [ShowStat(names[k], seconds[k], _hours(seconds[k]), len(episodes[k]), plays[k])
            for k in order[:TOP_SHOWS]]


def top_films(sessions, viewings, names) -> list[FilmStat]:
    """By plays (viewings), then time watched, then title."""
    seconds, plays = Counter(), Counter()
    for s in sessions:
        if s.kind == "film":
            seconds[s.key] += s.watched
    for (_, key), views in viewings.items():
        if views[0].sessions[0].kind == "film":
            plays[key] += len(views)
    order = sorted(seconds, key=lambda k: (-plays[k], -seconds[k], names[k], k))
    return [FilmStat(names[k], plays[k], seconds[k], _hours(seconds[k])) for k in order[:TOP_FILMS]]


def most_rewatched(viewings, film_names, show_names) -> Rewatch | None:
    """The title with the most plays after the first, if any: per person, a title
    played twice or more; on the server, those plays summed over everyone who
    played it twice or more. Ties: more plays, more time, then title."""
    plays, rewatches, seconds, sample = Counter(), Counter(), Counter(), {}
    for (user, key), views in viewings.items():
        if len(views) < 2:
            continue
        plays[key] += len(views)
        rewatches[key] += len(views) - 1
        seconds[key] += sum(v.watched for v in views)
        sample.setdefault(key, views[0].sessions[0])
    if not rewatches:
        return None

    def title(k):
        s = sample[k]
        return film_names.get(k, s.title) if s.kind == "film" else s.title

    best = min(rewatches, key=lambda k: (-rewatches[k], -plays[k], -seconds[k], title(k), k))
    s = sample[best]
    return Rewatch(kind=s.kind, title=title(best),
                   show=show_names.get(s.show_key) if s.show_key else None,
                   season=s.season, episode=s.episode,
                   plays=plays[best], rewatches=rewatches[best], seconds=seconds[best])


def longest_binge(sessions, names) -> Binge | None:
    """The most distinct episodes of one show, either in one sitting (each episode
    starting less than SITTING_GAP after the previous one stopped, so a sitting
    can run past midnight) or on one Toronto day (by start). Ties: more time,
    then the earlier start, then "day" before "sitting", then the show."""
    by_show: dict[str, list[_Session]] = defaultdict(list)
    for s in sessions:
        if s.show_key:
            by_show[s.show_key].append(s)
    candidates = []
    for show, plays in by_show.items():
        plays.sort(key=lambda s: (s.start, s.key))
        days: dict[date, list[_Session]] = defaultdict(list)
        for s in plays:
            days[s.start.date()].append(s)
        groups = [("day", g) for g in days.values()]
        run = [plays[0]]
        for s in plays[1:]:
            if s.start - max(p.end for p in run) < SITTING_GAP:
                run.append(s)
            else:
                groups.append(("sitting", run))
                run = [s]
        groups.append(("sitting", run))
        for kind, g in groups:
            first, last = g[0].start, max(p.end for p in g)
            candidates.append((-len({p.key for p in g}), -sum(p.watched for p in g), first, kind, names[show],
                               last, show))
    if not candidates:
        return None
    eps, secs, first, kind, name, last, _ = min(candidates)
    return Binge(show=name, episodes=-eps, seconds=-secs, kind=kind, start=first, end=last,
                 day=first.date(), crosses_midnight=last.date() > first.date() and last.time() != time())


def _spread(sessions) -> tuple[list[int], list[int], Counter]:
    """Watched seconds by weekday, by hour and by (evening, daypart), spread over
    the hours each session ran."""
    weekday, hour, slots = [0] * 7, [0] * 24, Counter()
    for s in sessions:
        at, left = s.start, s.watched
        while left > 0:
            nxt = at.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            chunk = min(left, int((nxt - at).total_seconds()) or 3600)
            weekday[at.weekday()] += chunk
            hour[at.hour] += chunk
            slots[_slot(at)] += chunk
            left -= chunk
            at = nxt
    return weekday, hour, slots


def _slot(at: datetime) -> tuple[int, str]:
    """(weekday, daypart). Night is 21:00 to 05:00 and belongs to the evening it
    began: 1 AM on a Monday is Sunday night."""
    h = at.hour
    if h < 5:
        return (at.weekday() - 1) % 7, "night"
    if h < 12:
        return at.weekday(), "morning"
    if h < 17:
        return at.weekday(), "afternoon"
    if h < 21:
        return at.weekday(), "evening"
    return at.weekday(), "night"


def busiest(sessions) -> Busiest | None:
    """Most watched time; ties go to the earlier weekday, hour or slot."""
    if not sessions:
        return None
    weekday, hour, slots = _spread(sessions)
    day = max(range(7), key=lambda d: (weekday[d], -d))
    top = max(range(24), key=lambda h: (hour[h], -h))
    order = ("morning", "afternoon", "evening", "night")
    slot = max(slots, key=lambda k: (slots[k], -k[0], -order.index(k[1])))
    return Busiest(weekday=WEEKDAYS[day], hour=top, slot=f"{WEEKDAYS[slot[0]]} {slot[1]}",
                   by_weekday=weekday, by_hour=hour)


def longest_streak(sessions) -> Streak | None:
    """The most Toronto days in a row with a counted play starting; the earliest
    such run on a tie. A known gap breaks a streak: there is no data for it."""
    days = sorted({s.start.date() for s in sessions})
    if not days:
        return None
    best = run = (days[0], days[0])
    for d in days[1:]:
        run = (run[0], d) if d - run[1] == timedelta(days=1) else (d, d)
        if (run[1] - run[0]) > (best[1] - best[0]):
            best = run
    return Streak((best[1] - best[0]).days + 1, best[0], best[1])


async def shows_finished(db, user: str | None, start: datetime, end: datetime) -> tuple[list[ShowFinished], int]:
    """The bot's own "Finished" achievements (every episode in the library
    watched, checked against Jellyfin since #24) earned in the period. The
    server lists each show once, at its first completion, and counts all of them."""
    params, who = [], ""
    if user is not None:
        who, params = " AND lower(user_name) = lower(?)", [user]
    rows = await _rows(db, "SELECT item_name, value, earned_at FROM achievements "
                           f"WHERE achievement_type = 'series_completion'{who}", params)
    done = sorted((when, name or "", value) for name, value, raw in rows
                  if (when := _earned(raw)) is not None and start <= when < end)
    shown, out = set(), []
    for when, name, value in done:
        if name_key(name) in shown:
            continue
        shown.add(name_key(name))
        out.append(ShowFinished(name, value if user is not None else None, when.date()))
    return out, len(done)


async def shows_started(db, user: str | None, start: datetime, sessions, names) -> list[str]:
    """Shows with a counted episode in the period and no earlier play of SHORT_PLAY
    or more (by the person; for the server, by anyone), in order of the first
    one. Plays from before PARTIAL_BEFORE are incomplete, so some may have
    started earlier than the bot saw."""
    params, who = [_text(start), int(SHORT_PLAY.total_seconds())], ""
    if user is not None:
        who = " AND lower(user_name) = lower(?)"
        params.append(user)
    rows = await _rows(db, "SELECT DISTINCT series_name FROM watch_history WHERE item_type = 'Episode' "
                           f"AND start_time < ? AND duration_seconds >= ?{who}", params)
    before = {name_key(r[0]) for r in rows if r[0]}
    started, out = set(), []
    for s in sessions:
        if s.show_key and s.show_key not in before and s.show_key not in started:
            started.add(s.show_key)
            out.append(names[s.show_key])
    return out


async def new_this_period(db, start: datetime, end: datetime, sessions, names
                          ) -> tuple[NewInPeriod, int, int]:
    """Titles watched that the library first had in the period, from library_seen,
    matched by name identity: "episode:name:<show>:<season>:<episode>", and for
    films "movie:name:<name>:<year>". The database does not keep a play's year,
    so a film matches every year of its name and is new only if the oldest of
    them was first seen in the period. Also returns the films and episodes the
    library got in the period (one name identity each)."""
    lo, hi = stamp(start.replace(tzinfo=TZ)), stamp(end.replace(tzinfo=TZ))
    episode_rows = await _rows(db, "SELECT identity FROM library_seen WHERE identity LIKE 'episode:name:%' "
                                   "AND first_seen >= ? AND first_seen < ?", (lo, hi))
    film_rows = await _rows(db, "SELECT identity, first_seen FROM library_seen "
                                "WHERE identity LIKE 'movie:name:%'")
    new_episodes = {r[0] for r in episode_rows}
    oldest: dict[str, str] = {}
    added_films = 0
    for identity, first in film_rows:
        name = identity[len("movie:name:"):].rsplit(":", 1)[0]
        oldest[name] = min(oldest.get(name, first), first)
        added_films += lo <= first < hi

    films, episodes, shows = [], set(), []
    for s in sessions:
        if s.kind == "film":
            name = s.key[len("film:"):]
            if name in oldest and lo <= oldest[name] < hi and s.title not in films:
                films.append(s.title)
        elif s.show_key and s.episode is not None:
            if f"episode:name:{s.show_key}:{s.season or 0}:{s.episode}" in new_episodes:
                episodes.add(s.key)
                if names[s.show_key] not in shows:
                    shows.append(names[s.show_key])
    return NewInPeriod(films, len(episodes), shows), added_films, len(new_episodes)


async def movie_nights(db, user: str | None, start: datetime, end: datetime, sessions,
                       discord_ids=()) -> tuple[MovieNights, int]:
    """Movie nights in the period (by their start, Toronto time).

    A night was held if the bot recapped it (status "done"), or if the bot was
    down for it ("missed") and someone's play of its film overlaps it. Cancelled
    and deleted nights never count.

    Someone attended a held night if either
    - a Discord member linked to them (or one of discord_ids) spent
      ATTEND_MINUTES in the Theatre while it was live, the rule the recap and
      /achievements use; or
    - their play of the night's film (its item id, else its name) overlaps the
      night's start-to-end window by ATTEND_MINUTES or more: SyncPlay without
      the voice channel.

    Returns the nights (attended per person, held for the server) and, for the
    server, the attendances summed over nights."""
    rows = await _rows(db, "SELECT event_id, title, film_name, item_id, start_time, end_time, status "
                           "FROM movie_nights WHERE status IN ('done', 'missed')")
    nights = []
    for event_id, title, film, item_id, s, e, status in rows:
        night_start, night_end = _from_utc(s), _from_utc(e)
        if night_start and night_end and start <= night_start < end:
            nights.append((night_start, str(event_id), title or film, name_key(film), item_id, night_end, status))
    nights.sort()
    if not nights:
        return MovieNights(0, []), 0

    links = {str(d): j.lower() for d, j in await _rows(db, "SELECT discord_user_id, jellyfin_user FROM user_links")}
    marks = ",".join("?" * len(nights))
    theatre: dict[str, set[str]] = defaultdict(set)
    for event_id, discord_id in await _rows(
            db, f"SELECT event_id, discord_user_id FROM movie_night_attendance "
                f"WHERE minutes >= ? AND event_id IN ({marks})", (ATTEND_MINUTES, *[n[1] for n in nights])):
        theatre[str(event_id)].add(links.get(str(discord_id), f"discord:{discord_id}"))
    mine = {str(d) for d in discord_ids} | {d for d, j in links.items() if user is not None and j == user.lower()}
    me = {links.get(d, f"discord:{d}") for d in mine} | ({user.lower()} if user is not None else set())

    held, attended, attendances = [], [], 0
    for night_start, event_id, title, film_key, item_id, night_end, status in nights:
        watchers = {s.user for s in sessions if s.kind == "film"
                    and ((item_id and s.item_id == item_id) or (film_key and s.key == f"film:{film_key}"))
                    and min(s.end, night_end) - max(s.start, night_start) >= timedelta(minutes=ATTEND_MINUTES)}
        people = watchers | (theatre[event_id] if status == "done" else set())
        if status == "missed" and not watchers:
            continue
        held.append(MovieNight(title, night_start.date()))
        attendances += len(people)
        if user is not None and people & me:
            attended.append(MovieNight(title, night_start.date()))
    if user is None:
        return MovieNights(len(held), held), attendances
    return MovieNights(len(attended), attended), 0


# --- persona and fun facts ---------------------------------------------------------------

def _pct(part: float, whole: float) -> int:
    return round(100 * part / whole) if whole else 0


def _night_share(sessions) -> float:
    _, hours, _ = _spread(sessions)
    total = sum(hours)
    return (sum(hours[22:]) + sum(hours[:4])) / total if total else 0.0


def persona(stats: dict) -> Persona | None:
    """The first rule that holds, in this order (shares are of watched time):

    1. Binger: a binge of 6+ episodes, and episodes are 50%+ of the time
    2. Comfort rewatcher: 5+ plays after the first, at least 10% of all plays
    3. Film buff: films are 60%+ of the time, 5+ different films
    4. Night owl: 35%+ of the time between 22:00 and 04:00
    5. Movie night regular: 3+ movie nights attended
    6. Weekend watcher: 50%+ of the time on Saturday and Sunday
    7. Devoted fan: one show is 40%+ of the time
    8. Explorer: 15+ different shows and films
    9. Casual viewer: everyone else
    """
    wt: WatchTime = stats["watch_time"]
    total = wt.total_seconds
    if not total:
        return None
    binge: Binge | None = stats["longest_binge"]
    if binge and binge.episodes >= 6 and wt.episode_seconds >= 0.5 * total:
        how = "in one sitting" if binge.kind == "sitting" else "in one day"
        return Persona("binger", "Binger", f"{binge.episodes} episodes of {binge.show} {how}.")
    rewatches = stats["rewatches"]
    top: Rewatch | None = stats["most_rewatched"]
    if rewatches >= 5 and rewatches >= 0.1 * wt.plays and top:
        return Persona("comfort_rewatcher", "Comfort rewatcher",
                       f"You went back to {_rewatch_title(top)} {top.plays} times.")
    if wt.film_seconds >= 0.6 * total and wt.films >= 5:
        return Persona("film_buff", "Film buff",
                       f"Films were {_pct(wt.film_seconds, total)}% of your watch time: {wt.films} of them.")
    night = stats["night_share"]
    if night >= 0.35:
        return Persona("night_owl", "Night owl",
                       f"{round(100 * night)}% of your watching happened between 10 PM and 4 AM.")
    nights: MovieNights = stats["movie_nights"]
    if nights.count >= 3:
        return Persona("movie_night_regular", "Movie night regular",
                       f"You came to {nights.count} movie nights.")
    week: Busiest | None = stats["busiest"]
    if week and sum(week.by_weekday[5:]) >= 0.5 * total:
        return Persona("weekend_watcher", "Weekend watcher",
                       f"{_pct(sum(week.by_weekday[5:]), total)}% of your watching was on weekends.")
    shows: list[ShowStat] = stats["top_shows"]
    if shows and shows[0].seconds >= 0.4 * total:
        return Persona("devoted_fan", "Devoted fan",
                       f"{shows[0].show} was {_pct(shows[0].seconds, total)}% of everything you watched.")
    titles = stats["distinct_shows"] + wt.films
    if titles >= 15:
        return Persona("explorer", "Explorer", f"You tried {titles} different shows and films.")
    return Persona("casual_viewer", "Casual viewer", "A bit of everything, at your own pace.")


def _rewatch_title(r: Rewatch) -> str:
    if r.kind == "episode" and r.show:
        return f"{r.show} S{r.season or 0}E{r.episode}"
    return r.title


def _clock(at: datetime) -> str:
    return f"{at.hour % 12 or 12}:{at.minute:02d} {'AM' if at.hour < 12 else 'PM'}"


def fun_facts(stats: dict, server: bool, period: Period) -> list[FunFact]:
    """Up to FUN_FACTS of what stands out most. Each candidate has a score: its
    number over a "notable" mark (50 hours, a 5-episode binge, a 5-day streak,
    20 episodes of one show, 2.5 plays of one title, a 2.5-hour film, 2 movie
    nights, 10 new titles, 2 finished shows; a start between midnight and 5 AM
    scores 0.8 to 1.8, later is higher). The highest scores of at least 1 win,
    at least two facts when there are two candidates at all; ties go by key."""
    when = {"year": "this year", "month": "this month"}.get(period.kind, "in this period")
    you = "Everyone" if server else "You"
    wt: WatchTime = stats["watch_time"]
    cands: list[tuple[float, FunFact]] = []

    def add(score, key, text, value):
        if score > 0:
            cands.append((round(score, 6), FunFact(key, text, value)))

    hours = wt.total_seconds / 3600
    add(hours / 50, "hours", f"{you} watched {hours:,.0f} hours {when}: {hours / 24:.1f} days of screen time.",
        round(hours, 1))
    if (b := stats["longest_binge"]):
        how = "in one sitting" if b.kind == "sitting" else "in one day"
        add(b.episodes / 5, "binge", f"{b.episodes} episodes of {b.show} {how}.", b.episodes)
    if (st := stats["longest_streak"]):
        text = (f"Something was playing {st.days} days in a row." if server
                else f"You watched something {st.days} days in a row.")
        add(st.days / 5, "streak", text, st.days)
    if stats["top_shows"]:
        top = stats["top_shows"][0]
        add(top.episodes / 20, "top_show", f"{top.episodes} episodes of {top.show}.", top.episodes)
    if (r := stats["most_rewatched"]):
        add(r.plays / 2.5, "rewatch", f"{_rewatch_title(r)} was played {r.plays} times.", r.plays)
    if (late := stats["latest_start"]):
        minutes = late.start.hour * 60 + late.start.minute
        title = late.show if late.show else late.title
        who = "Someone pressed play on" if server else "Your latest start:"
        add(0.8 + minutes / 300, "late_night", f"{who} {title} at {_clock(late.start)}.", _clock(late.start))
    if (long := stats["longest_film"]):
        h, m = divmod(long.watched // 60, 60)
        add(long.watched / 9000, "longest_film", f"The longest film in one go: {long.title}, {h}h {m:02d}m.",
            long.title)
    nights: MovieNights = stats["movie_nights"]
    if nights.count:
        text = (f"{nights.count} movie nights {when}." if server
                else f"{nights.count} movie night{'s' * (nights.count != 1)} with the server.")
        add(nights.count / 2, "movie_nights", text, nights.count)
    if server:
        added = stats["library_added"]
        add(added / 100, "new_titles", f"{added:,} films and episodes joined the library {when}.", added)
    else:
        new: NewInPeriod = stats["new_this_period"]
        count = len(new.films) + new.episodes
        add(count / 10, "new_titles", f"{count} of the things you watched were new to the library {when}.", count)
    finished = len(stats["shows_finished"])
    add(finished / 2, "shows_finished", f"{you} finished {finished} show{'s' * (finished != 1)}.", finished)

    cands.sort(key=lambda c: (-c[0], c[1].key))
    picked = [f for score, f in cands if score >= 1][:FUN_FACTS]
    if len(picked) < 2:
        picked = [f for _, f in cands[:2]]
    return picked


def _latest_start(sessions) -> _Session | None:
    """The session starting latest into the night, midnight to 5 AM."""
    small = [s for s in sessions if s.start.hour < 5]
    return max(small, key=lambda s: (s.start.time(), -s.start.toordinal(), s.key), default=None)


# --- coverage --------------------------------------------------------------------------

def coverage(sessions, start: datetime, end: datetime, excluded: Counter) -> Coverage:
    """The days with data, and KNOWN_GAPS and PARTIAL_BEFORE where they touch the period."""
    days = sorted({s.start.date() for s in sessions})
    first, last = start.date(), (end - timedelta(microseconds=1)).date()
    gaps = [Gap(max(g.start, first), min(g.end, last), g.reason)
            for g in KNOWN_GAPS if g.start <= last and g.end >= first]
    partial = first < PARTIAL_BEFORE
    notes = [f"No data {g.start:%d %b} to {g.end:%d %b %Y}: {g.reason}" for g in gaps]
    if partial:
        notes.append(PARTIAL_NOTE)
    known = sum(1 for s in sessions if s.runtime)
    names = ("short", "unclosed", "no_duration", "duplicates_merged", "capped", "beyond_runtime", "other_types")
    return Coverage(
        first_day=days[0] if days else None, last_day=days[-1] if days else None, days_with_data=len(days),
        known_gaps=gaps, partial_before=PARTIAL_BEFORE if partial else None, notes=notes,
        complete=not gaps and not partial, excluded={k: excluded.get(k, 0) for k in names},
        runtime_known_share=round(known / len(sessions), 3) if sessions else 0.0)


# --- the entry point ----------------------------------------------------------------------

async def wrapped_stats(db, user: str | None, start: date | datetime, end: date | datetime, *,
                        min_hours: float = MIN_HOURS, runtimes: dict[str, int] | None = None,
                        discord_ids=()) -> WrappedStats:
    """Wrapped for one Jellyfin user (watch_history.user_name, any case), or the
    whole server with user None, over [start, end).

    db: an aiosqlite or sqlite3 connection to the bot's database.
    min_hours: watched time below which eligible is False.
    runtimes: seconds by Jellyfin item id, where the caller knows them.
    discord_ids: Discord members to count Theatre attendance for besides those
    linked to user (an unlinked caller, as on /achievements)."""
    start, end = local(start), local(end)
    if end <= start:
        raise ValueError("end must be after start")
    excluded = Counter()
    rows = await _load_plays(db, user, start, end)
    sessions, viewings = build_sessions(rows, start, end, runtimes, excluded)
    show_names, film_names = _show_names(sessions), _film_names(sessions)
    period = _period(start, end)
    server = user is None

    finished, completions = await shows_finished(db, user, start, end)
    nights, attendances = await movie_nights(db, user, start, end, sessions, discord_ids)
    new, added_films, added_episodes = await new_this_period(db, start, end, sessions, show_names)
    stats = {
        "watch_time": watch_time(sessions, viewings),
        "top_shows": top_shows(sessions, viewings, show_names),
        "top_films": top_films(sessions, viewings, film_names),
        "most_rewatched": most_rewatched(viewings, film_names, show_names),
        "longest_binge": None if server else longest_binge(sessions, show_names),
        "busiest": busiest(sessions),
        "longest_streak": longest_streak(sessions),
        "shows_finished": finished,
        "shows_started": await shows_started(db, user, start, sessions, show_names),
        "new_this_period": new,
        "movie_nights": nights,
    }
    extra = {
        "rewatches": sum(len(v) - 1 for v in viewings.values()),
        "night_share": _night_share(sessions),
        "distinct_shows": len(show_names),
        "latest_start": _latest_start(sessions),
        "longest_film": max((s for s in sessions if s.kind == "film"),
                            key=lambda s: (s.watched, -s.start.toordinal(), s.key), default=None),
        "library_added": added_films + added_episodes,
    }
    return WrappedStats(
        scope="server" if server else "user", user=user, period=period,
        eligible=stats["watch_time"].total_seconds >= min_hours * 3600, min_hours=min_hours,
        persona=None if server else persona(stats | extra),
        fun_facts=fun_facts(stats | extra, server, period),
        coverage=coverage(sessions, start, end, excluded),
        server=ServerExtras(
            watchers=len({s.user for s in sessions}), library_added_films=added_films,
            library_added_episodes=added_episodes, movie_nights_held=nights.count,
            movie_night_attendances=attendances, shows_finished=completions) if server else None,
        **stats)


def wrapped_stats_sync(conn: sqlite3.Connection, user: str | None, start, end, **kwargs) -> WrappedStats:
    """wrapped_stats for scripts and threads with a plain sqlite3 connection."""
    return asyncio.run(wrapped_stats(conn, user, start, end, **kwargs))
