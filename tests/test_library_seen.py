"""Telling upgraded files from new films and episodes: identities, the library
record (first run, later runs, run again), the known_episodes backfill, and the
weekly post and the new-episode DMs end to end against a fake Jellyfin."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import aiosqlite
import pytest

import config
import library_seen as ls
import new_episodes as ne
import new_this_week as nw
import watch_data

NOW = datetime(2026, 11, 27, 18, 1, tzinfo=timezone.utc)
CUT = NOW - timedelta(days=7)


def stamp(days_ago: float) -> str:
    """A DateCreated the way Jellyfin writes it, seven decimal places and a Z."""
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "0Z"


def show(sid, name, tvdb=None, days_ago=400, **extra):
    ids = {"Tvdb": tvdb} if tvdb else {}
    return {"Id": sid, "Name": name, "Type": "Series", "ProviderIds": ids, "DateCreated": stamp(days_ago), **extra}


def ep(sid, name, season, number, days_ago, file="a", **extra):
    return {"Id": f"{sid}-{season}-{number}-{file}", "Type": "Episode", "SeriesId": sid, "SeriesName": name,
            "ParentIndexNumber": season, "IndexNumber": number, "DateCreated": stamp(days_ago),
            "LocationType": "FileSystem", **extra}


def film(name, days_ago, tmdb=None, year=2024, file="a"):
    return {"Id": f"film-{name}-{file}", "Type": "Movie", "Name": name, "ProductionYear": year,
            "ProviderIds": {"Tmdb": tmdb, "Imdb": None} if tmdb else {}, "DateCreated": stamp(days_ago),
            "LocationType": "FileSystem"}


class FakeJellyfin:
    """Newest-first listings that ignore date filters, as Jellyfin 12's do."""
    def __init__(self, series=(), episodes=(), films=()):
        self.series, self.episodes, self.films = list(series), list(episodes), list(films)
        self.requests = 0

    def _page(self, items, start, limit):
        self.requests += 1
        return sorted(items, key=lambda i: i["DateCreated"], reverse=True)[start:start + limit]

    async def added_by_date(self, item_type, start=0, limit=100):
        return self._page(self.films if item_type == "Movie" else self.series, start, limit)

    async def episodes_by_date_added(self, start=0, limit=100):
        return self._page(self.episodes, start, limit)

    async def provider_ids(self, ids):
        self.requests += 1
        return [s for s in self.series if s["Id"] in ids]

    async def get_image(self, item_id, image_type="Primary", max_width=1000):
        return None

    def replace(self, old: dict, new: dict):
        """What Sonarr and Radarr do: the old file goes, a better one comes."""
        items = self.films if old.get("Type") == "Movie" else self.episodes
        items.remove(old)
        items.append(new)


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "bot.db")
    monkeypatch.setattr(config, "DB_PATH", path)
    asyncio.run(watch_data.init_database())
    return path


def run(coro_fn):
    async def go():
        async with aiosqlite.connect(config.DB_PATH) as db:
            result = await coro_fn(db)
            await db.commit()
            return result
    return asyncio.run(go())


def sync(jf, now=NOW):
    return run(lambda db: ls.sync(db, jf, now))


def upgrades(jf, items, since=CUT):
    return run(lambda db: ls.upgrades(db, jf, items, since))


def rows():
    return run(lambda db: _all(db))


async def _all(db):
    async with db.execute("SELECT identity, kind, first_seen FROM library_seen ORDER BY identity") as cursor:
        return await cursor.fetchall()


PITT = show("pitt", "The Pitt", tvdb="448176")
INVINCIBLE = show("inv", "Invincible", tvdb="368207", days_ago=3)
BEAR = show("bear", "The Bear", tvdb="412429")


# --- identities --------------------------------------------------------------------

