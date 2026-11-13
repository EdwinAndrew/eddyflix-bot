"""/request against a fake Seerr and fake Discord: requests made as the right
user, one DM per request even across restarts, and the closed-DM fallback."""
import asyncio
from types import SimpleNamespace

import discord
import pytest

import config
import media_requests as mr
import watch_data
from cogs import requests as rq

ITEM = "a" * 32


class FakeSeerr:
    def __init__(self):
        self.requests, self.made, self.details_calls = {}, [], []
        self.users_list = [{"id": 1, "jellyfinUsername": "eddy"}, {"id": 4, "jellyfinUsername": "Kevin"}]
        self.seasons = {}

    async def create_request(self, media_type, tmdb_id, seasons=None, user_id=None):
        self.made.append((media_type, tmdb_id, seasons, user_id))
        return {"id": 40 + len(self.made), "status": mr.REQUEST_PENDING if user_id else mr.REQUEST_APPROVED}

    async def users(self):
        return self.users_list

    async def get_request(self, request_id):
        return self.requests.get(request_id)

    async def details(self, media_type, tmdb_id):
        self.details_calls.append((media_type, tmdb_id))
        return {"mediaInfo": {"seasons": [{"seasonNumber": n, "status": s} for n, s in self.seasons.items()]}}

    async def poster(self, path, width="w500"):
        return b"JPEG" if path else None


class FakeUser:
    def __init__(self, user_id, dms_open=True):
        self.id, self.dms_open, self.sent = user_id, dms_open, []

    def __str__(self):
        return f"user{self.id}"

    async def send(self, content=None, embed=None, **kw):
        if not self.dms_open:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Cannot send messages to this user")
        self.sent.append((content, embed, kw))


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, embed=None, **kw):
        self.sent.append((content, embed, kw))


class FakeBot:
    def __init__(self, users):
        self.users, self.channel = {u.id: u for u in users}, FakeChannel()

    def get_user(self, user_id):
        return self.users.get(user_id)

    async def fetch_user(self, user_id):
        return self.users[user_id]

    def get_channel(self, channel_id):
        return self.channel if channel_id == 555 else None


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "bot.db")
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "ACTIVITY_CHANNEL_ID", 555)
    monkeypatch.setattr(config, "JELLYFIN_PUBLIC_URL", "https://jf.example")
    asyncio.run(watch_data.init_database())
    return path


def cog_with(users, seerr=None):
    cog = rq.Requests(FakeBot(users))
    cog.seerr = seerr or FakeSeerr()
    return cog


FILM = {"id": 353081, "title": "Up", "releaseDate": "2009-05-28", "posterPath": "/up.jpg", "mediaInfo": None}
SHOW = {"id": 106379, "name": "Fallout", "firstAirDate": "2024-04-10", "posterPath": "/f.jpg",
        "seasons": [{"seasonNumber": n, "episodeCount": 8} for n in (1, 2)], "mediaInfo": None}


def done(kind="movie", media_status=mr.MEDIA_AVAILABLE, status=mr.REQUEST_COMPLETED, seasons=()):
    return {"id": 0, "type": kind, "status": status, "media": {"status": media_status, "jellyfinMediaId": ITEM},
            "seasons": [{"seasonNumber": n, "status": s} for n, s in seasons]}


# --- making requests ---------------------------------------------------------

def test_linked_member_requests_as_their_seerr_user(db_path):
    cog = cog_with([])
    asyncio.run(watch_data.set_link(111, "kevin"))
    user = SimpleNamespace(id=111)
    seerr_id, name = asyncio.run(cog.seerr_user(111))
    assert (seerr_id, name) == (4, "kevin")
    text = asyncio.run(cog.place_request(user, "tv", SHOW, [2], seerr_id, name))
    assert cog.seerr.made == [("tv", 106379, [2], 4)]
    assert "as **kevin**" in text and "waiting for approval" in text


def test_unlinked_member_cannot_request(db_path):
    # As the API key's user (the admin) the request would skip approval
    cog = cog_with([])
    assert asyncio.run(cog.seerr_user(222)) == (None, None)
    text = asyncio.run(cog.place_request(SimpleNamespace(id=222), "movie", FILM, None, None, None))
    assert cog.seerr.made == []
    assert text == rq.NOT_LINKED and "/jf-link" in text


