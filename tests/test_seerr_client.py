"""The Seerr client against a fake Seerr on localhost: what a request sends, how
refusals come back, and that the API key never goes to TMDB."""
import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import seerr_client
from seerr_client import SeerrClient, SeerrError


def with_fake_seerr(test, answers=None):
    """Run test(client, seen) against a local server that records every call."""
    seen = []
    answers = answers or {}

    async def handler(request: web.Request):
        body = await request.json() if request.can_read_body else None
        seen.append({"method": request.method, "path": request.path, "query": request.query_string,
                     "raw": request.raw_path, "key": request.headers.get("X-Api-Key"), "body": body})
        status, data = answers.get((request.method, request.path), (200, {"results": []}))
        if request.path.startswith("/t/p/"):
            return web.Response(body=b"JPEG", content_type="image/jpeg")
        return web.json_response(data, status=status)

    async def go():
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        server = TestServer(app)
        await server.start_server()
        client = SeerrClient(str(server.make_url("")), "secret-key")
        try:
            return await test(client, seen, server)
        finally:
            await client.close()
            await server.close()

    return asyncio.run(go())


def test_request_as_a_user_sends_their_id_and_the_seasons():
    async def test(client, seen, server):
        made = await client.create_request("tv", 106379, [2, 3], user_id=4)
        assert made == {"id": 31, "status": 1}
        call = seen[0]
        assert (call["method"], call["path"], call["key"]) == ("POST", "/api/v1/request", "secret-key")
        assert call["body"] == {"mediaType": "tv", "mediaId": 106379, "seasons": [2, 3], "userId": 4}
    with_fake_seerr(test, {("POST", "/api/v1/request"): (201, {"id": 31, "status": 1})})


def test_request_without_a_user_is_the_key_users():
    async def test(client, seen, server):
        await client.create_request("movie", 353081)
        assert seen[0]["body"] == {"mediaType": "movie", "mediaId": 353081}
    with_fake_seerr(test, {("POST", "/api/v1/request"): (201, {"id": 32, "status": 2})})


@pytest.mark.parametrize("status, message", [
    (409, "Request for this media already exists."),
    (403, "Movie Quota exceeded."),
    (202, "No seasons available to request"),    # Seerr's "nothing left", not a success
])
def test_refusals_raise(status, message):
    async def test(client, seen, server):
        with pytest.raises(SeerrError) as e:
            await client.create_request("movie", 1)
        assert (e.value.status, e.value.message) == (status, message)
    with_fake_seerr(test, {("POST", "/api/v1/request"): (status, {"message": message})})


def test_search_encodes_the_query_and_drops_people():
    async def test(client, seen, server):
        results = await client.search("it's a wonderful life?")
        assert [r["id"] for r in results] == [1, 2]
        assert seen[0]["raw"] == "/api/v1/search?query=it%27s%20a%20wonderful%20life%3F&page=1"
        assert await client.search("  ") == []
        assert len(seen) == 1
    with_fake_seerr(test, {("GET", "/api/v1/search"): (200, {"results": [
        {"id": 1, "mediaType": "movie"}, {"id": 9, "mediaType": "person"}, {"id": 2, "mediaType": "tv"}]})})


def test_missing_request_is_none():
    async def test(client, seen, server):
        assert await client.get_request(99) is None
        assert (await client.get_request(7))["id"] == 7
    with_fake_seerr(test, {("GET", "/api/v1/request/99"): (404, {"message": "Request not found."}),
                           ("GET", "/api/v1/request/7"): (200, {"id": 7})})


def test_poster_comes_from_tmdb_without_the_key(monkeypatch):
    async def test(client, seen, server):
        monkeypatch.setattr(seerr_client, "TMDB_IMAGES", str(server.make_url("/t/p")))
        assert await client.poster("/abc.jpg") == b"JPEG"
        assert seen[0]["path"] == "/t/p/w500/abc.jpg" and seen[0]["key"] is None
        assert await client.poster(None) is None
    with_fake_seerr(test)