def test_episode_identities_survive_a_new_file_and_cover_two_part_files():
    shows = {"pitt": ls.series_keys(PITT)}
    a = ls.episode_identities(ep("pitt", "The Pitt", 1, 5, 100), shows)
    b = ls.episode_identities(ep("pitt", "The Pitt", 1, 5, 1, file="bluray"), shows)
    assert a == b == ["episode:tvdb:448176:1:5", "episode:name:the pitt:1:5"]
    two_part = ls.episode_identities(ep("pitt", "The Pitt", 1, 7, 1, IndexNumberEnd=8), shows)
    assert two_part == ["episode:tvdb:448176:1:7", "episode:tvdb:448176:1:8",
                        "episode:name:the pitt:1:7", "episode:name:the pitt:1:8"]
    # A show not looked up is known by name; an episode without a number by nothing.
    assert ls.episode_identities(ep("x", "Fallout", 2, 1, 1), {}) == ["episode:name:fallout:2:1"]
    assert ls.episode_identities(ep("x", "Fallout", 2, None, 1), {}) == []


def test_movie_identities_and_names():
    assert ls.movie_identities(film("Dune", 1, tmdb="438631", year=2021)) == \
        ["movie:tmdb:438631", "movie:name:dune:2021"]
    assert ls.movie_identities(film("Dune", 1, year=1984)) == ["movie:name:dune:1984"]
    # Unlike normalize(), a "(US)" stays: two shows, not one.
    assert ls.name_key("Ghosts (US)") != ls.name_key("Ghosts")
    assert ls.name_key("Shōgun") == ls.name_key("shogun")


# --- first run, later runs, run again ------------------------------------------------

def test_first_run_notes_the_whole_library_in_big_pages(db_path, monkeypatch):
    monkeypatch.setattr(ls, "SEED_PAGE", 10)
    episodes = [ep("pitt", "The Pitt", 1, n, 200 + n) for n in range(1, 26)]
    jf = FakeJellyfin([PITT], episodes, [film("Dune", 300, tmdb="438631")])
    assert sync(jf) == 26
    identities = rows()
    assert len(identities) == 25 * 2 + 2
    assert ("episode:tvdb:448176:1:1", "episode", ls.stamp(ne.parse_date(stamp(201)))) in identities
    assert run(ls.seeded)
    assert jf.requests == 3 + 1 + 1          # episodes in 3 pages, films, every show at once


def test_running_again_changes_nothing(db_path):
    jf = FakeJellyfin([PITT], [ep("pitt", "The Pitt", 1, n, 200) for n in range(1, 4)], [film("Dune", 300)])
    sync(jf)
    before = rows()
    assert sync(jf) == 0 and sync(jf, NOW + timedelta(hours=1)) == 0
    assert rows() == before
    asyncio.run(watch_data.init_database())   # the migration, again, on the live database
    assert rows() == before


def test_later_runs_only_read_what_is_new(db_path, monkeypatch):
    monkeypatch.setattr(ls, "PAGE", 5)
    jf = FakeJellyfin([BEAR], [ep("bear", "The Bear", 1, n, 300 - n) for n in range(1, 60)])
    sync(jf)
    jf.requests = 0
    jf.episodes.append(ep("bear", "The Bear", 4, 1, 0.1))
    assert sync(jf) == 1
    assert jf.requests == 1 + 1 + 1          # one page of films, one of episodes, the show's ids


# --- new or upgraded -------------------------------------------------------------------

def test_an_upgraded_episode_is_not_new_and_a_new_one_is(db_path):
    old = ep("pitt", "The Pitt", 1, 1, 100)
    jf = FakeJellyfin([PITT], [old])
    sync(jf, NOW - timedelta(days=50))
    bluray = ep("pitt", "The Pitt", 1, 1, 2, file="bluray")
    jf.replace(old, bluray)
    new = ep("pitt", "The Pitt", 1, 2, 1)
    jf.episodes.append(new)
    assert upgrades(jf, [bluray, new]) == {bluray["Id"]}


def test_an_upgraded_film_is_not_new_and_a_new_one_is(db_path):
    old, other = film("Dune", 100, tmdb="438631", year=2021), film("Sinners", 2, tmdb="1233413", year=2025)
    jf = FakeJellyfin(films=[old])
    sync(jf, NOW - timedelta(days=50))
    remux = film("Dune", 1, tmdb="438631", year=2021, file="remux")
    jf.replace(old, remux)
    jf.films.append(other)
    assert upgrades(jf, [remux, other]) == {remux["Id"]}


