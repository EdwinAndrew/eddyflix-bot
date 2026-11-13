"""/request: reading what Seerr says about a title, choosing seasons, and
deciding when a request made from Discord is ready to announce.

Requests made from Discord are kept in the seerr_requests table, one row per
request and Discord member, so a restart neither repeats a DM nor misses one.
"""
from datetime import datetime, timezone

# Seerr's numbers (server/constants/media.ts). A request:
REQUEST_PENDING, REQUEST_APPROVED, REQUEST_DECLINED, REQUEST_FAILED, REQUEST_COMPLETED = 1, 2, 3, 4, 5
# ...and a film, show or season:
MEDIA_UNKNOWN, MEDIA_PENDING, MEDIA_PROCESSING, MEDIA_PARTIAL, MEDIA_AVAILABLE, MEDIA_BLOCKLISTED, MEDIA_DELETED = range(1, 8)

# What /request says about a title
AVAILABLE, PARTIAL, REQUESTED, REQUESTABLE, BLOCKED = "available", "partial", "requested", "requestable", "blocked"


# --- reading Seerr's answers -------------------------------------------------

def title_of(result: dict) -> str:
    return result.get("title") or result.get("name") or "Unknown"


def year_of(result: dict) -> str:
    date = result.get("releaseDate") or result.get("firstAirDate") or ""
    return date[:4]


def label(result: dict) -> str:
    """"Fallout (2024)", as in the autocomplete and the card."""
    name, year = title_of(result), year_of(result)
    return f"{name} ({year})" if year and not name.endswith(f"({year})") else name


def choice_value(result: dict) -> str:
    """What the autocomplete hands back: "movie:353081" or "tv:106379"."""
    return f"{result.get('mediaType')}:{result.get('id')}"


def parse_choice(value: str) -> tuple[str, int] | None:
    kind, _, number = value.partition(":")
    return (kind, int(number)) if kind in ("movie", "tv") and number.isdigit() else None


def plain(text: str, article: bool = True) -> str:
    """Lower case, without punctuation, and without a leading article unless asked."""
    text = " ".join("".join(c if c.isalnum() else " " for c in text.lower()).split())
    for word in ("the ", "a ", "an "):
        if not article and text.startswith(word):
            return text[len(word):]
    return text


def best_result(typed: str, results: list[dict]) -> dict | None:
    """The exact title if Seerr found it (with or without "The"), else its first hit."""
    for article in (True, False):
        exact = [r for r in results if plain(title_of(r), article) == plain(typed, article)]
        if exact:
            return exact[0]
    return (results or [None])[0]


def media_status(details: dict) -> int:
    return (details.get("mediaInfo") or {}).get("status") or MEDIA_UNKNOWN


def live_requests(details: dict) -> list[dict]:
    """Requests that still stand: not declined, not done. 4K ones are left out."""
    return [r for r in (details.get("mediaInfo") or {}).get("requests") or []
            if not r.get("is4k") and r.get("status") not in (REQUEST_DECLINED, REQUEST_COMPLETED)]


def tv_seasons(details: dict) -> list[int]:
    """The seasons TMDB lists with episodes, without specials (as Seerr's "all" does)."""
    return sorted(s["seasonNumber"] for s in details.get("seasons") or []
                  if s.get("seasonNumber") and (s.get("episodeCount") or 0) > 0)


def seasons_in_library(details: dict) -> list[int]:
    return sorted(s["seasonNumber"] for s in (details.get("mediaInfo") or {}).get("seasons") or []
                  if s.get("status") in (MEDIA_PARTIAL, MEDIA_AVAILABLE))


def seasons_requested(details: dict) -> list[int]:
    return sorted({s["seasonNumber"] for r in live_requests(details) for s in r.get("seasons") or []})


def followable_requests(details: dict) -> list[dict]:
    """Live requests worth a "DM me": not the ones whose seasons are already partly in."""
    have = set(seasons_in_library(details))
    return [r for r in live_requests(details)
            if not (r.get("seasons") and all(s["seasonNumber"] in have for s in r["seasons"]))]


def open_seasons(details: dict) -> list[int]:
    """Seasons nobody has asked for and the library does not have. Mirrors
    Seerr's own check, so what is offered is what it will accept."""
    taken = set(seasons_requested(details))
    taken |= {s["seasonNumber"] for s in (details.get("mediaInfo") or {}).get("seasons") or []
              if s.get("status") not in (MEDIA_UNKNOWN, MEDIA_DELETED)}
    return [n for n in tv_seasons(details) if n not in taken]


