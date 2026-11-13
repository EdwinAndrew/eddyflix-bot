"""/spoiler: expiry maths, the #watch-chat line, the table, and the loop taking it off."""
import asyncio

import discord
import pytest

import config
import watch_data
from cogs import movie_night as mn
from cogs import spoiler

NOW = 1_800_000_000


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "bot.db"))
    asyncio.run(watch_data.init_database())


def test_expiry():
    assert spoiler.expiry(NOW, 24) == NOW + 86_400
    assert spoiler.expiry(NOW, 1) == NOW + 3600


def test_announcement():
    first = spoiler.announcement("<@1>", "<@2>", 24, NOW, None)
    assert "<@1> is wearing **🙊 Spoiler Alert** for 24 hours" in first and f"<t:{NOW}:f>" in first and "<@2>" in first
    again = spoiler.announcement("<@1>", "<@2>", 1, NOW, NOW - 60)
    assert "reset" in again and "1 hour from now" in again and f"was <t:{NOW - 60}:R>" in again


def test_spoiler_table(db):
    assert asyncio.run(watch_data.set_spoiler(9, 1, NOW + 100, 2, had_role=False)) is None
    # Given again: the clock moves, had_role stays from the first time
    assert asyncio.run(watch_data.set_spoiler(9, 1, NOW + 500, 3, had_role=True)) == NOW + 100
    assert asyncio.run(watch_data.get_spoiler(9, 1)) == {"expires_at": NOW + 500, "given_by": "3", "had_role": False}
    assert asyncio.run(watch_data.spoiler_until(9, 1, NOW)) == NOW + 500
    assert asyncio.run(watch_data.spoiler_until(9, 1, NOW + 500)) is None   # run out
    assert asyncio.run(watch_data.spoiler_until(8, 1, NOW)) is None         # other server
    assert asyncio.run(watch_data.due_spoilers(NOW)) == []
    assert asyncio.run(watch_data.due_spoilers(NOW + 500)) == [{"guild_id": 9, "discord_user_id": 1, "had_role": False}]
    asyncio.run(watch_data.clear_spoiler(9, 1))
    assert asyncio.run(watch_data.get_spoiler(9, 1)) is None


def test_linked_jellyfin_users(db):
    asyncio.run(watch_data.set_link(1, "Eddy"))
    asyncio.run(watch_data.set_link(2, "bob"))
    assert asyncio.run(watch_data.linked_jellyfin_users()) == {"eddy", "bob"}


class Role:
    def __init__(self, name):
        self.name = name


class Member:
    def __init__(self, roles):
        self.roles = list(roles)

    async def remove_roles(self, *roles, reason=None):
        self.roles = [r for r in self.roles if r not in roles]


class Guild:
    id = 9

    def __init__(self, members):
        self.flair = Role(mn.SPOILER_FLAIR.role_name)
        self.roles = [self.flair]
        self.members = members

    async def fetch_member(self, user_id):
        if user_id not in self.members:
            raise discord.NotFound(type("R", (), {"status": 404, "reason": "gone"})(), "Unknown Member")
        return self.members[user_id]


class Bot:
    def __init__(self, guild):
        self.guild = guild

    def get_guild(self, guild_id):
        return self.guild if guild_id == self.guild.id else None


def test_loop_takes_off_what_ran_out(db, monkeypatch):
    guild = Guild({})
    picked, punished = Member([guild.flair]), Member([guild.flair])
    guild.members = {1: punished, 2: picked}
    asyncio.run(watch_data.set_spoiler(9, 1, NOW - 1, 5, had_role=False))
    asyncio.run(watch_data.set_spoiler(9, 2, NOW - 1, 5, had_role=True))    # wore it by choice
    asyncio.run(watch_data.set_spoiler(9, 3, NOW - 1, 5, had_role=False))   # left the server
    asyncio.run(watch_data.set_spoiler(9, 4, NOW + 60, 5, had_role=False))  # still running
    monkeypatch.setattr(spoiler.time, "time", lambda: NOW)

    cog = spoiler.Spoiler(Bot(guild))
    asyncio.run(cog.expire.coro(cog))   # one pass of the loop

    assert punished.roles == [] and picked.roles == [guild.flair]
    assert asyncio.run(watch_data.due_spoilers(NOW + 60)) == [{"guild_id": 9, "discord_user_id": 4, "had_role": False}]
