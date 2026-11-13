"""The movie night loop against fake Discord objects: one reminder, a start once
someone is in the Theatre, one recap, and nothing for cancelled or deleted events."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import discord
import pytest

import config
import watch_data
from cogs import movie_night as mn

BOT_ID = 999
START = datetime(2026, 11, 13, 1, 0, tzinfo=timezone.utc)


class FakeEvent:
    def __init__(self, event_id, name="🎬 1917 (2019)"):
        self.id, self.name, self.creator_id = event_id, name, BOT_ID
        self.status = discord.EventStatus.scheduled
        self.start_time, self.end_time = START, START + timedelta(minutes=135)
        self.channel, self.url = None, f"https://discord.com/events/7/{event_id}"

    async def start(self, reason=None):
        self.status = discord.EventStatus.active

    async def end(self, reason=None):
        self.status = discord.EventStatus.completed


class FakeChannel:
    def __init__(self, name):
        self.name, self.mention, self.sent, self.voice_states = name, f"<#{name}>", [], {}

    async def send(self, content=None, embed=None, **kw):
        self.sent.append((content, embed))


class FakeGuild:
    def __init__(self):
        self.id, self.roles, self.events = 7, [], {}
        self.announce, self.theatre = FakeChannel(mn.ANNOUNCE_CHANNEL), FakeChannel(mn.VOICE_CHANNEL)
        self.text_channels, self.voice_channels = [self.announce], [self.theatre]

    @property
    def scheduled_events(self):
        return list(self.events.values())

    def get_scheduled_event(self, event_id):
        return self.events.get(event_id)

    async def fetch_scheduled_event(self, event_id, with_counts=True):
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown event")

    def get_member(self, uid):
        return None


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "bot.db"))
    asyncio.run(watch_data.init_database())
    guild = FakeGuild()
    guild.events[1] = FakeEvent(1)
    bot = SimpleNamespace(guilds=[guild], get_guild=lambda gid: guild, user=SimpleNamespace(id=BOT_ID))
    clock = {"now": START - timedelta(days=1)}
    monkeypatch.setattr(discord.utils, "utcnow", lambda: clock["now"])
    monkeypatch.setattr(discord.utils, "snowflake_time", lambda _id: START - timedelta(days=2))

    def tick(minutes, cog=None):
        clock["now"] = START + timedelta(minutes=minutes)
        cog = cog or state["cog"]
        asyncio.run(cog.follow_nights.coro(cog))
    state = {"cog": mn.MovieNight(bot)}
    return SimpleNamespace(guild=guild, bot=bot, tick=tick, state=state)


def test_a_whole_night(setup):
    g = setup.guild
    setup.tick(-60)                                  # found in Discord, nothing due yet
    assert g.announce.sent == []
    setup.tick(-14)
    setup.tick(-13)
    assert len(g.announce.sent) == 1                 # one reminder
    content, embed = g.announce.sent[0]
    assert "starts <t:" in content and embed.title == "🍿 Starting soon: 1917 (2019)"

    setup.tick(0)                                    # start time, Theatre still empty
    assert g.events[1].status is discord.EventStatus.scheduled
    g.theatre.voice_states = {10: object(), 11: object(), BOT_ID: object()}
    setup.tick(1)
    assert g.events[1].status is discord.EventStatus.active

    setup.state["cog"] = mn.MovieNight(setup.bot)    # a restart mid-film
    for m in range(2, 30):
        setup.tick(m)
    g.theatre.voice_states = {10: object(), 12: object()}
    for m in range(30, 35):
        setup.tick(m)
    assert asyncio.run(watch_data.theatre_minutes(1)) == {10: 34, 11: 29, 12: 5}

    setup.tick(135)                                  # end time
    assert g.events[1].status is discord.EventStatus.completed
    assert len(g.announce.sent) == 2
    fields = {f.name: f.value for f in g.announce.sent[1][1].fields}
    assert fields["🎥 In the Theatre (2)"] == "<@10> · 1st movie night\n<@11> · 1st movie night"
    assert fields["Dropped by"] == "<@12>"

    setup.state["cog"] = mn.MovieNight(setup.bot)    # another restart: no second recap
    setup.tick(140)
    assert len(g.announce.sent) == 2


def test_discord_ending_the_event_early_does_not_end_the_night(setup):
    g = setup.guild
    g.theatre.voice_states = {10: object()}
    setup.tick(0)
    setup.tick(1)
    g.events[1].status = discord.EventStatus.completed   # by hand, or Discord after a break
    setup.tick(2)
    assert len(g.announce.sent) == 0
    assert asyncio.run(watch_data.theatre_minutes(1)) == {10: 3}         # still counting


def test_cancelled_and_deleted_events_get_no_recap(setup):
    g = setup.guild
    g.events[2] = FakeEvent(2)
    g.theatre.voice_states = {10: object()}
    setup.tick(0)
    setup.tick(30)
    g.events[1].status = discord.EventStatus.cancelled
    del g.events[2]                                       # deleted by hand
    setup.tick(31)
    setup.tick(200)
    assert g.announce.sent == []
    assert asyncio.run(watch_data.open_movie_nights()) == []
