"""Seerr API calls, for /request.

Seerr (the Overseerr/Jellyseerr successor) sits next to Jellyfin and takes the
requests, hands them to Radarr and Sonarr, and notices when they land.
"""
import asyncio
from urllib.parse import quote

import aiohttp
from yarl import URL

TMDB_IMAGES = "https://image.tmdb.org/t/p"


class SeerrError(Exception):
    """Seerr said no. status is its HTTP status, message what it said."""

    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status, self.message = status, message


class SeerrClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip('/')
        # Sent per call, not on the session: the session also fetches posters
        # from TMDB, which must not see the key.
        self.headers = {"X-Api-Key": api_key, "Accept": "application/json"}
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            async with self._session_lock:
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _call(self, method: str, endpoint: str, body: dict | None = None) -> tuple[int, dict]:
        """(status, JSON answer). endpoint is already percent-encoded."""
        session = await self._get_session()
        url = URL(f"{self.base_url}/api/v1{endpoint}", encoded=True)
        async with session.request(method, url, headers=self.headers, json=body) as resp:
            try:
                data = await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError):
                data = None
            return resp.status, data if isinstance(data, dict) else {}

    async def _get(self, endpoint: str) -> dict:
        status, data = await self._call("GET", endpoint)
        if status != 200:
            raise SeerrError(status, data.get("message") or "request failed")
        return data

    async def search(self, query: str) -> list[dict]:
        """Films and shows matching query, as TMDB ranks them. People are left out."""
        if not query.strip():
            return []
        # Seerr rejects some characters unless they are percent-encoded: "it's", "what?"
        data = await self._get(f"/search?query={quote(query.strip(), safe='')}&page=1")
        return [r for r in data.get("results", []) if r.get("mediaType") in ("movie", "tv")]

    async def details(self, media_type: str, tmdb_id: int) -> dict:
        """A film or show with what Seerr knows: mediaInfo (status, seasons,
        requests, jellyfinMediaId), and for shows the seasons TMDB lists."""
        return await self._get(f"/{'tv' if media_type == 'tv' else 'movie'}/{int(tmdb_id)}")

    async def users(self) -> list[dict]:
        return (await self._get("/user?take=500")).get("results", [])

    async def get_request(self, request_id: int) -> dict | None:
        """One request, or None when it no longer exists."""
        status, data = await self._call("GET", f"/request/{int(request_id)}")
        if status == 404:
            return None
        if status != 200:
            raise SeerrError(status, data.get("message") or "request failed")
        return data

    async def create_request(self, media_type: str, tmdb_id: int, seasons: list[int] | None = None,
                             user_id: int | None = None) -> dict:
        """Ask for a film, or some seasons of a show. With user_id the request is
        that Seerr user's (their quota, their auto-approve), which the API key's
        admin is allowed to do. Raises SeerrError when Seerr refuses."""
        body: dict = {"mediaType": media_type, "mediaId": int(tmdb_id)}
        if media_type == "tv":
            body["seasons"] = list(seasons or [])
        if user_id:
            body["userId"] = int(user_id)
        status, data = await self._call("POST", "/request", body)
        # 202 is Seerr's "no seasons left to request", not a success
        if status != 201:
            raise SeerrError(status, data.get("message") or "request failed")
        return data

    async def poster(self, poster_path: str | None, width: str = "w500") -> bytes | None:
        """A poster from TMDB as bytes, for uploading to Discord. None if there is none."""
        if not poster_path:
            return None
        session = await self._get_session()
        try:
            async with session.get(f"{TMDB_IMAGES}/{width}{poster_path}") as resp:
                return await resp.read() if resp.status == 200 else None
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None
