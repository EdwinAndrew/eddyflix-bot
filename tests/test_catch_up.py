"""The catch-up offer: finishing the newest episode of a show offers new-episode DMs
once, with a 🔔 button that subscribes as /jf-subscribe does and survives a restart."""
import asyncio
import re
from types import SimpleNamespace

import aiosqlite
import discord
import pytest

import catch_up
import config
import new_episodes
import watch_data
from cogs.activity import Activity
from cogs.subscriptions import Subscriptions

FALLOUT = "9f70f09353d6bba2d7d5652f45cca41d"
RUNTIME = 36_000_000_000          # an hour, in ticks


def episode(number, season=2, **extra):
    return {"Id": f"{FALLOUT}-{season}-{number}", "Name": f"Episode {number}", "Type": "Episode",
            "SeriesId": FALLOUT, "SeriesName": "Fallout", "ParentIndexNumber": season, "IndexNumber": number,
            "RunTimeTicks": RUNTIME, "LocationType": "FileSystem", **extra}


def session(number, season=2, played=0.95, user="Eddy", sid="s1", **extra):
    """A session as the activity cog last saw it, that far into the episode."""
    return {"Id": sid, "UserName": user, "UserId": "jf-eddy", "DeviceName": "TV",
            "NowPlayingItem": episode(number, season, **extra),
            "PlayState": {"PositionTicks": int(RUNTIME * played), "PlayMethod": "DirectPlay"}}


# --- the rules -------------------------------------------------------------------

def test_how_far_in():
    assert catch_up.progress(session(1, played=0.5)) == 0.5
    assert catch_up.reached_end(session(1, played=0.9))
    assert not catch_up.reached_end(session(1, played=0.89))
    assert catch_up.progress({"NowPlayingItem": {"Id": "x"}, "PlayState": {"PositionTicks": 5}}) is None
    assert not catch_up.reached_end({"NowPlayingItem": episode(1)})


def test_latest_skips_specials_and_missing_episodes():
    eps = [episode(1, season=0), episode(9, LocationType="Virtual"), episode(8), episode(3, season=1)]
    assert catch_up.latest(eps)["IndexNumber"] == 8
    assert catch_up.latest([episode(5, season=0)]) is None
    assert catch_up.latest([]) is None
    two_part = episode(7, IndexNumberEnd=8)
    assert catch_up.latest([episode(7), two_part])["Id"] == two_part["Id"]


def test_newest_first():
    assert catch_up.newest_first([episode(3), episode(2), episode(9, season=1), episode(1, season=0)])
    assert not catch_up.newest_first([episode(2), episode(3)])
    assert catch_up.newest_first([])


def test_caught_up_means_the_newest_episode():
    newest = episode(8)
    assert catch_up.caught_up(episode(8), newest)
    assert catch_up.caught_up(episode(8, Id="another-file"), newest)       # same episode, other file
    assert not catch_up.caught_up(episode(7), newest)
    assert not catch_up.caught_up(episode(8, season=1), newest)
    assert not catch_up.caught_up(episode(9, season=0), newest)            # a special
    assert catch_up.caught_up(episode(7, IndexNumberEnd=8), newest)        # a two-part file ending on it
    assert not catch_up.caught_up(episode(8), None)


# --- the offer, against a throwaway database --------------------------------------

class FakeJellyfin:
    def __init__(self, episodes, status="Continuing", played=False, sorted_=True):
        self.episodes, self.status, self.played, self.sorted = episodes, status, played, sorted_
        self.calls = []

    async def newest_episodes(self, series_id, limit=10):
        self.calls.append(("newest", limit))
        eps = sorted(self.episodes, key=lambda e: (e["ParentIndexNumber"], e["IndexNumber"]), reverse=True) \
            if self.sorted else list(self.episodes)
        return eps[:limit] if limit else eps

    async def get_item(self, item_id, user_id=None):
        self.calls.append(("item", item_id, user_id))
        if item_id == FALLOUT:
            return {"Id": FALLOUT, "Name": "Fallout", "Type": "Series", "Status": self.status}
        return {"Id": item_id, "UserData": {"Played": self.played}}

    async def get_user_id(self, name):
        return "jf-eddy"

    async def get_image(self, item_id, image_type="Primary", max_width=1000):
        return b"\xff\xd8 jpeg"


class FakeUser:
    def __init__(self, closed=False):
        self.closed, self.sent = closed, []

    async def send(self, **kwargs):
        if self.closed:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Cannot send messages to this user")
        self.sent.append(kwargs)


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "bot.db")
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "ACTIVITY_CHANNEL_ID", 42)
    asyncio.run(watch_data.init_database())
    asyncio.run(watch_data.set_link(100, "eddy"))
    return path


def sql(path, query, args=()):
    async def go():
        async with aiosqlite.connect(path) as db:
            async with db.execute(query, args) as cursor:
                rows = await cursor.fetchall()
            await db.commit()
            return rows
    return asyncio.run(go())


