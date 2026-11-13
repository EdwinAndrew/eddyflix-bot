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

    async def get_item(self, item_id: str, user_id: str | None = None) -> dict:
        """Get a single item details by id. With a user id it carries that user's
        progress in UserData."""
        # /Items/{id} answers 400 here without a user id; the list endpoint
        # filtered to one id does not.
        endpoint = f"/Items?Ids={item_id}&Fields=Overview,Genres,ProductionYear,ChildCount,RecursiveItemCount"
        if user_id:
            endpoint += f"&userId={user_id}"
        try:
            result = await self._get(endpoint)
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

    async def search_titles(self, term: str = "", limit: int = 25) -> List[dict]:
        """Films and series whose title matches term (all of them, by name, when term is empty)."""
        endpoint = (f"/Items?IncludeItemTypes=Movie,Series&Recursive=true&Limit={limit}"
                    f"&SortBy=SortName&Fields=ProductionYear&{FILMS_NOT_COLLECTIONS}")
        if term:
            endpoint += f"&SearchTerm={quote(term)}"
        result = await self._get(endpoint)
        return result.get('Items', []) if isinstance(result, dict) else []

    async def search_series(self, term: str = "", limit: int = 25) -> List[dict]:
        """Series whose title matches term (all of them, by name, when term is empty)."""
        endpoint = (f"/Items?IncludeItemTypes=Series&Recursive=true&Limit={limit}"
                    f"&SortBy=SortName&Fields=ProductionYear")
        if term:
            endpoint += f"&SearchTerm={quote(term)}"
        result = await self._get(endpoint)
        return result.get('Items', []) if isinstance(result, dict) else []

    async def episodes_by_date_added(self, start: int = 0, limit: int = 100) -> List[dict]:
        """One page of episodes, newest DateCreated first, for the subscription poll.

        Jellyfin 12 ignores MinDateCreated here, so the caller pages until it
        reaches episodes older than it wants.
        """
        endpoint = (f"/Items?IncludeItemTypes=Episode&Recursive=true&SortBy=DateCreated"
                    f"&SortOrder=Descending&StartIndex={start}&Limit={limit}"
                    f"&Fields=DateCreated&EnableImages=false&EnableUserData=false")
        result = await self._get(endpoint)
        return result.get('Items', []) if isinstance(result, dict) else []

    async def resume_items(self, user_id: str, limit: int = 5) -> List[dict]:
        """What this user stopped part-way through, most recent first."""
        result = await self._get(f"/UserItems/Resume?userId={user_id}&Limit={limit}")
        return result.get('Items', []) if isinstance(result, dict) else []

    async def next_up_items(self, user_id: str, limit: int = 5) -> List[dict]:
        """The next episode of each show this user is watching."""
        result = await self._get(f"/Shows/NextUp?userId={user_id}&Limit={limit}&enableResumable=true")
        return result.get('Items', []) if isinstance(result, dict) else []

    async def get_users(self) -> List[dict]:
        """Jellyfin's users, as {"Name", "Id", ...}."""
        result = await self._get("/Users")
        return result if isinstance(result, list) else []

    async def added_by_date(self, item_type: str, start: int = 0, limit: int = 100) -> List[dict]:
        """One page of films or series, newest DateCreated first, for the weekly
        "New on EddyFlix" post. Like episodes, the caller pages until it is past
        the date it wants."""
        endpoint = (f"/Items?IncludeItemTypes={item_type}&Recursive=true&SortBy=DateCreated"
                    f"&SortOrder=Descending&StartIndex={start}&Limit={limit}"
                    f"&Fields=DateCreated,ProductionYear&EnableImages=false&EnableUserData=false"
                    f"&{FILMS_NOT_COLLECTIONS}")
        result = await self._get(endpoint)
        return result.get('Items', []) if isinstance(result, dict) else []

    async def movies_by_date_added(self, start: int = 0, limit: int = 50) -> List[dict]:
        """One page of films, newest DateCreated first, for the admin summary."""
        return await self.added_by_date("Movie", start, limit)

    async def get_storage(self) -> dict:
        """Free and used space for Jellyfin's own folders and each library's (admin key)."""
        result = await self._get("/System/Info/Storage")
        return result if isinstance(result, dict) else {}

    async def next_up(self, user_id: str, series_id: str) -> dict | None:
        """The episode of a series this user would watch next, half-watched ones
        included, or None when there is none."""
        # NextUp honours seriesId, unlike /Items, which ignores SeriesId.
        result = await self._get(f"/Shows/NextUp?userId={user_id}&seriesId={series_id}"
                                 f"&Limit=1&enableResumable=true")
        items = result.get('Items', []) if isinstance(result, dict) else []
        return items[0] if items else None

    async def random_movies(self, count: int) -> List[dict]:
        result = await self._get(
            f"/Items?IncludeItemTypes=Movie&Recursive=true&SortBy=Random&Limit={count}"
            f"&Fields=ProductionYear&{FILMS_NOT_COLLECTIONS}")
        return result.get('Items', []) if isinstance(result, dict) else []

    async def pick_pool(self, types: str = "Movie,Series", genre: str | None = None,
                        user_id: str | None = None, unwatched: bool = False) -> List[dict]:
        """Every film and/or show matching the filters, for /pick to choose from.
        Jellyfin has no runtime filter (MaxRunTimeTicks is ignored), so the caller
        filters by length."""
        endpoint = (f"/Items?IncludeItemTypes={types}&Recursive=true&Limit=1000"
                    f"&EnableImages=false&{FILMS_NOT_COLLECTIONS}")
        if genre:
            endpoint += f"&Genres={quote(genre)}"
        if user_id:
            endpoint += f"&userId={user_id}"
            # IsPlayed without a user id gets a 400. For a show it means not finished.
            if unwatched:
                endpoint += "&IsPlayed=false"
        result = await self._get(endpoint)
        return result.get('Items', []) if isinstance(result, dict) else []

    async def get_genres(self, types: str = "Movie,Series") -> List[str]:
        """The genres of the library's films and/or shows, by name."""
        result = await self._get(f"/Genres?Recursive=true&IncludeItemTypes={types}")
        items = result.get('Items', []) if isinstance(result, dict) else []
        return [g['Name'] for g in items if g.get('Name')]

    async def played_episodes(self, user_id: str, limit: int = 100) -> List[dict]:
        """Episodes this user finished, most recently played first."""
        result = await self._get(
            f"/Items?userId={user_id}&IncludeItemTypes=Episode&Recursive=true&IsPlayed=true"
            f"&SortBy=DatePlayed&SortOrder=Descending&Limit={limit}&EnableImages=false")
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

    async def find_series_id(self, name: str) -> str | None:
        """The id of the series with exactly this name, or None if it is not in the library."""
        result = await self._get(f"/Items?IncludeItemTypes=Series&Recursive=true&SearchTerm={quote(name)}")
        for item in result.get('Items', []) if isinstance(result, dict) else []:
            if item.get('Name') == name:
                return item.get('Id')
        return None

    async def get_user_names(self) -> List[str]:
        """Jellyfin's user names, for /jf-link."""
        result = await self._get("/Users")
        return sorted((u.get('Name') for u in result if u.get('Name')), key=str.lower) if isinstance(result, list) else []

    async def get_user_id(self, name: str) -> str | None:
        """The id of the Jellyfin user with this name (any case), or None."""
        result = await self._get("/Users")
        for user in result if isinstance(result, list) else []:
            if (user.get('Name') or '').lower() == name.lower():
                return user.get('Id')
        return None

    async def get_series_episode_count(self, series_id: str) -> int:
        """Episodes of a series in the library; 0 if Jellyfin does not answer."""
        try:
            # ParentId, not SeriesId: Jellyfin 12 ignores SeriesId on /Items and
            # counted every episode in the library (1239) for every show.
            endpoint = f"/Items?ParentId={series_id}&IncludeItemTypes=Episode&Recursive=true&Limit=1"
            result = await self._get(endpoint)

            # Result can be a dict with TotalRecordCount
            if isinstance(result, dict):
                return int(result.get('TotalRecordCount', 0))
            # Fallback
            return 0
        except Exception as e:
            print(f"Error getting series episode count for {series_id}: {e}")
            return 0
