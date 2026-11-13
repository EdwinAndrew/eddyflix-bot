"""Movie night ratings and history: who may rate, the 24 hours, one rating per member
per night, the history lines and the top-rated list, and the button itself."""
import asyncio
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import discord
import pytest

import config
import ratings
import screenings
import watch_data

GUILD = 7
START = datetime(2026, 11, 14, 1, 0, tzinfo=timezone.utc)     # Fri Nov 13, 8 PM in Toronto


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "bot.db"))
    asyncio.run(watch_data.init_database())


def night(event_id, title, days_ago=0, status="done", item_id=None, host="Eddy", guild=GUILD):
    async def go():
        start = START - timedelta(days=days_ago)
        await watch_data.add_movie_night(event_id, guild, title, title.split(" (")[0], item_id, host,
                                         start, start + timedelta(hours=2), start - timedelta(days=1))
        await watch_data.update_movie_night(event_id, status=status)
    asyncio.run(go())


def theatre(event_id, minutes: dict):
    for uid, m in minutes.items():
        asyncio.run(watch_data.add_theatre_minutes(event_id, [uid], m))


def rate(event_id, uid, stars):
    asyncio.run(watch_data.set_rating(event_id, uid, stars))


# --- the rules -------------------------------------------------------------------

def test_open_for_24_hours_from_the_recap():
    posted = START
    assert ratings.is_open(posted, posted + timedelta(hours=23, minutes=59))
    assert not ratings.is_open(posted, posted + timedelta(hours=24))


def test_who_may_rate():
    minutes = {10: 90, 11: 5}
    assert ratings.can_rate(10, minutes)          # attended
    assert ratings.can_rate(11, minutes)          # dropped by: was in the Theatre
    assert not ratings.can_rate(12, minutes)      # never there
    assert ratings.can_rate(12, {})               # nobody counted: anyone
    assert ratings.can_rate(12, {10: 0})


def test_field_text_shows_the_running_average():
    closes = START + timedelta(hours=24)
    unix = int(closes.timestamp())
    assert ratings.field_text([], closes) == f"How was it? Everyone who was in the Theatre can rate it, until <t:{unix}:f>."
    assert ratings.field_text([5, 4, 4], closes).startswith("**4.3** / 5 from 3 ratings.")
    assert "from 1 rating." in ratings.field_text([3], closes)


def test_history_line():
    row = {"title": "1917 (2019)", "host": "Eddy", "start_time": START, "attended": 3, "average": 13 / 3, "votes": 3}
    assert ratings.history_line(row, "https://jf/1917") == \
        "**Fri Nov 13, 2026** · [1917 (2019)](https://jf/1917) · hosted by Eddy · 3 came · 🍿 4.3 (3)"
    row.update(host=None, attended=0, average=None, votes=0)
    assert ratings.history_line(row, None) == "**Fri Nov 13, 2026** · 1917 (2019) · nobody in the Theatre · not rated"


def test_top_lines():
    assert ratings.top_lines([("Dune", 5.0, 2), ("1917", 4.5, 4), ("Cats", 1.0, 1), ("Up", 1.0, 1)]) == (
        "🥇 **Dune** · 🍿 5.0 (2)\n🥈 **1917** · 🍿 4.5 (4)\n🥉 **Cats** · 🍿 1.0 (1)\n4. **Up** · 🍿 1.0 (1)")


# --- the database ----------------------------------------------------------------

def test_rating_again_replaces(db):
    night(1, "1917 (2019)")
    rate(1, 10, 3)
    rate(1, 10, 5)
    rate(1, 11, 4)
    assert asyncio.run(watch_data.night_ratings(1)) == {10: 5, 11: 4}