def make_cog(jf, users, channel=None):
    bot = SimpleNamespace(jf=jf, get_user=lambda uid: users.get(uid),
                          get_channel=lambda cid: channel if cid == 42 else None)

    async def fetch_user(uid):
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown User")
    bot.fetch_user = fetch_user
    return Subscriptions(bot)


def offer(cog, sess):
    asyncio.run(cog.offer_if_caught_up(sess))


SEASON = [episode(n) for n in range(1, 9)] + [episode(n, season=1) for n in range(1, 9)]


def test_finishing_the_newest_episode_offers_once(db_path):
    user, jf = FakeUser(), FakeJellyfin(SEASON)
    cog = make_cog(jf, {100: user})
    offer(cog, session(8))
    assert len(user.sent) == 1
    sent = user.sent[0]
    assert sent["embed"].title == "🔔 You're caught up on Fallout"
    assert sent["embed"].thumbnail.url == "attachment://poster.jpg" and len(sent["files"]) == 1
    assert [c.custom_id for c in sent["view"].children] == [f"subscribe:offer:{FALLOUT}:100"]
    assert sql(db_path, "SELECT discord_user_id, series_id, series_name FROM subscription_offers") == \
        [("100", FALLOUT, "Fallout")]
    assert jf.calls[0] == ("newest", 10)                       # one short page

    offer(cog, session(8))                                      # watched it again: never asked twice
    offer(make_cog(jf, {100: user}), session(8))                # nor after a restart
    assert len(user.sent) == 1


def test_an_earlier_episode_offers_nothing_yet(db_path):
    user = FakeUser()
    offer(make_cog(FakeJellyfin(SEASON), {100: user}), session(7))
    assert user.sent == [] and sql(db_path, "SELECT * FROM subscription_offers") == []
    offer(make_cog(FakeJellyfin(SEASON), {100: user}), session(8))     # caught up later
    assert len(user.sent) == 1


def test_a_show_that_has_ended_is_not_offered(db_path):
    user = FakeUser()
    offer(make_cog(FakeJellyfin(SEASON, status="Ended"), {100: user}), session(8))
    assert user.sent == []


def test_subscribers_and_unlinked_people_cost_no_jellyfin_calls(db_path):
    sql(db_path, "INSERT INTO subscriptions (discord_user_id, discord_username, show_name) "
                 "VALUES ('100', 'eddy', 'fallout')")                   # typed by hand, no id
    user, jf = FakeUser(), FakeJellyfin(SEASON)
    offer(make_cog(jf, {100: user}), session(8))
    offer(make_cog(jf, {100: user}), session(8, user="Somebody Else"))
    assert user.sent == [] and jf.calls == []


def test_films_are_ignored(db_path):
    jf = FakeJellyfin(SEASON)
    film = {"Id": "s1", "UserName": "Eddy", "NowPlayingItem": {"Id": "f", "Type": "Movie", "RunTimeTicks": 10},
            "PlayState": {"PositionTicks": 10}}
    offer(make_cog(jf, {100: FakeUser()}), film)
    assert jf.calls == []


def test_stopped_short_of_90_percent_asks_jellyfin(db_path):
    user = FakeUser()
    offer(make_cog(FakeJellyfin(SEASON, played=False), {100: user}), session(8, played=0.85))
    assert user.sent == []
    jf = FakeJellyfin(SEASON, played=True)                      # Jellyfin counted it as played
    offer(make_cog(jf, {100: user}), session(8, played=0.85))
    assert len(user.sent) == 1
    assert ("item", f"{FALLOUT}-2-8", "jf-eddy") in jf.calls


def test_a_server_that_ignores_the_sort_gets_asked_for_everything(db_path):
    shuffled = [episode(2), episode(9), episode(1)] + [episode(n, season=1) for n in range(1, 12)]
    user, jf = FakeUser(), FakeJellyfin(shuffled, sorted_=False)
    offer(make_cog(jf, {100: user}), session(8))                # E09 is newer, found on the full list
    assert user.sent == []
    assert [c for c in jf.calls if c[0] == "newest"] == [("newest", 10), ("newest", None)]


def test_closed_dms_get_a_mention_in_the_activity_channel(db_path):
    channel = FakeChannel()
    offer(make_cog(FakeJellyfin(SEASON), {100: FakeUser(closed=True)}, channel), session(8))
    assert len(channel.sent) == 1
    post = channel.sent[0]
    assert post["content"] == "<@100> you're caught up on **Fallout** (your DMs are closed)"
    assert [u.id for u in post["allowed_mentions"].users] == [100]
    assert post["view"].children[0].custom_id == f"subscribe:offer:{FALLOUT}:100"
    assert len(sql(db_path, "SELECT * FROM subscription_offers")) == 1


# --- the button ------------------------------------------------------------------

class FakeMessage:
    def __init__(self, embed):
        self.embeds = [embed]