def library_status(media_type: str, details: dict) -> str:
    status = media_status(details)
    if status == MEDIA_BLOCKLISTED:
        return BLOCKED
    if status == MEDIA_AVAILABLE:
        return AVAILABLE
    if media_type == "tv" and status == MEDIA_PARTIAL:
        return PARTIAL
    if status in (MEDIA_PENDING, MEDIA_PROCESSING) or live_requests(details):
        return REQUESTED
    return REQUESTABLE


def can_request(media_type: str, details: dict) -> bool:
    if media_type == "tv":
        return library_status(media_type, details) not in (BLOCKED, AVAILABLE) and bool(open_seasons(details))
    return library_status(media_type, details) == REQUESTABLE


def season_range(numbers: list[int]) -> str:
    """[1, 2, 3, 5] -> "1–3, 5"."""
    parts, numbers = [], sorted(set(numbers))
    i = 0
    while i < len(numbers):
        j = i
        while j + 1 < len(numbers) and numbers[j + 1] == numbers[j] + 1:
            j += 1
        parts.append(str(numbers[i]) if i == j else f"{numbers[i]}–{numbers[j]}")
        i = j + 1
    return ", ".join(parts)


def seasons_phrase(numbers: list[int]) -> str:
    return f"season{'s' if len(numbers) != 1 else ''} {season_range(numbers)}"


def status_line(media_type: str, details: dict) -> str:
    status = library_status(media_type, details)
    if status == AVAILABLE:
        return "✅ Already in the library"
    if status == BLOCKED:
        return "🚫 Can't be requested"
    lines = []
    if status == PARTIAL:
        have = seasons_in_library(details)
        lines.append("🟡 Partly in the library" + (f": {seasons_phrase(have)}" if have else ""))
    if status in (PARTIAL, REQUESTED):
        requests = live_requests(details)
        have = set(seasons_in_library(details))
        asked = [n for n in seasons_requested(details) if n not in have] if media_type == "tv" else []
        # Partly in with nothing more asked for: the requested seasons are already there
        if status == REQUESTED or (requests and asked):
            what = f"{seasons_phrase(asked).capitalize()} already requested" if asked else "Already requested"
            if media_status(details) == MEDIA_PROCESSING or any(r.get("status") == REQUEST_APPROVED for r in requests):
                what += ", on its way"
            elif requests and all(r.get("status") == REQUEST_PENDING for r in requests):
                what += ", waiting for approval"
            lines.append(f"⏳ {what}")
    if status == REQUESTABLE:
        lines.append("➕ Not in the library yet. It can be requested")
    elif media_type == "tv" and open_seasons(details):
        lines.append(f"➕ {seasons_phrase(open_seasons(details)).capitalize()} can be requested")
    return "\n".join(lines)


def season_choices(details: dict) -> list[tuple[str, list[int]]]:
    """The buttons for a show: everything missing, and the latest season on its own."""
    missing = open_seasons(details)
    if not missing:
        return []
    if len(missing) == 1:
        return [(f"Request season {missing[0]}", missing)]
    all_label = ("Request all seasons" if missing == tv_seasons(details)
                 else f"Request the {len(missing)} missing seasons")
    choices = [(all_label, missing)]
    latest = max(tv_seasons(details))
    if latest in missing:
        choices.append((f"Request season {latest} only", [latest]))
    return choices


def seerr_user_id(users: list[dict], jellyfin_name: str | None) -> int | None:
    """The Seerr user who is this Jellyfin user, matched on name, any case."""
    if not jellyfin_name:
        return None
    wanted = jellyfin_name.strip().lower()
    for field in ("jellyfinUsername", "username", "displayName"):
        for user in users:
            if (user.get(field) or "").strip().lower() == wanted:
                return user.get("id")
    return None


# --- when it is ready --------------------------------------------------------

READY, DECLINED, GONE = "ready", "declined", "gone"


def needs_seasons(request: dict) -> bool:
    """A show that is partly in: whether the requested seasons are there needs
    the show's own season list, which a request does not carry."""
    return (request.get("type") == "tv" and (request.get("media") or {}).get("status") == MEDIA_PARTIAL
            and outcome(request) is None)


