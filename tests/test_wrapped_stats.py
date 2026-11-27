"""The Wrapped stats engine (#65) against the hand-made database in
wrapped_fixtures.py, plus small databases for single rules, and the timing on a
synthetic one the size of a full year."""
import asyncio
import json
import time
from datetime import date, datetime, timedelta

import aiosqlite
import pytest

import screenings
import wrapped_stats as ws
from wrapped_fixtures import RUNTIMES, create, episode, fixture_db, play, PLAY_SQL, synthetic_db

YEAR = ws.year(2026)
NOVEMBER = ws.month(2026, 11)


@pytest.fixture
def conn(tmp_path):
    db = fixture_db(str(tmp_path / "wrapped.db"))
    yield db
    db.close()


@pytest.fixture
def blank(tmp_path):
    """An empty database with the bot's schema, and a way to add plays."""
    db = create(str(tmp_path / "blank.db"))

    def add(*rows):
        db.executemany(PLAY_SQL, rows)
        db.commit()
    yield db, add
    db.close()


def stats(conn, user, period=YEAR, **kw):
    return ws.wrapped_stats_sync(conn, user, *period, **kw)


# --- people ----------------------------------------------------------------------

def test_heavy_user_year(conn):
    s = stats(conn, "Harper")
    assert s.scope == "user" and s.eligible
    # Films: Paddington 2 three times (312 min), Dune 155, 1917 119, Wicked 160.
    # Episodes: The Pitt 14 x 50 + E3 paused nine hours, held to 2 h; The Simpsons 9 x 22.
    assert s.watch_time.film_seconds == (312 + 155 + 119 + 160) * 60
    assert s.watch_time.episode_seconds == (14 * 50 + 120 + 9 * 22) * 60
    assert s.watch_time.films == 4 and s.watch_time.episodes == 24
    assert [(t.show, t.episodes) for t in s.top_shows] == [("The Pitt", 15), ("The Simpsons", 9)]
    assert [(f.title, f.plays) for f in s.top_films] == [("Paddington 2", 3), ("Wicked", 1), ("Dune", 1)]
    assert s.longest_streak == ws.Streak(15, date(2026, 10, 25), date(2026, 11, 8))
    assert s.shows_started == ["The Pitt"]          # The Simpsons was played in Dec 2025
    assert s.persona.key == "binger"
    assert 2 <= len(s.fun_facts) <= 3


def test_light_user_is_eligible(conn):
    s = stats(conn, "quinn")
    assert s.eligible and s.watch_time.total_seconds == (6 * 7 + 155) * 60
    assert s.top_shows[0].show == "Bluey" and s.top_shows[0].episodes == 6
    assert s.new_this_period.episodes == 6 and s.new_this_period.films == []
    assert s.persona.key == "casual_viewer"
    assert s.longest_binge.episodes == 6


def test_ineligible_user_and_a_configurable_threshold(conn):
    s = stats(conn, "Rowan")
    assert s.watch_time.total_seconds == 3600 and not s.eligible and s.min_hours == ws.MIN_HOURS
    assert stats(conn, "Rowan", min_hours=0.5).eligible
    assert not stats(conn, "quinn", min_hours=10).eligible


def test_nobody_at_all(conn):
    s = stats(conn, "ghost")
    assert not s.eligible and s.watch_time.total_seconds == 0
    assert s.persona is None and s.fun_facts == [] and s.busiest is None and s.longest_binge is None
    assert s.coverage.first_day is None


def test_user_name_is_case_insensitive(conn):
    assert stats(conn, "harper").to_dict() | {"user": None} == stats(conn, "HARPER").to_dict() | {"user": None}


# --- rewatches -------------------------------------------------------------------

def test_rewatches_in_the_year_but_not_the_month(conn):
    r = stats(conn, "Harper").most_rewatched
    assert (r.kind, r.title, r.plays, r.rewatches) == ("film", "Paddington 2", 3, 2)
    assert stats(conn, "Harper", NOVEMBER).most_rewatched is None


def test_stopping_and_resuming_is_one_play(blank):
    db, add = blank
    add(play("a", "2026-05-01 20:00", 50, "f1", "Heat"),            # stopped halfway...
        play("a", "2026-05-02 20:00", 60, "f1", "Heat"),            # ...finished the next day
        play("a", "2026-06-01 20:00", 110, "f1", "Heat"))           # watched again a month later
    s = stats(db, "a")
    assert s.top_films[0].plays == 2 and s.most_rewatched.plays == 2