def test_no_request_buttons_unless_linked():
    assert rq.RequestView(SimpleNamespace(), 1, "tv", SHOW, None, None).children == []
    assert rq.request_card("tv", SHOW, None).footer.text == rq.NOT_LINKED


def test_linked_but_not_in_seerr_falls_back_to_the_key_user(db_path):
    cog = cog_with([])
    asyncio.run(watch_data.set_link(333, "Someone"))
    assert asyncio.run(cog.seerr_user(333)) == (None, None)


def test_refusal_is_explained_and_nothing_is_noted(db_path):
    seerr = FakeSeerr()

    async def refuse(*a, **kw):
        raise rq.SeerrError(409, "Request for this media already exists.")
    seerr.create_request = refuse
    cog = cog_with([], seerr)
    assert "already asked" in asyncio.run(cog.place_request(SimpleNamespace(id=1), "movie", FILM, None, 4, "kevin"))
    assert asyncio.run(waiting()) == {}


async def waiting():
    import aiosqlite
    async with aiosqlite.connect(config.DB_PATH) as db:
        return await mr.waiting(db)


def test_buttons_for_a_new_show():
    view = rq.RequestView(SimpleNamespace(), 1, "tv", SHOW, 4, "kevin")
    assert [b.label for b in view.children] == ["Request all seasons", "Request season 2 only"]


def test_buttons_for_a_film_in_the_library(monkeypatch):
    monkeypatch.setattr(config, "JELLYFIN_PUBLIC_URL", "https://jf.example")
    film = {**FILM, "mediaInfo": {"status": mr.MEDIA_AVAILABLE, "jellyfinMediaId": ITEM}}
    view = rq.RequestView(SimpleNamespace(), 1, "movie", film, None, None)
    assert [(b.label, b.url) for b in view.children] == [
        ("Open in Jellyfin", f"https://jf.example/web/#/details?id={ITEM}")]


def test_buttons_for_a_film_already_asked_for():
    film = {**FILM, "mediaInfo": {"status": mr.MEDIA_PENDING, "requests": [{"id": 9, "status": mr.REQUEST_PENDING}]}}
    view = rq.RequestView(SimpleNamespace(), 1, "movie", film, None, None)
    assert [b.label for b in view.children] == ["DM me when it's ready"]


def test_off_without_settings(monkeypatch):
    monkeypatch.setattr(config, "SEERR_URL", "")
    monkeypatch.setattr(config, "SEERR_API_KEY", "")
    cog = rq.Requests(FakeBot([]))
    assert cog.seerr is None
    asyncio.run(cog.on_services_ready())
    assert not cog.check_requests.is_running()


def test_request_answers_not_set_up_without_settings(monkeypatch):
    monkeypatch.setattr(config, "SEERR_URL", "")
    cog = rq.Requests(FakeBot([]))
    sent = []

    async def send_message(text, ephemeral=False):
        sent.append((text, ephemeral))
    interaction = SimpleNamespace(response=SimpleNamespace(send_message=send_message))
    asyncio.run(cog.request.callback(cog, interaction, "up"))
    assert sent == [("Requests aren't set up on this server.", True)]


# --- the poll ----------------------------------------------------------------

def test_one_dm_when_ready_even_after_a_restart(db_path):
    kevin = FakeUser(111)
    cog = cog_with([kevin])
    asyncio.run(cog.place_request(kevin, "movie", FILM, None, 4, "Kevin"))
    request_id = 41

    cog.seerr.requests[request_id] = done(status=mr.REQUEST_APPROVED, media_status=mr.MEDIA_PROCESSING)
    asyncio.run(cog.poll())
    assert kevin.sent == []

    cog.seerr.requests[request_id] = done()
    asyncio.run(cog.poll())
    asyncio.run(cog.poll())
    assert len(kevin.sent) == 1
    _, embed, kw = kevin.sent[0]
    assert embed.title == "🍿 Up (2009) is ready"
    assert embed.url == f"https://jf.example/web/#/details?id={ITEM}"
    assert embed.thumbnail.url == "attachment://poster.jpg" and kw["files"][0].filename == "poster.jpg"

    # A restart is a new cog on the same database
    restarted = cog_with([kevin], cog.seerr)
    asyncio.run(restarted.poll())
    assert len(kevin.sent) == 1


