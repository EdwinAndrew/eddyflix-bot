#!/usr/bin/env python3
"""Databases for the Wrapped stats engine (wrapped_stats.py): a small hand-made
one the tests check number by number, and a large synthetic one for timing.

Print an example result as JSON, the shape the cards (#66, #68) render:

    python tests/wrapped_fixtures.py                    # Harper, 2026
    python tests/wrapped_fixtures.py --server           # the server, 2026
    python tests/wrapped_fixtures.py --month 2026-11    # Harper, November
    python tests/wrapped_fixtures.py --synthetic        # time a full year on 50,000 plays

Made-up people and plays only; nothing here comes from the live database.

The hand-made one, all 2026 unless it says otherwise, in Toronto time:
- Harper (heavy, linked as Discord 1001): Paddington 2 three times (Feb, Jun,
  Nov); The Pitt S1E1-15, one a night from 25 Oct to 8 Nov, E3 left paused
  for nine hours; eight Simpsons back to back from 22:30 on 14 Nov to past
  1 AM, the first recorded twice; Dune on 5 Mar during a movie night the bot
  missed; 1917 at the 13 Nov movie night; Wicked, new to the library in
  November; a Bluey play never closed and one of two minutes; a Dune play with
  no duration; Simpsons on New Year's Eve 2025 and 2026, and at 00:10 on
  1 Jan 2027. Finished The Pitt on 8 Nov.
- Quinn (light, about 3.3 h, linked as Discord 2002): six short Bluey
  episodes, Dune; at the 13 Nov movie night in the Theatre only, playing nothing.
- Rowan (ineligible, 1 h, not linked): 1917 at the 13 Nov movie night, an hour.
"""
import asyncio
import contextlib
import random
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config         # noqa: E402
import watch_data     # noqa: E402

PLAY_SQL = """
    INSERT INTO watch_history (session_id, user_name, item_id, item_name, item_type, series_name,
        season_number, episode_number, start_time, end_time, duration_seconds, play_method, device_name)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'DirectPlay', 'TV')
"""
# Runtimes in seconds, by item id, as a caller would pass them from RunTimeTicks
RUNTIMES = {"ep-thepitt-1-3": 50 * 60}


def create(path: str) -> sqlite3.Connection:
    """An empty database with the bot's own schema."""
    old = config.DB_PATH
    config.DB_PATH = path
    try:
        with contextlib.redirect_stdout(sys.stderr):
            asyncio.run(watch_data.init_database())
    finally:
        config.DB_PATH = old
    return sqlite3.connect(path)


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def play(user, start, minutes, item_id, name, kind="Movie", series=None, season=None, episode=None,
         closed=True, duration=None, session=None):
    """One watch_history row as a tuple. minutes of None: closed with no time."""
    began = at(start) if isinstance(start, str) else start
    if minutes is None:
        end, secs = began, 0
    else:
        end, secs = began + timedelta(minutes=minutes), int(minutes * 60)
    if duration is not None:
        secs = duration
    return (session or f"s-{user}-{began:%Y%m%d%H%M}", user, item_id, name, kind, series, season, episode,
            began.isoformat(" "), end.isoformat(" ") if closed else None, secs if closed else None)


def episode(user, start, minutes, show, season, number, **kw):
    sid = show.lower().replace(" ", "")
    return play(user, start, minutes, f"ep-{sid}-{season}-{number}", f"{show} {season}x{number}",
                "Episode", show, season, number, **kw)


def fixture_plays() -> list[tuple]:
    rows = []
    h = "Harper"
    for day in ("2026-02-14", "2026-06-20", "2026-11-22"):
        rows.append(play(h, f"{day} 19:00", 104, "film-pad2", "Paddington 2"))
    first = at("2026-10-25 21:00")
    for n in range(1, 16):
        minutes = 9 * 60 if n == 3 else 50       # E3: paused overnight
        rows.append(episode(h, first + timedelta(days=n - 1), minutes, "The Pitt", 1, n))
    start = at("2026-11-14 22:30")
    for n in range(1, 9):
        rows.append(episode(h, start + timedelta(minutes=27 * (n - 1)), 22, "The Simpsons", 3, n))
    # The same episode recorded a second time, a minute in, from another session
    rows.append(episode(h, "2026-11-14 22:31", 21, "The Simpsons", 3, 1, session="dup"))
    rows.append(play(h, "2026-03-05 20:00", 155, "film-dune", "Dune"))
    rows.append(play(h, "2026-11-13 20:02", 119, "film-1917", "1917"))
    rows.append(play(h, "2026-11-28 14:00", 160, "film-wicked", "Wicked"))
    rows.append(episode(h, "2026-11-20 08:00", 7, "Bluey", 1, 1, closed=False))
    rows.append(episode(h, "2026-11-20 08:05", 2, "Bluey", 1, 2))
    rows.append(play(h, "2026-11-21 10:00", None, "film-dune", "Dune"))
    rows.append(episode(h, "2025-12-31 23:50", 22, "The Simpsons", 3, 11))
    rows.append(episode(h, "2026-12-31 23:30", 22, "The Simpsons", 3, 9))
    rows.append(episode(h, "2027-01-01 00:10", 22, "The Simpsons", 3, 10))

    q = "quinn"
    for n in range(1, 7):
        rows.append(episode(q, at("2026-11-02 18:00") + timedelta(minutes=8 * (n - 1)), 7, "Bluey", 1, n))
    rows.append(play(q, "2026-11-10 19:30", 155, "film-dune", "Dune"))

    rows.append(play("Rowan", "2026-11-13 20:05", 60, "film-1917", "1917"))
    return rows