def test_known_runtime_counts_complete_viewings(blank):
    db, add = blank
    add(play("a", "2026-05-01 20:00", 100, "f1", "Heat"),
        play("a", "2026-05-02 20:00", 100, "f1", "Heat"),           # a day later, but the first was complete
        play("a", "2026-05-03 20:00", 30, "f1", "Heat"),            # started again...
        play("a", "2026-05-03 21:00", 100, "f1", "Heat"))           # ...and over from the start
    s = stats(db, "a", runtimes={"f1": 100 * 60})
    assert s.top_films[0].plays == 3
    # The fourth play only counts up to the runtime: 100 + 100 + (30 + 70)
    assert s.watch_time.total_seconds == 300 * 60
    assert s.coverage.runtime_known_share == 1.0


def test_an_upgraded_file_is_the_same_title(blank):
    db, add = blank
    add(episode("a", "2026-05-01 20:00", 40, "Lost", 1, 1),
        play("a", "2026-05-09 20:00", 40, "new-id", "Pilot", "Episode", "Lost", 1, 1))
    s = stats(db, "a")
    assert s.watch_time.episodes == 1 and s.most_rewatched.plays == 2


# --- binges ----------------------------------------------------------------------

def test_binge_across_midnight_is_one_sitting(conn):
    b = stats(conn, "Harper").longest_binge
    assert (b.show, b.episodes, b.kind, b.day) == ("The Simpsons", 8, "sitting", date(2026, 11, 14))
    assert b.crosses_midnight and b.start == datetime(2026, 11, 14, 22, 30)
    assert b.end == datetime(2026, 11, 15, 2, 1)


def test_binge_by_day_with_long_breaks(blank):
    db, add = blank
    # Five episodes on one day, two hours apart: no sitting longer than one
    add(*[episode("a", f"2026-07-04 {10 + 2 * n}:00", 30, "Bluey", 1, n) for n in range(1, 6)])
    b = stats(db, "a").longest_binge
    assert (b.kind, b.episodes, b.crosses_midnight) == ("day", 5, False)


def test_server_has_no_binge(conn):
    assert stats(conn, None).longest_binge is None


# --- quality guards --------------------------------------------------------------

def test_overlapping_duplicates_count_once(conn):
    s = stats(conn, "Harper")
    simpsons = s.top_shows[1]
    assert (simpsons.episodes, simpsons.plays, simpsons.seconds) == (9, 9, 9 * 22 * 60)
    assert s.coverage.excluded["duplicates_merged"] == 1


def test_unclosed_short_and_zero_duration_plays_are_left_out(conn):
    s = stats(conn, "Harper")
    ex = s.coverage.excluded
    assert (ex["unclosed"], ex["short"], ex["no_duration"]) == (1, 1, 1)
    assert "Bluey" not in [t.show for t in s.top_shows]
    assert [f.plays for f in s.top_films if f.title == "Dune"] == [1]


def test_play_with_no_duration_column_uses_its_end_time(blank):
    db, add = blank
    row = play("a", "2026-05-01 20:00", 30, "f1", "Heat")
    add(row[:10] + (None,))
    assert stats(db, "a").watch_time.total_seconds == 30 * 60


def test_time_is_capped_at_the_runtime_or_a_ceiling(conn):
    unknown = stats(conn, "Harper")
    known = stats(conn, "Harper", runtimes=RUNTIMES)
    assert unknown.watch_time.episode_seconds - known.watch_time.episode_seconds == (120 - 50) * 60
    assert unknown.coverage.excluded["capped"] == known.coverage.excluded["capped"] == 1
    assert known.coverage.runtime_known_share > 0 == unknown.coverage.runtime_known_share


def test_other_item_types_do_not_count(blank):
    db, add = blank
    add(play("a", "2026-05-01 20:00", 60, "song", "A song", "Audio"))
    s = stats(db, "a")
    assert s.watch_time.total_seconds == 0 and s.coverage.excluded["other_types"] == 1


# --- time ------------------------------------------------------------------------

def test_toronto_days_decide_the_period(conn):
    year = stats(conn, "Harper")
    december = stats(conn, "Harper", ws.month(2026, 12))
    # 2026-12-31 23:30 counts in 2026 and December; 2025-12-31 23:50 and 2027-01-01 00:10 do not
    assert year.coverage.last_day == date(2026, 12, 31)
    assert december.watch_time.total_seconds == 22 * 60
    assert stats(conn, "Harper", ws.year(2027)).watch_time.total_seconds == 22 * 60