class FakeResponse:
    def __init__(self):
        self.sent, self.edits = [], []

    async def send_message(self, content, ephemeral=False):
        self.sent.append((content, ephemeral))

    async def edit_message(self, embed=None, view=None):
        self.edits.append((embed, view))


def press(series_id, member_id, presser, message, jf=None):
    interaction = SimpleNamespace(user=SimpleNamespace(id=presser, name=f"user{presser}"), message=message,
                                  response=FakeResponse(), client=SimpleNamespace(jf=jf))

    async def go():
        item = discord.ui.Button(custom_id=f"subscribe:offer:{series_id}:{member_id}")
        match = re.fullmatch(catch_up.OfferButton.__discord_ui_compiled_template__, item.custom_id)
        button = await catch_up.OfferButton.from_custom_id(interaction, item, match)
        await button.callback(interaction)
    asyncio.run(go())
    return interaction.response


def test_pressing_subscribes_like_the_command_and_edits_the_offer(db_path):
    offer(make_cog(FakeJellyfin(SEASON), {100: FakeUser()}), session(8))
    message = FakeMessage(catch_up.offer_embed("Fallout"))
    response = press(FALLOUT, 100, 100, message)
    assert sql(db_path, "SELECT discord_user_id, discord_username, show_name, show_id FROM subscriptions") == \
        [("100", "user100", "Fallout", FALLOUT)]
    embed, view = response.edits[0]
    assert embed.title == "🔔 Subscribed to Fallout" and "I'll DM you when new episodes" in embed.description
    assert view.children[0].disabled and view.children[0].label == "Subscribed"

    again = press(FALLOUT, 100, 100, message)                   # harmless
    assert "already subscribed" in again.edits[0][0].description
    assert len(sql(db_path, "SELECT * FROM subscriptions")) == 1


def test_someone_else_pressing_it_in_the_channel_gets_their_own_answer(db_path):
    offer(make_cog(FakeJellyfin(SEASON), {100: FakeUser(closed=True)}, FakeChannel()), session(8))
    response = press(FALLOUT, 100, 200, FakeMessage(catch_up.offer_embed("Fallout")))
    assert response.edits == []
    assert response.sent[0][1] is True and "**Fallout**" in response.sent[0][0]
    assert sql(db_path, "SELECT discord_user_id FROM subscriptions") == [("200",)]


def test_the_button_falls_back_to_jellyfin_for_the_name(db_path):
    response = press(FALLOUT, 100, 100, FakeMessage(catch_up.offer_embed("Fallout")), jf=FakeJellyfin(SEASON))
    assert response.edits and sql(db_path, "SELECT show_name FROM subscriptions") == [("Fallout",)]
    gone = press("deadbeef", 100, 100, FakeMessage(catch_up.offer_embed("X")), jf=None)
    assert "isn't in Jellyfin" in gone.sent[0][0]


def test_shared_subscribe_helper(db_path):
    async def go():
        async with aiosqlite.connect(db_path) as db:
            first = await new_episodes.subscribe(db, "100", "eddy", "Fallout", FALLOUT)
            second = await new_episodes.subscribe(db, "100", "eddy", "fallout", "re-added-id")   # by name
            return first, second, await new_episodes.subscribed(db, "100", FALLOUT, None)
    assert asyncio.run(go()) == (True, False, True)


# --- where plays end -----------------------------------------------------------------

class Recorder:
    def __init__(self, fail=False):
        self.seen, self.fail = [], fail

    async def offer_if_caught_up(self, last_seen):
        self.seen.append(last_seen)
        if self.fail:
            raise RuntimeError("Jellyfin fell over")


def activity(recorder):
    return Activity(SimpleNamespace(jf=None, get_cog=lambda name: recorder if name == "Subscriptions" else None))


def test_the_feed_hands_over_each_play_as_last_seen(db_path, monkeypatch):
    async def nothing(*args, **kwargs):
        return None
    monkeypatch.setattr(watch_data, "record_watch_end", nothing)
    recorder = Recorder()
    cog = activity(recorder)

    async def go():
        await cog.record(session(7, played=0.97))
        await cog.record(session(8, played=0.1))          # autoplay moved on: E07 ended
        await cog.record(session(8, played=0.95))
        stopped = {"Id": "s1", "UserName": "Eddy"}
        await cog.record(stopped)                         # stopped: E08 ended
    asyncio.run(go())
    assert [(s["NowPlayingItem"]["IndexNumber"], s["PlayState"]["PositionTicks"]) for s in recorder.seen] == \
        [(7, int(RUNTIME * 0.97)), (8, int(RUNTIME * 0.95))]


def test_a_failing_offer_never_stops_the_poll(db_path):
    asyncio.run(activity(Recorder(fail=True)).play_ended(session(8)))
    asyncio.run(Activity(SimpleNamespace(jf=None)).play_ended(session(8)))      # no subscriptions cog