def outcome(request: dict | None, season_status: dict[int, int] | None = None) -> str | None:
    """READY, DECLINED or GONE once the request is settled; None while waiting.

    Ready means something can be watched: Seerr has marked the request done or
    the title available, or (for a show) every season asked for is at least
    partly in. Seerr marks a season done only once all its episodes are in, which
    for a season still airing is weeks after the first one lands.
    """
    if request is None:
        return GONE
    status, media = request.get("status"), request.get("media") or {}
    if status == REQUEST_DECLINED:
        return DECLINED
    if status == REQUEST_COMPLETED or media.get("status") == MEDIA_AVAILABLE:
        return READY
    if request.get("type") == "tv":
        seasons = request.get("seasons") or []
        if seasons and all(s.get("status") == REQUEST_COMPLETED for s in seasons):
            return READY
        if seasons and season_status and all(
                season_status.get(s.get("seasonNumber")) in (MEDIA_PARTIAL, MEDIA_AVAILABLE) for s in seasons):
            return READY
    return None


def season_status(details: dict) -> dict[int, int]:
    return {s["seasonNumber"]: s.get("status") for s in (details.get("mediaInfo") or {}).get("seasons") or []}


# --- the seerr_requests table (created in watch_data.init_database) ----------

async def track(db, request_id: int, discord_user_id: int, media_type: str, tmdb_id: int, title: str,
                poster_path: str | None, seasons: list[int] | None, seerr_user_id: int | None) -> bool:
    """Note a request to announce to this member. False if it was already noted."""
    cursor = await db.execute("""
        INSERT OR IGNORE INTO seerr_requests
            (request_id, discord_user_id, media_type, tmdb_id, title, poster_path, seasons, seerr_user_id, requested_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (int(request_id), str(discord_user_id), media_type, int(tmdb_id), title, poster_path,
          ",".join(map(str, seasons)) if seasons else None, seerr_user_id, datetime.now(timezone.utc)))
    return cursor.rowcount > 0


async def waiting(db) -> dict[int, list[dict]]:
    """Requests not yet announced, by request id, each with who is waiting for it."""
    db_rows = []
    async with db.execute("""
        SELECT request_id, discord_user_id, media_type, tmdb_id, title, poster_path, seasons
        FROM seerr_requests WHERE state = 'waiting' ORDER BY requested_at
    """) as cursor:
        db_rows = await cursor.fetchall()
    grouped: dict[int, list[dict]] = {}
    for request_id, user_id, media_type, tmdb_id, title, poster_path, seasons in db_rows:
        grouped.setdefault(request_id, []).append({
            "request_id": request_id, "discord_user_id": user_id, "media_type": media_type,
            "tmdb_id": tmdb_id, "title": title, "poster_path": poster_path,
            "seasons": [int(s) for s in seasons.split(",")] if seasons else [],
        })
    return grouped


async def settle(db, request_id: int, discord_user_id: str, state: str):
    """Mark one member's row announced, declined or gone, so it is never sent again."""
    await db.execute("""
        UPDATE seerr_requests SET state = ?, settled_at = ?
        WHERE request_id = ? AND discord_user_id = ? AND state = 'waiting'
    """, (state, datetime.now(timezone.utc), int(request_id), str(discord_user_id)))


# --- wording -----------------------------------------------------------------

def choice_label(result: dict) -> str:
    """How a search hit reads in the autocomplete, with a hint when it is not new."""
    hint = {MEDIA_AVAILABLE: " · in the library", MEDIA_PARTIAL: " · partly in the library",
            MEDIA_PENDING: " · requested", MEDIA_PROCESSING: " · requested"}.get(media_status(result), "")
    return f"{'📺' if result.get('mediaType') == 'tv' else '🎬'} {label(result)}{hint}"


def placed_message(request: dict, title: str, seasons: list[int] | None, as_user: str | None) -> str:
    """The answer once Seerr has taken a request."""
    what = f"{seasons_phrase(seasons)} of **{title}**" if seasons else f"**{title}**"
    who = f" as **{as_user}**" if as_user else ""
    if request.get("status") == REQUEST_APPROVED:
        state = "It's approved and on its way."
    else:
        state = "It's waiting for approval."
    text = f"✅ Requested {what}{who}. {state} I'll DM you when it's ready to watch."
    if not as_user:
        text += "\n-# Made with the server's Seerr account. Use /jf-link so requests are made as you."
    return text


def refusal(status: int, message: str) -> str:
    """Seerr's no, in words."""
    if status == 409:
        return "Someone has already asked for that one."
    if status == 202:
        return "Those seasons are already in the library or requested."
    if status == 403:
        return f"Seerr said no: {message}"
    return f"❌ Seerr couldn't take the request ({status}: {message}). Try again later."