def test_utc_achievement_lands_on_the_toronto_day(conn):
    # Stored as CURRENT_TIMESTAMP, 2026-11-09 02:30 UTC: 8 Nov, 21:30 in Toronto
    assert stats(conn, "Harper").shows_finished == [ws.ShowFinished("The Pitt", 15, date(2026, 11, 8))]
    assert stats(conn, "quinn").shows_finished == []       # Bluey was finished in 2025


def test_busiest_day_hour_and_slot(conn):
    b = stats(conn, "Harper").busiest
    assert (b.weekday, b.hour, b.slot) == ("Saturday", 21, "Saturday night")
    assert sum(b.by_hour) == sum(b.by_weekday) == stats(conn, "Harper").watch_time.total_seconds


def test_small_hours_belong_to_the_night_before():
    assert ws._slot(datetime(2026, 11, 16, 1, 0)) == (6, "night")       # Monday 1 AM: Sunday night
    assert ws._slot(datetime(2026, 11, 15, 22, 0)) == (6, "night")


def test_periods():
    assert ws.month(2026, 12) == (date(2026, 12, 1), date(2027, 1, 1))
    assert ws._period(ws.local(date(2026, 1, 1)), ws.local(date(2027, 1, 1))).kind == "year"
    assert ws._period(ws.local(date(2026, 2, 1)), ws.local(date(2026, 3, 1))).label == "February 2026"
    assert ws._period(ws.local(date(2026, 2, 3)), ws.local(date(2026, 2, 10))).kind == "custom"


def test_end_before_start_is_refused(conn):
    with pytest.raises(ValueError):
        ws.wrapped_stats_sync(conn, None, date(2026, 2, 1), date(2026, 1, 1))


# --- coverage --------------------------------------------------------------------

def test_coverage_in_a_year_with_the_gap(conn):
    c = stats(conn, "Harper").coverage
    assert c.known_gaps == [ws.Gap(date(2026, 10, 12), date(2026, 10, 18), ws.KNOWN_GAPS[0].reason)]
    assert c.partial_before == ws.PARTIAL_BEFORE and not c.complete and len(c.notes) == 2
    assert (c.first_day, c.last_day) == (date(2026, 2, 14), date(2026, 12, 31))


def test_coverage_in_a_month_after_the_gap(conn):
    c = stats(conn, "Harper", NOVEMBER).coverage
    assert c.complete and c.known_gaps == [] and c.partial_before is None and c.notes == []


def test_gap_is_clipped_to_the_period_and_the_list_extends(conn, monkeypatch):
    c = stats(conn, None, (date(2026, 10, 15), date(2026, 10, 26))).coverage
    assert [(g.start, g.end) for g in c.known_gaps] == [(date(2026, 10, 15), date(2026, 10, 18))]
    monkeypatch.setattr(ws, "KNOWN_GAPS", ws.KNOWN_GAPS + (ws.Gap(date(2026, 11, 20), date(2026, 11, 21), "Test"),))
    assert len(stats(conn, None, NOVEMBER).coverage.known_gaps) == 1


# --- movie nights ----------------------------------------------------------------

def test_attended_by_play_and_by_theatre(conn):
    harper = stats(conn, "Harper").movie_nights
    # 5 Mar: the bot missed the night, but Harper's Dune play overlaps it. 30 Oct:
    # missed with no play, so not held. The cancelled one never counts.
    assert harper == ws.MovieNights(2, [ws.MovieNight("Dune (2021)", date(2026, 3, 5)),
                                        ws.MovieNight("1917 (2019)", date(2026, 11, 13))])
    assert stats(conn, "quinn").movie_nights.count == 1           # in the Theatre, played nothing
    assert stats(conn, "Rowan").movie_nights.count == 1           # played 1917, not linked
    assert stats(conn, "Harper", NOVEMBER).movie_nights.count == 1


def test_theatre_minutes_need_the_threshold(conn):
    # Discord 3003 was in the Theatre for 5 minutes only
    assert stats(conn, "ghost", discord_ids=[3003]).movie_nights.count == 0
    assert stats(conn, "ghost", discord_ids=[2002]).movie_nights.count == 1
    assert ws.ATTEND_MINUTES == screenings.ATTEND_MINUTES