def test_new_this_week_then_upgraded_this_week_is_still_new(db_path):
    jf = FakeJellyfin([INVINCIBLE], [ep("x", "Old", 1, 1, 400)])
    sync(jf, NOW - timedelta(days=6))
    first = ep("inv", "Invincible", 1, 1, 5.5)
    jf.episodes.append(first)
    sync(jf, NOW - timedelta(days=5))
    better = ep("inv", "Invincible", 1, 1, 2, file="better")
    jf.replace(first, better)
    assert upgrades(jf, [better]) == set()


def test_a_show_that_gets_a_tvdb_id_later_still_matches(db_path):
    nameless = show("pitt", "The Pitt")
    jf = FakeJellyfin([nameless], [ep("pitt", "The Pitt", 1, 1, 100)])
    sync(jf, NOW - timedelta(days=50))
    jf.series = [PITT]                       # Jellyfin found the show's metadata
    bluray = ep("pitt", "The Pitt", 1, 1, 1, file="bluray")
    jf.episodes = [bluray]
    assert upgrades(jf, [bluray]) == {bluray["Id"]}
    # ...and the TVDB id is now known too, with the old date.
    assert ("episode:tvdb:448176:1:1", "episode", ls.stamp(ne.parse_date(stamp(100)))) in rows()


def test_a_film_first_seen_by_the_weekly_post_is_known_next_week(db_path):
    jf = FakeJellyfin()
    sync(jf, NOW - timedelta(days=14))
    dune = film("Dune", 3, tmdb="438631")
    assert upgrades(jf, [dune]) == set()
    remux = film("Dune", -6, tmdb="438631", file="remux")
    assert upgrades(jf, [remux], since=NOW) == {remux["Id"]}


def test_replacement_is_judged_by_the_file_for_the_dms():
    pitt = {"pitt": ls.series_keys(PITT)}
    first = ep("pitt", "The Pitt", 1, 1, 3)
    ids = ls.episode_identities(first, pitt)
    seen = {ids[0]: ne.parse_date(first["DateCreated"])}
    assert not ls.is_replacement(first, ids, seen)
    assert not ls.is_replacement(ep("pitt", "The Pitt", 1, 1, 3 - 0.01, file="b"), ids, seen)  # minutes later
    assert ls.is_replacement(ep("pitt", "The Pitt", 1, 1, 1, file="bluray"), ids, seen)
    assert not ls.is_replacement(first, ids, {})


# --- the first run, with known_episodes' history -----------------------------------------

def test_backfill_dates_upgraded_episodes_by_when_the_dms_first_noted_them():
    # known_episodes in arrival order: The Pitt's season long ago, other shows since,
    # then Invincible this week. The Pitt's files were all replaced this week.
    pitt = [ep("pitt", "The Pitt", 1, n, 1 + n * 0.01, file="bluray") for n in range(1, 16)]
    others = [ep("bear", "The Bear", 4, n, 150 - n) for n in range(1, 11)]
    invincible = [ep("inv", "Invincible", 1, n, 3) for n in range(1, 9)]
    order = pitt + others + invincible
    rowids = {ne.episode_key(e): n for n, e in enumerate(order, start=1)}
    earliest = ls.backfill(order, rowids)
    for e in pitt:
        assert earliest[ne.episode_key(e)] < CUT
    for e in invincible:
        assert earliest[ne.episode_key(e)] == ne.parse_date(e["DateCreated"])
    assert ls.backfill(order, {}) == {}


