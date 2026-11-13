"""The self-test: the live checks against a fake Jellyfin, and the Discord checks
against fake Discord objects. The real thing runs with selftest.py and /jf-selftest."""
import asyncio
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import discord
import pytest

import config
import selftest
import watch_data
from cogs import selftest as cog

MIN = 60 * 10_000_000
NOW = datetime(2026, 11, 12, 20, 0, tzinfo=timezone.utc)


class FakeJellyfin:
    def __init__(self, episodes=None, fail=False):
        self.fail = fail
        self.episodes = episodes if episodes is not None else [
            {"Id": "e3", "SeriesId": "fo", "SeriesName": "Fallout", "ParentIndexNumber": 2, "IndexNumber": 3,
             "Name": "C", "DateCreated": "2026-11-10T05:00:03.0000000Z"},
            {"Id": "e1", "SeriesId": "fo", "SeriesName": "Fallout", "ParentIndexNumber": 2, "IndexNumber": 1,
             "Name": "A", "DateCreated": "2026-11-10T05:00:02.0000000Z"},
            {"Id": "x9", "SeriesId": "bb", "SeriesName": "The Bear", "ParentIndexNumber": 1, "IndexNumber": 9,
             "Name": "X", "DateCreated": "2026-11-01T05:00:00.0000000Z"},
        ]

    async def get_users(self):
        if self.fail:
            raise Exception("API request failed: 401")
        return [{"Name": "nobody", "Id": "u0"}, {"Name": "eddy", "Id": "u1"}]

    async def search_titles(self, term="", limit=25):
        return [{"Type": "Movie", "Name": "Rush Hour"}, {"Type": "Series", "Name": "The Bear"}]

    async def resume_items(self, user_id, limit=5):
        return [{"Id": "rh"}] if user_id == "u1" else []

    async def get_item(self, item_id, user_id=None):
        return {"Id": "rh", "Type": "Movie", "Name": "Rush Hour", "ProductionYear": 1998,
                "UserData": {"PlaybackPositionTicks": 6 * MIN + 20 * 10_000_000}}

    async def next_up_items(self, user_id, limit=5):
        return [{"SeriesId": "s", "SeriesName": "The Simpsons"}] if user_id == "u1" else []

    async def next_up(self, user_id, series_id):
        return {"SeriesId": series_id, "ParentIndexNumber": 2, "IndexNumber": 10, "Name": "Bart Gets Hit by a Car"}

    async def episodes_by_date_added(self, start=0, limit=100):
        return self.episodes

    async def get_image(self, item_id, image_type="Primary", max_width=1000):
        return b"x" * 4096


def lines(findings):
    return {c.name: (c.ok, c.detail) for c in findings.checks}


def test_jellyfin_checks_all_good(monkeypatch):
    monkeypatch.setattr(config, "JELLYFIN_PUBLIC_URL", "https://jf.example")
    f = asyncio.run(selftest.jellyfin_checks(FakeJellyfin()))
    assert not f.failed
    shown = lines(f)
    assert shown["A user's progress (/watch)"] == (True, "Rush Hour (1998) stopped at 6:20")
    assert shown["Next Up for one show (/watch)"] == (True, "The Simpsons: S02E10 · Bart Gets Hit by a Car")
    assert shown["New-episode poll query"][1].startswith("newest is Fallout S02E03")
    # The newest show's episodes, in order, for the sample DM
    assert [e["Id"] for e in f.sample_episodes] == ["e1", "e3"]
    assert f.poster and shown["Posters download"] == (True, "4 KB")


def test_jellyfin_down_is_one_failure_not_a_crash():
    f = asyncio.run(selftest.jellyfin_checks(FakeJellyfin(fail=True)))
    assert [c.ok for c in f.checks] == [False]
    assert "401" in f.checks[0].detail


def test_missing_public_url_and_wrong_order_fail(monkeypatch):
    monkeypatch.setattr(config, "JELLYFIN_PUBLIC_URL", "")
    jf = FakeJellyfin()
    jf.episodes = list(reversed(jf.episodes))
    shown = lines(asyncio.run(selftest.jellyfin_checks(jf)))
    assert shown["Public link for buttons"][0] is False
    assert shown["New-episode poll query"] == (False, "not newest first")


def test_check_lines():
    assert selftest.Check("A", True, "fine").line == "✅ A: fine"
    assert selftest.Check("B", False).line == "❌ B"
    assert selftest.Check("C", None, "skipped").line == "➖ C: skipped"


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "bot.db")
    monkeypatch.setattr(config, "DB_PATH", path)
    asyncio.run(watch_data.init_database())
    return path


def test_database_checks(db_path):
    shown = lines(selftest.database_checks())
    assert shown["Database tables"][0] is True
    assert shown["Transcode reasons are recorded"][0] is True
    assert shown["New-episode poll has run"] == (False, "no mark yet")
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO bot_state (key, value) VALUES ('episodes_seen_until', '2026-11-12T19:04:12+00:00')")
    assert lines(selftest.database_checks())["New-episode poll has run"] == (True, "seen up to 2026-11-12T19:04")