def test_everyone_waiting_gets_one_dm_and_closed_dms_get_a_mention(db_path):
    asker, follower = FakeUser(111), FakeUser(222, dms_open=False)
    cog = cog_with([asker, follower])
    asyncio.run(cog.place_request(asker, "movie", FILM, None, 4, "kevin"))
    asked_for = {**FILM, "mediaInfo": {"status": mr.MEDIA_PENDING, "requests": [{"id": 41, "status": mr.REQUEST_APPROVED}]}}
    asyncio.run(cog.follow(follower, "movie", asked_for))
    asyncio.run(cog.follow(follower, "movie", asked_for))       # twice: still one row

    cog.seerr.requests[41] = done()
    asyncio.run(cog.poll())
    asyncio.run(cog.poll())
    assert len(asker.sent) == 1
    assert len(cog.bot.channel.sent) == 1
    content, embed, kw = cog.bot.channel.sent[0]
    assert content.startswith("<@222>") and "DMs are closed" in content
    assert [u.id for u in kw["allowed_mentions"].users] == [222]


def test_a_show_is_announced_once_its_seasons_are_partly_in(db_path):
    kevin = FakeUser(111)
    cog = cog_with([kevin])
    asyncio.run(cog.place_request(kevin, "tv", SHOW, [2], 4, "Kevin"))
    cog.seerr.requests[41] = done("tv", mr.MEDIA_PARTIAL, mr.REQUEST_APPROVED, [(2, mr.REQUEST_APPROVED)])

    cog.seerr.seasons = {1: mr.MEDIA_AVAILABLE, 2: mr.MEDIA_UNKNOWN}
    asyncio.run(cog.poll())
    assert kevin.sent == [] and cog.seerr.details_calls == [("tv", 106379)]

    cog.seerr.seasons = {1: mr.MEDIA_AVAILABLE, 2: mr.MEDIA_PARTIAL}
    asyncio.run(cog.poll())
    assert len(kevin.sent) == 1
    assert kevin.sent[0][1].description.startswith("Season 2, which you asked for, is on Jellyfin.")


def test_declined_and_deleted_requests_are_settled(db_path):
    kevin = FakeUser(111)
    cog = cog_with([kevin])
    asyncio.run(cog.place_request(kevin, "movie", FILM, None, 4, "Kevin"))        # 41: declined
    asyncio.run(cog.place_request(kevin, "tv", SHOW, [1, 2], 4, "Kevin"))         # 42: deleted in Seerr
    cog.seerr.requests[41] = done(status=mr.REQUEST_DECLINED, media_status=mr.MEDIA_UNKNOWN)
    asyncio.run(cog.poll())
    asyncio.run(cog.poll())
    assert [c for c, _, _ in kevin.sent] == ["Your request for **Up (2009)** was declined in Seerr."]
    assert asyncio.run(waiting()) == {}


def test_card(monkeypatch):
    monkeypatch.setattr(config, "JELLYFIN_PUBLIC_URL", "https://jf.example")
    film = {**FILM, "overview": "A house flies.", "runtime": 96, "genres": [{"name": "Animation"}]}
    embed = rq.request_card("movie", film, "Kevin")
    assert embed.title == "🎬 Up (2009)" and embed.url is None
    assert [(f.name, f.value) for f in embed.fields] == [
        ("Status", "➕ Not in the library yet. It can be requested"), ("Runtime", "1h 36m"), ("Genre", "Animation")]
    assert embed.footer.text == "Requests are made as your Seerr user, Kevin."
    assert "/jf-link" in rq.request_card("movie", film, None).footer.text
    there = {**SHOW, "status": "Returning Series",
             "mediaInfo": {"status": mr.MEDIA_AVAILABLE, "jellyfinMediaId": ITEM}}
    embed = rq.request_card("tv", there, None)
    assert embed.url == f"https://jf.example/web/#/details?id={ITEM}"
    assert embed.fields[1].value == "2 seasons · returning series"
    assert embed.footer.text is None
