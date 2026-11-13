"""/pick and /continue: the filters, the draw, and the lines /continue lists."""
import asyncio
import random

import config
from cogs.watch import (continue_line, draw, genres_matching, in_progress, last_finished, match_genre,
                        pick_types, pool_footer, short_enough)
from jellyfin_client import JellyfinClient

MIN = 60 * 10_000_000      # a minute in Jellyfin ticks


def requested(call):
    client = JellyfinClient("http://jellyfin:8096", "key")
    seen = []

    async def fake_get(endpoint):
        seen.append(endpoint)
        return {"Items": []}

    client._get = fake_get
    asyncio.run(call(client))
    return seen[0]


# --- /pick ---------------------------------------------------------------------

def test_type_option():
    assert pick_types("film") == "Movie"
    assert pick_types("show") == "Series"
    assert pick_types(None) == "Movie,Series"


def test_pool_query_asks_for_films_not_collections():
    endpoint = requested(lambda c: c.pick_pool("Movie,Series"))
    assert "IncludeItemTypes=Movie,Series" in endpoint and "Recursive=true" in endpoint
    assert "CollapseBoxSetItems=false" in endpoint
    assert "Genres" not in endpoint and "userId" not in endpoint


def test_pool_query_genre_is_escaped():
    endpoint = requested(lambda c: c.pick_pool("Series", genre="Action & Adventure"))
    assert "IncludeItemTypes=Series" in endpoint
    assert "Genres=Action%20%26%20Adventure" in endpoint


def test_unwatched_only_with_a_user():
    # IsPlayed without a userId gets a 400 from Jellyfin
    assert "IsPlayed" not in requested(lambda c: c.pick_pool("Movie", unwatched=True))
    endpoint = requested(lambda c: c.pick_pool("Movie", user_id="u1", unwatched=True))
    assert "userId=u1" in endpoint and "IsPlayed=false" in endpoint
    assert "IsPlayed" not in requested(lambda c: c.pick_pool("Movie", user_id="u1"))


def test_genres_query():
    endpoint = requested(lambda c: c.get_genres("Movie"))
    assert endpoint.startswith("/Genres?") and "IncludeItemTypes=Movie" in endpoint


def test_length_filter():
    film = {"RunTimeTicks": 98 * MIN}
    assert short_enough(film, None)
    assert short_enough(film, 120)
    assert not short_enough(film, 90)
    assert short_enough({"RunTimeTicks": 90 * MIN}, 90)
    # Many shows have no runtime in Jellyfin: unknown is not short enough
    assert not short_enough({"RunTimeTicks": 0}, 30)
    assert not short_enough({}, 30)
    assert short_enough({}, None)


def test_genre_spelling_and_autocomplete():
    genres = ["Action", "Action & Adventure", "Comedy", "Sci-Fi & Fantasy", "Science Fiction"]
    assert match_genre(" sci-fi & fantasy ", genres) == "Sci-Fi & Fantasy"
    assert match_genre("Nope", genres) == "Nope"
    assert genres_matching(genres, "sci") == ["Sci-Fi & Fantasy", "Science Fiction"]
    assert genres_matching(genres, "") == genres
    assert len(genres_matching([f"g{n}" for n in range(40)], "")) == 25


def test_footer():
    assert pool_footer(77, "Movie", True, True) == "Picked from 77 unwatched films"
    assert pool_footer(1, "Series", False, True) == "Picked from 1 show"
    assert pool_footer(138, "Movie,Series", False, False) == \
        "Picked from 138 films and shows. Use /link to see where you left off."


def test_rerolls_do_not_repeat_until_the_pool_runs_out():
    random.seed(1)
    pool = [{"Id": str(n)} for n in range(5)]
    queue, current, seen = [], None, []
    for _ in range(5):
        current = draw(queue, pool, current and current["Id"])
        seen.append(current["Id"])
    assert sorted(seen) == ["0", "1", "2", "3", "4"]
    # Starting over never shows the same one twice in a row
    for seed in range(50):
        random.seed(seed)
        queue = []
        assert draw(queue, pool, "3")["Id"] != "3"


def test_a_pool_of_one_rerolls_to_itself():
    pool = [{"Id": "only"}]
    queue = []
    assert draw(queue, pool, None)["Id"] == "only"
    assert draw(queue, pool, "only")["Id"] == "only"


# --- /continue -----------------------------------------------------------------

