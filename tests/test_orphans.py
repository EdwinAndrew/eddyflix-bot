"""Plays the bot never saw stop are closed once at start-up (watch_data.close_orphans)."""
import asyncio
import sqlite3
from datetime import datetime, timedelta

import pytest

import config
import watch_data

T0 = datetime(2026, 7, 29, 1, 57, 33)


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "bot.db")
    monkeypatch.setattr(config, "DB_PATH", path)
    asyncio.run(watch_data.init_database())
    return path


class Jellyfin:
    def __init__(self, runtimes):
        self.runtimes = runtimes          # item id -> minutes, or missing
        self.asked = []

    async def get_item(self, item_id, user_id=None):
        self.asked.append(item_id)
        minutes = self.runtimes.get(item_id)
        return {"Id": item_id, "RunTimeTicks": minutes * 60 * 10_000_000} if minutes else {}


def add(db_path, session, item, start, user="eddy", end=None):
    with sqlite3.connect(db_path) as db:
        cur = db.execute(
            "INSERT INTO watch_history (session_id, user_name, item_id, item_name, item_type, start_time, end_time) "
            "VALUES (?, ?, ?, ?, 'Episode', ?, ?)",
            (session, user, item, item, start.isoformat(" "), end.isoformat(" ") if end else None))
        return cur.lastrowid


def rows(db_path):
    with sqlite3.connect(db_path) as db:
        return {r[0]: (r[1], r[2]) for r in db.execute("SELECT id, end_time, duration_seconds FROM watch_history")}


def close(db_path, jf, live=frozenset(), now=T0 + timedelta(days=100)):
    return asyncio.run(watch_data.close_orphans(jf, set(live), now))


def test_a_duplicate_opened_every_poll_is_deleted(db_path):
    dupes = [add(db_path, "s", "ep", T0 + timedelta(seconds=30 * i)) for i in range(6)]
    last = add(db_path, "s", "ep", T0 + timedelta(minutes=3), end=T0 + timedelta(minutes=40))
    assert close(db_path, Jellyfin({"ep": 45})) == (0, 6)
    assert set(rows(db_path)) == {last}
    assert dupes


def test_closed_at_start_plus_runtime(db_path):
    i = add(db_path, "s", "ep", T0)
    assert close(db_path, Jellyfin({"ep": 45})) == (1, 0)
    end, seconds = rows(db_path)[i]
    assert seconds == 45 * 60
    assert datetime.fromisoformat(end) == T0 + timedelta(minutes=45)


def test_no_later_than_the_next_play(db_path):
    i = add(db_path, "s1", "ep1", T0)
    add(db_path, "s2", "ep2", T0 + timedelta(minutes=20), end=T0 + timedelta(minutes=60))
    add(db_path, "s3", "other", T0 + timedelta(minutes=5), user="Sarah", end=T0 + timedelta(minutes=9))
    close(db_path, Jellyfin({"ep1": 45}))
    assert rows(db_path)[i][1] == 20 * 60          # eddy's next play, not Sarah's


def test_unknown_length_counts_nothing(db_path):
    # The next play can be days later; that is not how long anyone watched
    a = add(db_path, "s1", "gone1", T0)
    add(db_path, "s2", "ep2", T0 + timedelta(days=3), end=T0 + timedelta(days=3, minutes=30))
    b = add(db_path, "s3", "gone2", T0 + timedelta(days=5))
    assert close(db_path, Jellyfin({})) == (2, 0)
    assert rows(db_path)[a][1] == 0
    assert rows(db_path)[b][1] == 0


def test_plays_still_going_are_left_open(db_path):
    i = add(db_path, "s", "ep", T0)
    assert close(db_path, Jellyfin({"ep": 45}), live={("s", "ep")}) == (0, 0)
    assert rows(db_path)[i] == (None, None)


def test_never_past_now_and_runtime_asked_once(db_path):
    now = T0 + timedelta(minutes=10)
    a = add(db_path, "s1", "ep", T0)
    b = add(db_path, "s2", "ep", T0 + timedelta(minutes=1), user="Sarah")
    jf = Jellyfin({"ep": 45})
    close(db_path, jf, now=now)
    assert rows(db_path)[a][1] == 10 * 60
    assert rows(db_path)[b][1] == 9 * 60
    assert jf.asked == ["ep"]


def test_closed_plays_count_in_popular_content(db_path):
    add(db_path, "s", "ep", T0)
    close(db_path, Jellyfin({"ep": 45}))
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT item_id, total_watch_time, watch_count FROM popular_content").fetchall() == \
            [("ep", 45 * 60, 1)]
