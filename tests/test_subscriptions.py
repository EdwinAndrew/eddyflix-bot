"""New-episode DMs: finding new episodes, batching them per show, and who gets told."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import aiosqlite
import discord
import pytest

import config
import new_episodes as ne
import watch_data
from cogs import subscriptions
from cogs.subscriptions import Subscriptions, best_series, notification_embed

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
FALLOUT = "9f70f09353d6bba2d7d5652f45cca41d"


def stamp(minutes_ago: float) -> str:
    """A DateCreated the way Jellyfin writes it, seven decimal places and a Z."""
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "0Z"


def ep(number, season=2, minutes_ago=30, show="Fallout", series_id=FALLOUT, item_id=None, **extra):
    return {"Id": item_id or f"{series_id}-{season}-{number}", "Name": f"Episode {number}", "Type": "Episode",
            "SeriesName": show, "SeriesId": series_id, "ParentIndexNumber": season, "IndexNumber": number,
            "DateCreated": stamp(minutes_ago), "LocationType": "FileSystem", **extra}


# --- dates, names and keys ---------------------------------------------------

def test_parse_date_reads_jellyfin_timestamps():
    assert ne.parse_date("2026-11-12T08:04:59.3266898Z") == datetime(2026, 11, 12, 8, 4, 59, 326689, tzinfo=timezone.utc)
    assert ne.parse_date("2025-12-11T21:33:55.0000000Z").tzinfo is not None
    assert ne.parse_date(None) is None
    assert ne.parse_date("not a date") is None


def test_normalize_matches_hand_typed_names():
    assert ne.normalize("the rookie") == ne.normalize("The Rookie")
    assert ne.normalize("yellowstone") == ne.normalize("Yellowstone (2018)")
    assert ne.normalize("Euphoria") == ne.normalize("Euphoria (US)")
    assert ne.normalize("shogun") == ne.normalize("Shōgun")
    assert ne.normalize("Your Friends & Neighbors") == "your friends neighbors"


def test_episode_key_ignores_the_file():
    # A Sonarr upgrade or a rebuilt library gives a new item id and a new date.
    assert ne.episode_key(ep(1)) == ne.episode_key(ep(1, item_id="new-file", minutes_ago=1))
    assert ne.episode_key(ep(1)) != ne.episode_key(ep(2))
    assert ne.episode_key(ep(1)) == ne.episode_key(ep(1, series_id="re-added"))


# --- picking what is new -----------------------------------------------------

def test_a_season_is_one_batch_per_show():
    episodes = [ep(n) for n in (3, 1, 2)] + [ep(5, season=9, show="The Simpsons", series_id="simpsons")]
    ready = ne.pick_new(episodes, set(), NOW)
    assert [[e["IndexNumber"] for e in group] for group in ready] == [[1, 2, 3], [5]]
    assert [g[0]["SeriesName"] for g in ready] == ["Fallout", "The Simpsons"]


def test_known_episodes_are_skipped():
    known = {ne.episode_key(ep(1))}
    ready = ne.pick_new([ep(1, item_id="upgraded"), ep(2)], known, NOW)
    assert [[e["IndexNumber"] for e in g] for g in ready] == [[2]]


def test_two_files_of_one_episode_count_once():
    ready = ne.pick_new([ep(1), ep(1, item_id="4k-version")], set(), NOW)
    assert len(ready[0]) == 1


def test_missing_episodes_are_ignored():
    assert ne.pick_new([ep(1, LocationType="Virtual")], set(), NOW) == []


def test_a_show_still_importing_is_held():
    # Newest episode 2 minutes old: Sonarr may still be importing the rest.
    assert ne.pick_new([ep(1, minutes_ago=8), ep(2, minutes_ago=2)], set(), NOW) == []
    # Ten quiet minutes later it goes out as one.
    later = NOW + timedelta(minutes=10)
    assert len(ne.pick_new([ep(1, minutes_ago=8), ep(2, minutes_ago=2)], set(), later)[0]) == 2


def test_a_slow_trickle_is_not_held_forever():
    trickle = [ep(n, minutes_ago=70 - n * 8) for n in range(1, 9)]   # one every 8 minutes for an hour
    assert len(ne.pick_new(trickle, set(), NOW)[0]) == 8


def test_holding_one_show_does_not_hold_another():
    episodes = [ep(1, minutes_ago=2), ep(1, season=9, show="The Simpsons", series_id="simpsons", minutes_ago=30)]
    assert [g[0]["SeriesName"] for g in ne.pick_new(episodes, set(), NOW)] == ["The Simpsons"]


def test_the_mark_only_moves_forward():
    mark = NOW - timedelta(minutes=5)
    assert ne.newest_date([ep(1, minutes_ago=60)], mark) == mark
    assert ne.newest_date([ep(1, minutes_ago=1)], mark) == NOW - timedelta(minutes=1)
    assert ne.newest_date([], None) is None


# --- formatting --------------------------------------------------------------

@pytest.mark.parametrize("episodes, expected", [
    ([ep(5)], "S02E05"),
    ([ep(n) for n in range(1, 7)], "S02E01–E06"),
    ([ep(1), ep(2), ep(3), ep(5)], "S02E01–E03, E05"),
    ([ep(10, season=1), ep(1), ep(2)], "S01E10, S02E01–E02"),
    ([ep(1, IndexNumberEnd=2), ep(3)], "S02E01–E03"),
    ([ep(0, season=0)], "S00E00"),
])
def test_episode_range(episodes, expected):
    assert ne.episode_range(episodes) == expected


def test_episode_code_for_a_two_part_file():
    assert ne.episode_code(ep(1, IndexNumberEnd=2)) == "S02E01–E02"
    assert ne.episode_code(ep(7)) == "S02E07"


def test_headline():
    assert ne.headline("Fallout", 1) == "A new episode of Fallout"
    assert ne.headline("Fallout", 6) == "6 new episodes of Fallout"


def test_embed_lists_ten_and_links_when_public_url_is_set(monkeypatch):
    monkeypatch.setattr(config, "JELLYFIN_PUBLIC_URL", "https://jf.example")
    embed = notification_embed([ep(n) for n in range(1, 14)])
    assert embed.title == "📺 13 new episodes of Fallout"
    assert embed.url == f"https://jf.example/web/#/details?id={FALLOUT}"
    assert "**S02E01–E13**" in embed.description
    assert f"[S02E01 · Episode 1](https://jf.example/web/#/details?id={FALLOUT}-2-1)" in embed.description
    assert "S02E11" not in embed.description and "…and 3 more" in embed.description


def test_embed_without_public_url_has_no_links(monkeypatch):
    monkeypatch.setattr(config, "JELLYFIN_PUBLIC_URL", "")
    embed = notification_embed([ep(1)])
    assert embed.url is None and "](" not in embed.description


# --- matching subscriptions --------------------------------------------------

def test_subscribers_match_by_id_or_name():
    rows = [(1, "100", "the rookie", None),        # typed by hand before the autocomplete
            (2, "200", "Fallout", FALLOUT),
            (3, "300", "Fallout", "old-id"),       # the show was added again since
            (4, "400", "The Simpsons", "simpsons")]
    assert [r[0] for r in ne.subscribers(rows, FALLOUT, "Fallout")] == [2, 3]
    assert [r[0] for r in ne.subscribers(rows, "rookie-id", "The Rookie")] == [1]
    assert ne.subscribers(rows, "x", "Arcane") == []


def test_best_series():
    found = [{"Name": "Yellowstone (2018)", "Id": "a"}, {"Name": "1923", "Id": "b"}]
    assert best_series("yellowstone (2018)", found)["Id"] == "a"
    assert best_series("Yellowstone", found)["Id"] == "a"
    assert best_series("1923", found)["Id"] == "b"
    assert best_series("yellow", found) is None                  # two hits, neither exact
    assert best_series("yellow", found[:1])["Id"] == "a"         # the only hit
    assert best_series("The Rookie", []) is None                 # not in the library


# --- the poll, against a throwaway database ----------------------------------

class FakeJellyfin:
    def __init__(self, episodes):
        self.episodes = episodes
        self.pages = 0

    async def episodes_by_date_added(self, start=0, limit=100):
        self.pages += 1
        ordered = sorted(self.episodes, key=lambda e: e["DateCreated"], reverse=True)
        return ordered[start:start + limit]

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
    return path


def sql(path, query, args=()):
    async def go():
        async with aiosqlite.connect(path) as db:
            async with db.execute(query, args) as cursor:
                rows = await cursor.fetchall()
            await db.commit()
            return rows
    return asyncio.run(go())


def make_cog(jf, users, channel):
    bot = SimpleNamespace(jf=jf, get_user=lambda uid: users.get(uid),
                          get_channel=lambda cid: channel if cid == 42 else None)

    async def fetch_user(uid):
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown User")
    bot.fetch_user = fetch_user
    return Subscriptions(bot)


def poll(cog, at):
    asyncio.run(cog.poll(now=at))


def test_first_run_notes_the_library_and_announces_nothing(db_path):
    sql(db_path, "INSERT INTO subscriptions (discord_user_id, discord_username, show_name) VALUES ('100', 'eddy', 'Fallout')")
    user, channel = FakeUser(), FakeChannel()
    library = [ep(n, minutes_ago=600) for n in range(1, 8)]
    poll(make_cog(FakeJellyfin(library), {100: user}, channel), NOW)
    assert user.sent == [] and channel.sent == []
    assert len(sql(db_path, "SELECT * FROM known_episodes")) == 7
    assert sql(db_path, "SELECT value FROM bot_state") == [(ne.parse_date(stamp(600)).isoformat(),)]


def test_new_episodes_are_dmed_once_and_survive_a_restart(db_path):
    sql(db_path, "INSERT INTO subscriptions (discord_user_id, discord_username, show_name) VALUES ('100', 'eddy', 'fallout')")
    user, channel = FakeUser(), FakeChannel()
    library = [ep(n, season=1, minutes_ago=600) for n in range(1, 9)]
    jf = FakeJellyfin(library)
    poll(make_cog(jf, {100: user}, channel), NOW)

    # Sonarr imports season 2 over a few minutes; also an upgraded file from season 1.
    jf.episodes += [ep(n, minutes_ago=3 + n * 0.1) for n in range(1, 7)]
    jf.episodes.append(ep(4, season=1, item_id="upgraded", minutes_ago=3))
    poll(make_cog(jf, {100: user}, channel), NOW)            # still importing: held
    assert user.sent == []

    poll(make_cog(jf, {100: user}, channel), NOW + timedelta(minutes=10))
    assert len(user.sent) == 1
    embed = user.sent[0]["embed"]
    assert embed.title == "📺 6 new episodes of Fallout" and "S02E01–E06" in embed.description
    assert embed.thumbnail.url == "attachment://poster.jpg" and len(user.sent[0]["files"]) == 1
    # The hand-typed row now carries the show's id.
    assert sql(db_path, "SELECT show_id FROM subscriptions") == [(FALLOUT,)]

    # A restart (a new cog) and later polls do not tell anyone again.
    poll(make_cog(jf, {100: user}, channel), NOW + timedelta(minutes=15))
    poll(make_cog(jf, {100: user}, channel), NOW + timedelta(hours=5))
    assert len(user.sent) == 1 and channel.sent == []


def test_episodes_added_while_the_bot_was_down_are_not_missed(db_path):
    sql(db_path, "INSERT INTO subscriptions (discord_user_id, discord_username, show_name, show_id) "
                 "VALUES ('100', 'eddy', 'Fallout', ?)", (FALLOUT,))
    user = FakeUser()
    jf = FakeJellyfin([ep(1, season=1, minutes_ago=6000)])
    poll(make_cog(jf, {100: user}, FakeChannel()), NOW - timedelta(days=4))
    jf.episodes.append(ep(1, minutes_ago=60 * 24))           # a day before the bot came back
    poll(make_cog(jf, {100: user}, FakeChannel()), NOW)
    assert len(user.sent) == 1


def test_closed_dms_fall_back_to_a_mention_in_the_activity_channel(db_path):
    for uid in ("100", "200", "300"):
        sql(db_path, "INSERT INTO subscriptions (discord_user_id, discord_username, show_name, show_id) "
                     "VALUES (?, 'x', 'Fallout', ?)", (uid, FALLOUT))
    open_user, closed_user, channel = FakeUser(), FakeUser(closed=True), FakeChannel()
    jf = FakeJellyfin([ep(1, season=1, minutes_ago=6000)])
    users = {100: open_user, 200: closed_user}               # 300 cannot be found at all
    poll(make_cog(jf, users, channel), NOW)
    jf.episodes.append(ep(1, minutes_ago=30))
    poll(make_cog(jf, users, channel), NOW)

    assert len(open_user.sent) == 1
    assert len(channel.sent) == 1
    post = channel.sent[0]
    assert post["content"].startswith("<@200> <@300>")
    assert {u.id for u in post["allowed_mentions"].users} == {200, 300}
    assert len(post["files"]) == 1


def test_shows_nobody_subscribed_to_are_noted_quietly(db_path):
    user, channel = FakeUser(), FakeChannel()
    jf = FakeJellyfin([ep(1, season=1, minutes_ago=6000)])
    poll(make_cog(jf, {}, channel), NOW)
    jf.episodes.append(ep(1, show="Arcane", series_id="arcane", minutes_ago=30))
    poll(make_cog(jf, {}, channel), NOW)
    assert channel.sent == []
    assert ("arcane:2:1",) in sql(db_path, "SELECT episode_key FROM known_episodes")


def test_polling_stops_paging_at_the_lookback(db_path, monkeypatch):
    monkeypatch.setattr(subscriptions, "PAGE", 10)
    jf = FakeJellyfin([ep(n, season=1, minutes_ago=6000 + n * 60) for n in range(1, 200)])
    cog = make_cog(jf, {}, FakeChannel())
    poll(cog, NOW)                                            # first run reads the lot
    jf.pages = 0
    jf.episodes += [ep(n, minutes_ago=30) for n in range(1, 4)]
    poll(cog, NOW)
    assert jf.pages == 1