def film(id, played, position=10 * MIN, **extra):
    return {"Type": "Movie", "Id": id, "Name": id, "RunTimeTicks": 98 * MIN,
            "UserData": {"PlaybackPositionTicks": position, "LastPlayedDate": played, "Played": False}, **extra}


def episode(id, show, played=None, position=0, done=False, season=1, number=1):
    data = {"PlaybackPositionTicks": position, "Played": done}
    if played:
        data["LastPlayedDate"] = played
    return {"Type": "Episode", "Id": id, "SeriesId": show, "SeriesName": show.title(),
            "ParentIndexNumber": season, "IndexNumber": number, "Name": f"Episode {number}",
            "RunTimeTicks": 22 * MIN, "UserData": data}


def ids(items):
    return [i["Id"] for i in items]


def test_a_show_is_listed_once_newest_first():
    resume = [episode("yel2", "yellowstone", "2026-11-13T00:09:26.4272191Z", 9 * MIN, number=2),
              film("rush", "2026-11-10T23:53:29.85Z"),
              episode("obx1", "outer banks", "2026-06-02T02:19:02Z", 5 * MIN)]
    # Next-up repeats the half-watched episodes, and adds shows with nothing half-watched
    next_up = [episode("simp", "simpsons", number=10),
               episode("yel2", "yellowstone", "2026-11-13T00:09:26.4272191Z", 9 * MIN, number=2),
               episode("obx1", "outer banks", "2026-06-02T02:19:02Z", 5 * MIN),
               episode("gbd1", "grand blue")]
    finished = last_finished([episode("simp9", "simpsons", "2026-11-11T00:43:10.9583762Z", done=True),
                              episode("simp8", "simpsons", "2026-11-11T00:20:46Z", done=True),
                              episode("yel1", "yellowstone", "2026-11-12T23:00:00Z", done=True)])
    assert ids(in_progress(resume, next_up, finished)) == ["yel2", "simp", "rush", "obx1", "gbd1"]


def test_an_old_half_watched_episode_gives_way_to_next_up():
    # Stopped part-way through E03 in October, then watched E04 and E05 to the end
    resume = [episode("e3", "show", "2026-10-01T20:00:00Z", 5 * MIN, number=3)]
    next_up = [episode("e6", "show", number=6)]
    finished = last_finished([episode("e5", "show", "2026-10-03T20:00:00Z", done=True),
                              episode("e4", "show", "2026-10-02T20:00:00Z", done=True)])
    assert ids(in_progress(resume, next_up, finished)) == ["e6"]
    # Without the later ones, the half-watched episode wins
    assert ids(in_progress(resume, next_up, {})) == ["e3"]


def test_finished_episodes_seasons_and_shows_are_skipped():
    resume = [episode("done", "show", "2026-10-22T13:08:13Z", 17 * MIN, done=True),
              {"Type": "Season", "Id": "season", "SeriesId": "show"},
              {"Type": "Series", "Id": "show"}]
    assert in_progress(resume, [], {}) == []


def test_at_most_eight():
    resume = [film(f"f{n}", f"2026-11-{n + 1:02d}T00:00:00Z") for n in range(12)]
    listed = in_progress(resume, [], {})
    assert len(listed) == 8 and listed[0]["Id"] == "f11"


def test_continue_lines(monkeypatch):
    monkeypatch.setattr(config, "JELLYFIN_PUBLIC_URL", "")
    rush = film("rush", "2026-11-10T23:53:29Z", position=6 * MIN + 20 * 10_000_000,
                RunTimeTicks=97 * MIN + 49 * 10_000_000, Name="Rush Hour")
    assert continue_line(rush) == "▶️ **Rush Hour** · stopped at 6:20 of 1:37:49"
    simpsons = {**episode("simp", "simpsons", season=2, number=10), "SeriesName": "The Simpsons",
                "Name": "Bart Gets Hit by a Car"}
    assert continue_line(simpsons) == "⏭️ **The Simpsons** · S02E10 · Bart Gets Hit by a Car"
    half = episode("yel2", "yellowstone", "2026-11-13T00:09:26Z", 9 * MIN, number=2)
    assert continue_line(half) == "▶️ **Yellowstone** · S01E02 · Episode 2 · stopped at 9:00 of 22:00"


def test_continue_lines_link_to_jellyfin(monkeypatch):
    monkeypatch.setattr(config, "JELLYFIN_PUBLIC_URL", "https://jf.example")
    line = continue_line(episode("e" * 32, "simpsons", season=2, number=10))
    assert line.startswith(f"⏭️ [Simpsons](https://jf.example/web/#/details?id={'e' * 32}) · S02E10")