def test_database_missing_is_a_failure(tmp_path):
    assert selftest.database_checks(str(tmp_path / "nope.db")).failed


# --- the Discord checks ------------------------------------------------------

def http_error(status=403, text="Missing Permissions"):
    return discord.HTTPException(SimpleNamespace(status=status, reason="x"), text)


class FakeEvent:
    """Like discord.py's ScheduledEvent: start() and end() return a new object and
    leave this one as it was, and end() refuses an event that is not active."""

    def __init__(self, fail_at=None, calls=None, status=discord.EventStatus.scheduled):
        self.fail_at, self.status = fail_at, status
        self.calls = calls if calls is not None else []

    async def _step(self, name):
        self.calls.append(name)
        if name == self.fail_at:
            raise http_error()

    async def start(self, reason=None):
        await self._step("start")
        return FakeEvent(self.fail_at, self.calls, discord.EventStatus.active)

    async def end(self, reason=None):
        if self.status is not discord.EventStatus.active:
            raise ValueError("This scheduled event is not active.")
        await self._step("end")
        return FakeEvent(self.fail_at, self.calls, discord.EventStatus.completed)

    async def delete(self, reason=None):
        await self._step("delete")


class FakeGuild:
    def __init__(self, event=None, create_fails=False):
        self.event, self.create_fails, self.created = event, create_fails, []

    async def create_scheduled_event(self, **kw):
        if self.create_fails:
            raise http_error()
        self.created.append(kw)
        return self.event

    def get_member(self, uid):
        return SimpleNamespace(bot=uid == 555)


def test_event_round_trip_creates_starts_ends_deletes():
    event, guild = FakeEvent(), FakeGuild(FakeEvent())
    guild.event = event
    check = asyncio.run(cog.event_round_trip(guild, theatre=object(), now=NOW))
    assert check.ok and check.detail == "created → started → ended → deleted"
    assert event.calls == ["start", "end", "delete"]
    assert guild.created[0]["name"] == cog.TEST_EVENT
    assert guild.created[0]["privacy_level"] is discord.PrivacyLevel.guild_only


def test_event_is_deleted_even_when_starting_fails():
    event = FakeEvent(fail_at="start")
    check = asyncio.run(cog.event_round_trip(FakeGuild(event), theatre=object(), now=NOW))
    assert check.ok is False
    assert event.calls == ["start", "delete"]
    assert check.detail == "created → deleted → failed: Missing Permissions"


def test_event_is_deleted_whatever_goes_wrong():
    class Broken(FakeEvent):
        async def start(self, reason=None):
            self.calls.append("start")
            raise ValueError("This scheduled event is not active.")
    event = Broken()
    check = asyncio.run(cog.event_round_trip(FakeGuild(event), theatre=object(), now=NOW))
    assert check.ok is False and event.calls == ["start", "delete"]
    assert "not active" in check.detail


def test_event_left_behind_says_so():
    check = asyncio.run(cog.event_round_trip(FakeGuild(FakeEvent(fail_at="delete")), theatre=object(), now=NOW))
    assert check.ok is False and "NOT deleted" in check.detail


def test_event_create_refused():
    check = asyncio.run(cog.event_round_trip(FakeGuild(create_fails=True), theatre=object(), now=NOW))
    assert (check.ok, check.detail) == (False, "failed: Missing Permissions")


def channel(**perms):
    allowed = dict(view_channel=True, send_messages=True, embed_links=True, attach_files=True,
                   mention_everyone=False) | perms
    return SimpleNamespace(name="movie-night", permissions_for=lambda me: SimpleNamespace(**allowed))


def test_posting_check():
    ticket = SimpleNamespace(name="🎟️ Ticket Holder", mentionable=True)
    assert cog.posting_check("P", channel(), None, ticket).ok
    shy = SimpleNamespace(name="🎟️ Ticket Holder", mentionable=False)
    check = cog.posting_check("P", channel(attach_files=False), None, shy)
    assert check.ok is False and check.detail == "can't: attach_files, pinging 🎟️ Ticket Holder"
    assert cog.posting_check("P", channel(mention_everyone=True), None, shy).ok
    assert cog.posting_check("P", None, None).ok is False


def test_audience_check_skips_unless_caller_is_in_the_theatre():
    theatre = SimpleNamespace(voice_states={1: None, 555: None, 999: None})   # caller, a bot, this bot
    assert cog.audience_check(FakeGuild(), theatre, 1, 999).detail == "you"
    assert cog.audience_check(FakeGuild(), theatre, 2, 999).ok is None


def test_report_title():
    assert cog.report([selftest.Check("A", True), selftest.Check("B", None)]).title == "🧪 Self-test: all good"
    assert cog.report([selftest.Check("A", False)]).title == "🧪 Self-test: 1 failed"