def test_history_newest_first_with_attendance_and_average(db):
    night(1, "1917 (2019)", days_ago=14, item_id="a")
    night(2, "Dune (2021)", days_ago=7, item_id="b", host=None)
    night(3, "Cats (2019)", days_ago=3, status="cancelled")
    night(4, "Up (2009)", days_ago=1, status="live")
    night(5, "Elsewhere", days_ago=2, guild=8)
    theatre(1, {10: 90, 11: 60, 12: screenings.ATTEND_MINUTES - 1})
    rate(1, 10, 5)
    rate(1, 11, 4)
    rows = asyncio.run(watch_data.finished_nights(GUILD))
    assert [(r["title"], r["attended"], r["average"], r["votes"]) for r in rows] == [
        ("Dune (2021)", 0, None, 0),
        ("1917 (2019)", 2, 4.5, 2),          # the drop-by does not count as came
    ]
    assert rows[1]["start_time"] == START - timedelta(days=14)
    assert len(asyncio.run(watch_data.finished_nights(GUILD, limit=1))) == 1


def test_top_rated_counts_a_film_shown_twice_once(db):
    night(1, "1917 (2019)", days_ago=30, item_id="a")
    night(2, "1917 (2019)", days_ago=10, item_id="a")
    night(3, "Dune (2021)", days_ago=5, item_id="b")
    night(4, "Cats (2019)", days_ago=4, status="cancelled", item_id="c")
    for event_id, uid, stars in [(1, 10, 5), (2, 10, 4), (2, 11, 3), (3, 10, 5), (4, 10, 1)]:
        rate(event_id, uid, stars)
    assert asyncio.run(watch_data.top_rated_films(GUILD)) == [("Dune (2021)", 5.0, 1), ("1917 (2019)", 4.0, 3)]


# --- the button ------------------------------------------------------------------

class FakeMessage:
    def __init__(self, posted, embed):
        self.created_at, self.embeds, self.edits = posted, [embed], []

    async def edit(self, embed=None, **kw):
        self.edits.append(embed)


class FakeResponse:
    def __init__(self):
        self.sent = []

    async def send_message(self, content, ephemeral=False):
        self.sent.append(content)


def click(event_id, stars, uid, message):
    interaction = SimpleNamespace(user=SimpleNamespace(id=uid), message=message, response=FakeResponse())

    async def go():
        item = discord.ui.Button(custom_id=f"movienight:rate:{event_id}:{stars}")
        match = re.fullmatch(ratings.RateButton.__discord_ui_compiled_template__, item.custom_id)
        button = await ratings.RateButton.from_custom_id(interaction, item, match)
        await button.callback(interaction)
    asyncio.run(go())
    return interaction.response.sent[-1]


def recap(posted):
    embed = discord.Embed(title="🎬 That's a wrap: 1917 (2019)")
    embed.add_field(name="🎥 In the Theatre (1)", value="<@10>")
    embed.add_field(name=ratings.FIELD, value=ratings.field_text([], posted + ratings.WINDOW))
    return FakeMessage(posted, embed)


def test_the_button_rates_and_updates_the_recap(db, monkeypatch):
    night(1, "1917 (2019)")
    theatre(1, {10: 90, 11: 4})
    message = recap(START)
    monkeypatch.setattr(discord.utils, "utcnow", lambda: START + timedelta(hours=2))
    assert "4/5" in click(1, 4, 10, message)
    assert "5/5" in click(1, 5, 11, message)                       # dropped by still counts
    assert asyncio.run(watch_data.night_ratings(1)) == {10: 4, 11: 5}
    assert message.edits[-1].fields[1].value.startswith("**4.5** / 5 from 2 ratings")
    assert len(message.edits[-1].fields) == 2                      # replaced, not added

    assert "Only people who were in the Theatre" in click(1, 1, 12, message)
    monkeypatch.setattr(discord.utils, "utcnow", lambda: START + timedelta(hours=25))
    assert "closed" in click(1, 1, 10, message)
    assert asyncio.run(watch_data.night_ratings(1)) == {10: 4, 11: 5}


def test_the_view_has_five_buttons_with_the_event_in_their_ids():
    async def go():
        return [c.custom_id for c in ratings.rating_view("1234").children]
    assert asyncio.run(go()) == [f"movienight:rate:1234:{n}" for n in range(1, 6)]
