"""Watch parties: working out who is watching together, and what the feed posts."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import config
import parties
import watch_data
from cogs.activity import Activity

FILM = {"Id": "film1", "Name": "1917", "Type": "Movie", "ProductionYear": 2019}
OTHER = {"Id": "film2", "Name": "Big World", "Type": "Movie"}


def session(sid, user, item=FILM, minutes=10.0):
    return {"Id": sid, "UserName": user, "DeviceName": f"{user}'s TV", "NowPlayingItem": item,
            "PlayState": {"PositionTicks": int(minutes * 60 * parties.TICKS_PER_SECOND)}}


def idle(sid, user):
    return {"Id": sid, "UserName": user, "DeviceName": f"{user}'s TV"}


# --- detection ------------------------------------------------------------------

def test_two_people_on_the_same_film_together():
    found = parties.find_parties([session("a", "eddy"), session("b", "Sarah", minutes=10.5)])
    assert list(found) == ["film1"]
    assert found["film1"]["users"] == ["eddy", "Sarah"]
    assert found["film1"]["session_ids"] == {"a", "b"}


def test_far_apart_is_not_together():
    assert parties.find_parties([session("a", "eddy", minutes=5), session("b", "Sarah", minutes=50)]) == {}


def test_one_person_on_two_devices_is_not_a_party():
    assert parties.find_parties([session("a", "eddy"), session("b", "eddy", minutes=10.2)]) == {}


def test_different_films_are_not_a_party():
    assert parties.find_parties([session("a", "eddy"), session("b", "Sarah", item=OTHER)]) == {}


def test_the_biggest_group_on_a_film_wins():
    found = parties.find_parties([session("a", "eddy", minutes=10), session("b", "Sarah", minutes=11),
                                  session("c", "Kevin", minutes=12), session("d", "Julie", minutes=80)])
    assert found["film1"]["users"] == ["eddy", "Kevin", "Sarah"]


@pytest.mark.parametrize("names, text", [
    (["eddy"], "**eddy**"),
    (["eddy", "Sarah"], "**eddy** and **Sarah**"),
    (["eddy", "Sarah", "Kevin"], "**eddy**, **Sarah** and **Kevin**"),
])
def test_join_names(names, text):
    assert parties.join_names(names) == text


def test_titles():
    assert parties.title_of(FILM) == "1917 (2019)"
    episode = {"Type": "Episode", "SeriesName": "Outer Banks", "ParentIndexNumber": 1, "IndexNumber": 3}
    assert parties.title_of(episode) == "Outer Banks S1E3"


def test_ended_embed_says_how_long():
    start = datetime(2026, 10, 22, 20, 0)
    embed = parties.ended_embed(FILM, ["eddy", "Sarah"], start, start + timedelta(minutes=123))
    assert embed.description == "**eddy** and **Sarah** watched **1917 (2019)** together, for 2h 03m"


# --- the feed, poll by poll ---------------------------------------------------------

class Message:
    def __init__(self, embed):
        self.embeds, self.attachments, self.edits = [embed], [], []

    async def edit(self, embed):
        self.edits.append(embed)


class Channel:
    def __init__(self):
        self.messages = []

    async def send(self, embed=None, files=None):
        self.messages.append(Message(embed))
        return self.messages[-1]


class Jellyfin:
    def __init__(self):
        self.sessions = []

    async def get_sessions(self):
        return self.sessions

    async def get_image(self, *args, **kwargs):
        return None

    async def get_item(self, item_id):
        return {}


@pytest.fixture
def feed(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "bot.db"))
    asyncio.run(watch_data.init_database())
    jf, channel = Jellyfin(), Channel()
    cog = Activity(SimpleNamespace(jf=jf, get_channel=lambda _id: channel))

    def poll(*sessions):
        jf.sessions = list(sessions)
        asyncio.run(cog.check_sessions.coro(cog))
        return [m.embeds[0].title for m in channel.messages]

    return poll, channel


def test_a_watch_party_from_start_to_end(feed):
    poll, channel = feed
    poll(idle("a", "eddy"), idle("b", "Sarah"), idle("c", "Kevin"))     # first poll after start: quiet
    assert channel.messages == []

    titles = poll(session("a", "eddy", minutes=0.2), session("b", "Sarah", minutes=0.1), idle("c", "Kevin"))
    assert titles == ["🍿 Watching together"]                               # one post, not two "started"
    party_post = channel.messages[0]
    assert "**eddy** and **Sarah**" in party_post.embeds[0].description

    poll(session("a", "eddy", minutes=1), session("b", "Sarah", minutes=1), session("c", "Kevin", minutes=1.5))
    assert "**eddy**, **Kevin** and **Sarah**" in party_post.edits[-1].description
    assert len(channel.messages) == 1                                       # Kevin joining edits it

    titles = poll(idle("a", "eddy"), idle("b", "Sarah"), idle("c", "Kevin"))
    assert party_post.edits[-1].title == "🎬 Watched together"
    assert "⏹️ Playback Stopped" not in titles                              # no stop posts for the party


def test_someone_watching_alone_still_gets_their_own_post(feed):
    poll, channel = feed
    poll(idle("a", "eddy"))
    assert poll(session("a", "eddy")) == ["▶️ Playback Started"]
    assert poll(idle("a", "eddy")) == ["▶️ Playback Started", "⏹️ Playback Stopped"]


def test_a_party_already_running_at_start_is_tracked_quietly(feed):
    poll, channel = feed
    poll(session("a", "eddy"), session("b", "Sarah"))                        # bot restarted mid-film
    poll(idle("a", "eddy"), idle("b", "Sarah"))
    assert channel.messages == []
