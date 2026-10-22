"""Watch history, streaks and achievements, against a throwaway SQLite database."""
import asyncio
import re
from datetime import date, datetime, timedelta

import aiosqlite
import pytest

import config
import watch_data


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "bot.db")
    monkeypatch.setattr(config, "DB_PATH", path)
    asyncio.run(watch_data.init_database())
    return path


def run(coro_fn, path):
    async def go():
        async with aiosqlite.connect(path) as db:
            result = await coro_fn(db)
            await db.commit()
            return result
    return asyncio.run(go())


def _fetch(path, sql, args=()):
    async def go():
        async with aiosqlite.connect(path) as db:
            async with db.execute(sql, args) as cur:
                return await cur.fetchall()
    return asyncio.run(go())


def watch_on(path, day):
    run(lambda db: watch_data.update_streak("eddy", db, today=day), path)


D1 = date(2026, 10, 1)


def test_first_watch_starts_a_streak(db_path):
    watch_on(db_path, D1)
    assert _fetch(db_path, "SELECT current_streak, longest_streak FROM streaks") == [(1, 1)]


def test_same_day_counts_once(db_path):
    watch_on(db_path, D1)
    watch_on(db_path, D1)
    assert _fetch(db_path, "SELECT current_streak FROM streaks") == [(1,)]


def test_consecutive_days_extend_and_a_gap_resets(db_path):
    for n in range(3):
        watch_on(db_path, D1 + timedelta(days=n))
    assert _fetch(db_path, "SELECT current_streak, longest_streak FROM streaks") == [(3, 3)]
    watch_on(db_path, D1 + timedelta(days=5))
    assert _fetch(db_path, "SELECT current_streak, longest_streak FROM streaks") == [(1, 3)]


def test_seven_day_streak_awards_once(db_path):
    for n in range(8):
        watch_on(db_path, D1 + timedelta(days=n))
    rows = _fetch(db_path, "SELECT achievement_name, value FROM achievements WHERE achievement_type='streak_milestone'")
    assert rows == [("7 Day Streak", 7)]


class FakeJellyfin:
    def __init__(self, episodes=None, fail=False):
        self.episodes, self.fail = episodes, fail

    async def get_item(self, item_id):
        if self.fail:
            raise RuntimeError("Jellyfin down")
        return {"SeriesId": "series-1"}

    async def get_series_episode_count(self, series_id):
        return self.episodes


def add_episodes(path, count, hours_each=0.5, series="Show"):
    async def go(db):
        start = datetime(2026, 10, 1, 20, 0)
        for n in range(count):
            await db.execute(
                "INSERT INTO watch_history (session_id, user_name, item_id, item_name, item_type, series_name,"
                " start_time, end_time, duration_seconds) VALUES (?,?,?,?,?,?,?,?,?)",
                ("s", "eddy", f"ep{n}", f"Episode {n}", "Episode", series, start, start, int(hours_each * 3600)))
    run(go, path)


def check(path, jf, series="Show"):
    run(lambda db: watch_data.check_achievements(jf, "eddy", series, "Episode", "Episode", "ep0", db), path)


def achievements(path, kind):
    return _fetch(path, "SELECT achievement_name, value FROM achievements WHERE achievement_type=? ORDER BY value", (kind,))


def test_series_finished_when_every_episode_is_watched(db_path):
    add_episodes(db_path, 6)
    check(db_path, FakeJellyfin(episodes=6))
    assert achievements(db_path, "series_completion") == [('Finished "Show"', 6)]


def test_series_not_finished_while_episodes_remain(db_path):
    add_episodes(db_path, 12)
    check(db_path, FakeJellyfin(episodes=20))
    assert achievements(db_path, "series_completion") == []


def test_without_jellyfin_ten_episodes_count_as_finished(db_path):
    # The fallback that issue #24 wants gone; pinned here so changing it is deliberate.
    add_episodes(db_path, 10)
    check(db_path, FakeJellyfin(fail=True))
    assert achievements(db_path, "series_completion") == [('Finished "Show"', 10)]


def test_hour_milestones_awarded_once_each(db_path):
    add_episodes(db_path, 24, hours_each=0.5)          # 12 hours
    check(db_path, FakeJellyfin(episodes=100))
    check(db_path, FakeJellyfin(episodes=100))
    assert achievements(db_path, "watch_milestone") == [("Watched 10 Hours", 10)]


def test_start_and_end_record_duration_and_popularity(db_path):
    session = {"UserName": "eddy", "DeviceName": "TV", "PlayState": {"PlayMethod": "DirectPlay"},
               "NowPlayingItem": {"Id": "m1", "Name": "1917", "Type": "Movie"}}
    asyncio.run(watch_data.record_watch_start("sess", session))
    # Pretend it started 90 minutes ago.
    run(lambda db: db.execute("UPDATE watch_history SET start_time=?", (datetime.now() - timedelta(minutes=90),)), db_path)
    asyncio.run(watch_data.record_watch_end(FakeJellyfin(), "sess", "m1"))

    (duration,), = _fetch(db_path, "SELECT duration_seconds FROM watch_history")
    assert 89 * 60 <= duration <= 91 * 60
    assert _fetch(db_path, "SELECT item_name, watch_count FROM popular_content") == [("1917", 1)]


def test_timestamps_keep_the_stored_format(db_path):
    session = {"UserName": "eddy", "NowPlayingItem": {"Id": "m1", "Name": "1917", "Type": "Movie"}}
    asyncio.run(watch_data.record_watch_start("sess", session))
    (start,), = _fetch(db_path, "SELECT start_time FROM watch_history")
    # Same as rows written before Python 3.12: a space between date and time.
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(\.\d+)?", start), start
