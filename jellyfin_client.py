import asyncio
import time

import aiohttp
from typing import Dict, List
from urllib.parse import quote

# Without this, Jellyfin groups films into their collections when listing them:
# 39 films and 19 collections here instead of 111 films.
FILMS_NOT_COLLECTIONS = "CollapseBoxSetItems=false"


class JellyfinClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        # Jellyfin 12 only accepts the Authorization header. The legacy
        # X-MediaBrowser-Token / X-Emby-Token headers and ?api_key= get a 401.
        self.headers = {
            "Authorization": f'MediaBrowser Token="{api_key}"'
        }
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()
        # Monotonic time of the last authenticated call that succeeded, read by
        # the bot's heartbeat. Starts at construction so a fresh start gets the
        # same grace period as a running bot.
        self.last_auth_success = time.monotonic()

    async def _get_session(self) -> aiohttp.ClientSession:
        """Return a shared session, creating it on first use.

        Previously every call opened and tore down its own ClientSession,
        which meant a fresh TCP connection for each poll - the session loop
        alone runs every 30 seconds.
        """
        if self._session is None or self._session.closed:
            async with self._session_lock:
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession(
                        headers=self.headers,
                        timeout=aiohttp.ClientTimeout(total=30),
                    )
        return self._session

    async def close(self) -> None:
        """Close the shared session. Safe to call more than once."""
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def seconds_since_auth_success(self) -> float:
        return time.monotonic() - self.last_auth_success

    async def _get(self, endpoint: str) -> dict:
        """Make GET request to Jellyfin API"""
        url = f"{self.base_url}{endpoint}"
        session = await self._get_session()
        async with session.get(url) as resp:
            if resp.status != 200:
                raise Exception(f"API request failed: {resp.status}")
            data = await resp.json()
            # /System/Info/Public answers without a key, so it proves nothing
            # about authentication.
            if "/Public" not in endpoint:
                self.last_auth_success = time.monotonic()
            return data

    async def get_server_info(self) -> dict:
        """Get server information"""
        return await self._get("/System/Info/Public")

    async def get_sessions(self) -> List[dict]:
        """Get all active sessions"""
        return await self._get("/Sessions")

    async def get_library_stats(self) -> List[dict]:
        """Get library statistics"""
        try:
            result = await self._get("/Library/MediaFolders")
            items = result.get('Items', [])

            stats = []
            for lib in items:
                lib_id = lib.get('Id')
                lib_type = lib.get('CollectionType', 'unknown')

                # Query items in this library to get accurate count
                items_endpoint = f"/Items?ParentId={lib_id}&Recursive=true&Fields=BasicSyncInfo"
                items_result = await self._get(items_endpoint)
                item_count = items_result.get('TotalRecordCount', 0)

                stats.append({
                    'name': lib.get('Name', 'Unknown'),
                    'type': lib_type.capitalize(),
                    'count': item_count
                })

            return stats
        except Exception as e:
            print(f"Error getting library stats: {e}")
            return []

    async def get_recently_added(self, limit: int = 10) -> List[dict]:
        """Get recently added items"""
        try:
            endpoint = f"/Items/Latest?Limit={limit}&Fields=PrimaryImageAspectRatio,Overview,PremiereDate,SeriesName,ParentIndexNumber,IndexNumber&ImageTypeLimit=1"
            result = await self._get(endpoint)
            
            # /Items/Latest returns a list directly
            if isinstance(result, list):
                return result
            # Handle case where it might return as items in a dict
            elif isinstance(result, dict) and 'Items' in result:
                return result.get('Items', [])
            else:
                print(f"Unexpected response format for recently added: {type(result)}")
                return []
        except Exception as e:
            print(f"Error getting recently added items: {e}")
            return []

    async def get_system_info(self) -> dict:
        """Get detailed system information (requires admin)"""
        try:
            return await self._get("/System/Info")
        except Exception as e:
            print(f"Error getting system info: {e}")
            return {}

    async def get_item(self, item_id: str) -> dict:
        """Get a single item details by id"""
        # /Items/{id} answers 400 here without a user id; the list endpoint
        # filtered to one id does not.
        try:
            result = await self._get(f"/Items?Ids={item_id}&Fields=Overview,Genres")
            items = result.get('Items', []) if isinstance(result, dict) else []
            return items[0] if items else {}
        except Exception as e:
            print(f"Error getting item {item_id}: {e}")
            return {}

    async def search_movies(self, term: str = "", limit: int = 25) -> List[dict]:
        """Films whose title matches term (all films, by name, when term is empty)."""
        endpoint = (f"/Items?IncludeItemTypes=Movie&Recursive=true&Limit={limit}"
                    f"&SortBy=SortName&Fields=ProductionYear&{FILMS_NOT_COLLECTIONS}")
        if term:
            endpoint += f"&SearchTerm={quote(term)}"
        result = await self._get(endpoint)
        return result.get('Items', []) if isinstance(result, dict) else []

    async def random_movies(self, count: int) -> List[dict]:
        result = await self._get(
            f"/Items?IncludeItemTypes=Movie&Recursive=true&SortBy=Random&Limit={count}"
            f"&Fields=ProductionYear&{FILMS_NOT_COLLECTIONS}")
        return result.get('Items', []) if isinstance(result, dict) else []

    async def get_image(self, item_id: str, image_type: str = "Primary",
                        max_width: int = 1000) -> bytes | None:
        """An item's image as JPEG bytes, or None if it has none.

        The bot uploads these to Discord itself. Linking to Jellyfin's public
        address would not work: it answers Canadian addresses only, and
        Discord fetches images from elsewhere.
        """
        url = f"{self.base_url}/Items/{item_id}/Images/{image_type}?maxWidth={max_width}&format=Jpg"
        session = await self._get_session()
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            return await resp.read()

    async def get_series_episode_count(self, series_id: str) -> int:
        """Return the total number of episodes for a series id"""
        try:
            # Query items filtered by SeriesId and include only Episodes; use Limit=1 to get TotalRecordCount
            endpoint = f"/Items?SeriesId={series_id}&IncludeItemTypes=Episode&Recursive=true&Limit=1"
            result = await self._get(endpoint)

            # Result can be a dict with TotalRecordCount
            if isinstance(result, dict):
                return int(result.get('TotalRecordCount', 0))
            # Fallback
            return 0
        except Exception as e:
            print(f"Error getting series episode count for {series_id}: {e}")
            return 0
