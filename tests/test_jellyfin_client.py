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