def test_first_run_uses_known_episodes_from_before_this_version_only(db_path):
    async def history(db):
        # The DM poll noted these over the last year, The Pitt's first ...
        keys = ([f"the pitt:1:{n}" for n in range(1, 16)] + [f"the bear:4:{n}" for n in range(1, 11)]
                + [f"invincible:1:{n}" for n in range(1, 33)])
        await db.executemany("INSERT INTO known_episodes (episode_key) VALUES (?)", [(k,) for k in keys])
        await db.execute("DELETE FROM bot_state WHERE key = ?", (ls.HISTORY_KEY,))
        await ls.create_tables(db)           # this version's first start
        # ... and one noted after it (say the DM poll got there first), of an old
        # file. Counted, it would make everything before it, Invincible too, old.
        await db.execute("INSERT INTO known_episodes (episode_key) VALUES ('the bear:3:1')")
    run(history)

    pitt = [ep("pitt", "The Pitt", 1, n, 2, file="bluray") for n in range(1, 16)]
    bear = [ep("bear", "The Bear", 4, n, 150 - n) for n in range(1, 11)] + [ep("bear", "The Bear", 3, 1, 300)]
    invincible = [ep("inv", "Invincible", 1, n, 3) for n in range(1, 33)]
    jf = FakeJellyfin([PITT, BEAR, INVINCIBLE], pitt + bear + invincible)
    sync(jf)
    assert upgrades(jf, pitt + invincible) == {e["Id"] for e in pitt}


def test_a_fresh_install_has_no_history(db_path):
    assert run(ls.history) == {}
    jf = FakeJellyfin([INVINCIBLE], [ep("inv", "Invincible", 1, 1, 3)])
    sync(jf)
    assert upgrades(jf, jf.episodes) == set()


# --- the weekly post, end to end ------------------------------------------------------------

def weekly_cog(jf):
    from cogs.weekly_new import WeeklyNew
    return WeeklyNew(SimpleNamespace(jf=jf))


def test_the_weekly_post_tells_this_weeks_mix_apart(db_path):
    pitt_old = [ep("pitt", "The Pitt", 1, n, 200) for n in range(1, 16)]
    bear_old = [ep("bear", "The Bear", 4, n, 150) for n in range(1, 8)]
    jf = FakeJellyfin([PITT, BEAR], pitt_old + bear_old)
    sync(jf, NOW - timedelta(days=30))

    for old in pitt_old:                     # 15 upgrades
        jf.replace(old, dict(old, Id=old["Id"] + "-bluray", DateCreated=stamp(2)))
    jf.series.append(INVINCIBLE)             # a new show, 32 episodes
    jf.episodes += [ep("inv", "Invincible", 1 + n // 8, 1 + n % 8, 3) for n in range(32)]
    jf.episodes.append(ep("bear", "The Bear", 4, 8, 1))
    jf.films.append(film("Sinners", 4, tmdb="1233413", year=2025))

    entries = asyncio.run(weekly_cog(jf).gather(NOW))
    assert [(e.kind, e.name, len(e.episodes), len(e.upgrades)) for e in entries] == [
        ("film", "Sinners", 0, 0),
        ("show", "Invincible", 32, 0),
        ("episodes", "The Bear", 1, 0),
        ("upgraded_show", "The Pitt", 0, 15),
    ]
    assert [nw.caption(e) for e in entries] == ["New film", "New show", "+1 episode", "+15 upgraded"]
    assert nw.counts(entries) == "1 film · 1 new show · new episodes of 1 show · upgrades for 1 show"


def test_a_week_of_upgrades_only_is_not_posted(db_path):
    old = ep("pitt", "The Pitt", 1, 1, 200)
    jf = FakeJellyfin([PITT], [old])
    sync(jf, NOW - timedelta(days=30))
    jf.replace(old, dict(old, Id="bluray", DateCreated=stamp(1)))
    assert asyncio.run(weekly_cog(jf).build(NOW)) is None


def test_the_weekly_post_still_goes_out_if_the_record_fails(db_path):
    class Broken(FakeJellyfin):
        async def provider_ids(self, ids):
            raise TimeoutError("Jellyfin did not answer")
    jf = Broken([PITT], [ep("pitt", "The Pitt", 1, 1, 1)])
    entries = asyncio.run(weekly_cog(jf).gather(NOW))
    assert [(e.kind, e.count) for e in entries] == [("episodes", 1)]