def fill_fixture(conn: sqlite3.Connection, reverse: bool = False) -> None:
    """The hand-made data. reverse inserts every table backwards, for the
    determinism test: row order must not matter."""
    order = (lambda rows: list(reversed(rows))) if reverse else list
    conn.executemany(PLAY_SQL, order(fixture_plays()))
    conn.executemany("INSERT INTO user_links (discord_user_id, jellyfin_user) VALUES (?, ?)",
                     order([("1001", "harper"), ("2002", "Quinn")]))
    nights = [
        # event, title, film, item id, start and end (UTC), status
        ("501", "1917 (2019)", "1917", "film-1917", "2026-11-14 01:00:00+00:00", "2026-11-14 03:15:00+00:00", "done"),
        ("502", "Dune (2021)", "Dune", "film-dune", "2026-03-06 01:00:00+00:00", "2026-03-06 03:50:00+00:00", "missed"),
        ("503", "Dune (2021)", "Dune", "film-dune", "2026-10-30 23:00:00+00:00", "2026-10-31 01:50:00+00:00", "missed"),
        ("504", "Paddington 2 (2017)", "Paddington 2", "film-pad2", "2026-09-05 23:00:00+00:00",
         "2026-09-06 01:00:00+00:00", "cancelled"),
    ]
    conn.executemany("""
        INSERT INTO movie_nights (event_id, guild_id, title, film_name, item_id, host, start_time, end_time,
                                  created_at, status)
        VALUES (?, '7', ?, ?, ?, 'Host', ?, ?, '2026-01-01 00:00:00+00:00', ?)
    """, order(nights))
    conn.executemany("INSERT INTO movie_night_attendance (event_id, discord_user_id, minutes) VALUES (?, ?, ?)",
                     order([("501", "1001", 120), ("501", "2002", 90), ("501", "3003", 5)]))
    seen = [(f"episode:name:the pitt:1:{n}", "episode", "2026-10-20T12:00:00.000000+00:00") for n in range(1, 16)]
    seen += [(f"episode:tvdb:1:1:{n}", "episode", "2026-10-20T12:00:00.000000+00:00") for n in range(1, 16)]
    seen += [(f"episode:name:the simpsons:3:{n}", "episode", "2024-01-01T12:00:00.000000+00:00")
             for n in range(1, 12)]
    seen += [(f"episode:name:bluey:1:{n}", "episode", "2026-11-01T12:00:00.000000+00:00") for n in range(1, 7)]
    seen += [
        ("movie:name:paddington 2:2017", "movie", "2023-05-01T12:00:00.000000+00:00"),
        ("movie:name:dune:2021", "movie", "2026-03-01T12:00:00.000000+00:00"),
        ("movie:name:dune:1984", "movie", "2020-01-01T12:00:00.000000+00:00"),
        ("movie:name:1917:2019", "movie", "2026-11-10T12:00:00.000000+00:00"),
        ("movie:tmdb:530915", "movie", "2026-11-10T12:00:00.000000+00:00"),
        ("movie:name:wicked:2024", "movie", "2026-11-01T12:00:00.000000+00:00"),
        ("movie:name:nosferatu:2024", "movie", "2026-12-24T12:00:00.000000+00:00"),
    ]
    conn.executemany("INSERT INTO library_seen (identity, kind, first_seen) VALUES (?, ?, ?)", order(seen))
    conn.executemany("""
        INSERT INTO achievements (user_name, achievement_type, achievement_name, item_name, value, item_total, earned_at)
        VALUES (?, 'series_completion', ?, ?, ?, ?, ?)
    """, order([("Harper", 'Finished "The Pitt"', "The Pitt", 15, 15, "2026-11-09 02:30:00"),
                ("quinn", 'Finished "Bluey"', "Bluey", 6, 6, "2025-12-01 02:30:00")]))
    conn.commit()


def fixture_db(path: str, reverse: bool = False) -> sqlite3.Connection:
    conn = create(path)
    fill_fixture(conn, reverse)
    return conn