def test_movie_night_plays_count_as_watch_time(conn):
    assert "1917" in [f.title for f in stats(conn, "Rowan").top_films]


# --- library_seen ----------------------------------------------------------------

def test_new_this_period(conn):
    new = stats(conn, "Harper").new_this_period
    # Dune matches both Dune (2021, new) and Dune (1984, old): the play has no year, so not new
    assert new.films == ["1917", "Wicked"] and new.episodes == 15 and new.shows == ["The Pitt"]
    assert stats(conn, "Harper", ws.month(2026, 10)).new_this_period.films == []


# --- the server ------------------------------------------------------------------

def test_server_totals_without_names_or_ids(conn):
    s = stats(conn, None)
    people = [stats(conn, u) for u in ("Harper", "quinn", "Rowan")]
    assert s.scope == "server" and s.user is None and s.persona is None
    assert s.watch_time.total_seconds == sum(p.watch_time.total_seconds for p in people)
    assert s.server == ws.ServerExtras(watchers=3, library_added_films=4, library_added_episodes=21,
                                       movie_nights_held=2, movie_night_attendances=4, shows_finished=1)
    assert s.shows_finished == [ws.ShowFinished("The Pitt", None, date(2026, 11, 8))]
    text = json.dumps(s.to_dict()).lower()
    for secret in ("harper", "quinn", "rowan", "1001", "2002", "3003", "s-harper"):
        assert secret not in text


def test_server_rewatches_add_up_over_people(blank):
    db, add = blank
    add(play("a", "2026-03-01 20:00", 90, "f1", "Up"), play("a", "2026-06-01 20:00", 90, "f1", "Up"),
        play("b", "2026-03-02 20:00", 90, "f1", "Up"), play("b", "2026-06-02 20:00", 90, "f1", "Up"),
        play("c", "2026-03-03 20:00", 90, "f1", "Up"))                    # once: not a rewatch
    r = stats(db, None).most_rewatched
    assert (r.plays, r.rewatches) == (4, 2)


# --- persona and fun facts ---------------------------------------------------------

def test_night_owl(blank):
    db, add = blank
    add(*[episode("owl", f"2026-04-{d:02d} 23:30", 45, f"Show {d}", 1, 1) for d in range(1, 8)])
    p = stats(db, "owl").persona
    assert p.key == "night_owl" and "between 10 PM and 4 AM" in p.reason


def test_film_buff(blank):
    db, add = blank
    add(*[play("f", f"2026-04-{d:02d} 15:00", 100, f"f{d}", f"Film {d}") for d in range(1, 8)])
    assert stats(db, "f").persona.key == "film_buff"


def test_fun_facts_are_scored_then_by_key(conn):
    keys = [f.key for f in stats(conn, "Harper").fun_facts]
    assert keys == ["streak", "new_titles", "binge"]


# --- determinism and speed -------------------------------------------------------

def test_same_database_same_result_whatever_the_row_order(tmp_path, conn):
    other = fixture_db(str(tmp_path / "reversed.db"), reverse=True)
    for user in ("Harper", "quinn", None):
        for period in (YEAR, NOVEMBER):
            a = stats(conn, user, period, runtimes=RUNTIMES).to_dict()
            assert a == stats(conn, user, period, runtimes=RUNTIMES).to_dict()
            assert json.dumps(a) == json.dumps(stats(other, user, period, runtimes=RUNTIMES).to_dict())
    other.close()


def test_aiosqlite_and_sqlite3_agree(tmp_path, conn):
    async def run():
        async with aiosqlite.connect(str(tmp_path / "wrapped.db")) as db:
            return await ws.wrapped_stats(db, "Harper", *YEAR)
    assert asyncio.run(run()).to_dict() == stats(conn, "Harper").to_dict()


def test_full_year_on_a_large_database_is_fast(tmp_path):
    db = synthetic_db(str(tmp_path / "big.db"))
    assert db.execute("SELECT COUNT(*) FROM watch_history").fetchone()[0] >= 50_000
    started = time.perf_counter()
    server = stats(db, None)
    took = time.perf_counter() - started
    heavy_started = time.perf_counter()
    heavy = stats(db, "user0")
    heavy_took = time.perf_counter() - heavy_started
    print(f"full year: server {took:.2f}s, heaviest user {heavy_took:.2f}s")
    assert server.server.watchers == 7 and heavy.eligible
    assert took < 5 and heavy_took < 5
    db.close()
