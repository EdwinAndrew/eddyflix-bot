"""The film queries ask for films, not collections."""
import asyncio

from jellyfin_client import JellyfinClient


def requested(call):
    client = JellyfinClient("http://jellyfin:8096", "key")
    seen = []

    async def fake_get(endpoint):
        seen.append(endpoint)
        return {"Items": []}

    client._get = fake_get
    asyncio.run(call(client))
    return seen[0]


def test_search_lists_films_not_collections():
    for term in ("", "spider"):
        endpoint = requested(lambda c: c.search_movies(term))
        assert "IncludeItemTypes=Movie" in endpoint
        assert "CollapseBoxSetItems=false" in endpoint


def test_random_picks_films_not_collections():
    endpoint = requested(lambda c: c.random_movies(4))
    assert "SortBy=Random" in endpoint
    assert "CollapseBoxSetItems=false" in endpoint


def test_episode_count_is_per_series():
    # SeriesId is ignored by Jellyfin 12 and counted the whole library.
    endpoint = requested(lambda c: c.get_series_episode_count("abc"))
    assert "ParentId=abc" in endpoint and "SeriesId" not in endpoint


def test_title_search_lists_films_and_shows_not_collections():
    endpoint = requested(lambda c: c.search_titles("bear"))
    assert "IncludeItemTypes=Movie,Series" in endpoint
    assert "CollapseBoxSetItems=false" in endpoint
    assert "SearchTerm=bear" in endpoint


def test_next_up_is_per_series_and_includes_half_watched():
    endpoint = requested(lambda c: c.next_up("u1", "abc"))
    assert endpoint.startswith("/Shows/NextUp?")
    assert "userId=u1" in endpoint and "seriesId=abc" in endpoint
    assert "enableResumable=true" in endpoint


def test_item_carries_user_progress_only_when_asked():
    assert "userId" not in requested(lambda c: c.get_item("abc"))
    assert "userId=u1" in requested(lambda c: c.get_item("abc", "u1"))