# --- the large synthetic one -------------------------------------------------------------

def synthetic_db(path: str, plays: int = 50_000, users: int = 7, shows: int = 120, episodes: int = 6_700,
                 films: int = 350, seed: int = 65) -> sqlite3.Connection:
    """A database the size the live one may reach by the end of 2026: plays over the
    whole year, people working through shows an episode at a time, some films,
    some short and duplicate plays, and the library record to go with it."""
    rng = random.Random(seed)
    conn = create(path)
    per_show = episodes // shows
    show_names = [f"Show {i:03d}" for i in range(shows)]
    film_names = [f"Film {i:03d}" for i in range(films)]
    people = [f"user{i}" for i in range(users)]
    weights = [40, 20, 12, 10, 8, 6, 4][:users] + [4] * max(0, users - 7)
    progress = {(u, s): 0 for u in people for s in range(shows)}
    t0 = datetime(2026, 1, 1)
    rows = []
    for i in range(plays):
        user = rng.choices(people, weights)[0]
        start = t0 + timedelta(seconds=rng.randrange(365 * 86400))
        if rng.random() < 0.85:
            show = rng.randrange(shows)
            n = progress[(user, show)] % per_show
            progress[(user, show)] += 1
            season, number = n // 12 + 1, n % 12 + 1
            minutes = rng.choice([2, 22, 22, 24, 42, 45, 50, 60, 300])
            rows.append(play(user, start, minutes, f"ep-{show}-{season}-{number}", f"Episode {number}", "Episode",
                             show_names[show], season, number, session=f"s{i}"))
        else:
            film = rng.randrange(films)
            rows.append(play(user, start, rng.choice([1, 95, 110, 130, 150]), f"film-{film}", film_names[film],
                             session=f"s{i}"))
        if rng.random() < 0.01:
            rows.append(rows[-1][:8] + (rows[-1][8], None, None))   # never closed
    conn.executemany(PLAY_SQL, rows)
    seen = []
    for s, name in enumerate(show_names):
        for n in range(per_show):
            first = (t0 + timedelta(days=rng.randrange(-400, 365))).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")
            seen.append((f"episode:name:{name.lower()}:{n // 12 + 1}:{n % 12 + 1}", "episode", first))
            seen.append((f"episode:tvdb:{s}:{n // 12 + 1}:{n % 12 + 1}", "episode", first))
    for f, name in enumerate(film_names):
        first = (t0 + timedelta(days=rng.randrange(-400, 365))).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")
        seen += [(f"movie:name:{name.lower()}:2020", "movie", first), (f"movie:tmdb:{f}", "movie", first)]
    conn.executemany("INSERT INTO library_seen (identity, kind, first_seen) VALUES (?, ?, ?)", seen)
    nights = [(str(900 + m), f"Film {m:03d}", f"film-{m}", f"2026-{m:02d}-15 00:00:00+00:00",
               f"2026-{m:02d}-15 02:30:00+00:00") for m in range(1, 13)]
    conn.executemany("""
        INSERT INTO movie_nights (event_id, guild_id, title, film_name, item_id, start_time, end_time, created_at, status)
        VALUES (?, '7', ?, ?, ?, ?, ?, '2026-01-01 00:00:00+00:00', 'done')
    """, [(e, t, t, i, s, end) for e, t, i, s, end in nights])
    conn.executemany("INSERT INTO user_links (discord_user_id, jellyfin_user) VALUES (?, ?)",
                     [(str(1000 + i), u) for i, u in enumerate(people)])
    conn.commit()
    return conn


def main(argv: list[str]) -> None:
    import json
    import tempfile
    import time

    import wrapped_stats as ws

    with tempfile.TemporaryDirectory() as tmp:
        if "--synthetic" in argv:
            started = time.perf_counter()
            conn = synthetic_db(f"{tmp}/synthetic.db")
            built = time.perf_counter() - started
            count = conn.execute("SELECT COUNT(*) FROM watch_history").fetchone()[0]
            print(f"built {count:,} plays in {built:.1f}s")
            for user in [None] + [f"user{i}" for i in range(7)]:
                started = time.perf_counter()
                stats = ws.wrapped_stats_sync(conn, user, *ws.year(2026))
                print(f"{user or 'server':>7}: {time.perf_counter() - started:.2f}s, "
                      f"{stats.watch_time.total_hours:,.0f} h, {stats.watch_time.plays:,} plays")
            return
        conn = fixture_db(f"{tmp}/wrapped.db")
        user = None if "--server" in argv else "Harper"
        period = ws.year(2026)
        if "--month" in argv:
            y, m = argv[argv.index("--month") + 1].split("-")
            period = ws.month(int(y), int(m))
        stats = ws.wrapped_stats_sync(conn, user, *period, runtimes=RUNTIMES)
        print(json.dumps(stats.to_dict(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1:])
