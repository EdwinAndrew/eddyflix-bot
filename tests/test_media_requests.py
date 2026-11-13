"""/request: what a title's status reads as, which seasons are offered, who a
request is made as, and when a request counts as ready."""
import asyncio

import aiosqlite
import pytest

import config
import media_requests as mr
import watch_data


def movie(status=None, requests=(), **extra):
    info = {"status": status, "requests": list(requests)} if status or requests else None
    return {"id": 353081, "title": "Mission: Impossible - Fallout", "releaseDate": "2018-07-25",
            "mediaInfo": info, **extra}


def show(seasons=5, media=None, season_status=None, requests=(), episodes=None):
    """A show with seasons 1..n (and a special season 0) and what Seerr knows of it."""
    tmdb = [{"seasonNumber": 0, "episodeCount": 3}] + [
        {"seasonNumber": n, "episodeCount": (episodes or {}).get(n, 10)} for n in range(1, seasons + 1)]
    info = None
    if media or season_status or requests:
        info = {"status": media or mr.MEDIA_UNKNOWN, "requests": list(requests),
                "seasons": [{"seasonNumber": n, "status": s} for n, s in (season_status or {}).items()],
                "jellyfinMediaId": "a" * 32 if media in (mr.MEDIA_PARTIAL, mr.MEDIA_AVAILABLE) else None}
    return {"id": 106379, "name": "Fallout", "firstAirDate": "2024-04-10", "seasons": tmdb, "mediaInfo": info}


def request(status, seasons=(), request_id=7, season_status=mr.REQUEST_APPROVED, is4k=False):
    return {"id": request_id, "status": status, "is4k": is4k,
            "seasons": [{"seasonNumber": n, "status": season_status} for n in seasons]}


# --- labels and search -------------------------------------------------------

def test_labels_and_choices():
    hit = {"mediaType": "tv", "id": 106379, "name": "Fallout", "firstAirDate": "2024-04-10",
           "mediaInfo": {"status": mr.MEDIA_PARTIAL}}
    assert mr.label(hit) == "Fallout (2024)"
    assert mr.choice_label(hit) == "📺 Fallout (2024) · partly in the library"
    assert mr.choice_label({"mediaType": "movie", "id": 1, "title": "Up", "releaseDate": "2009-05-28"}) == "🎬 Up (2009)"
    assert mr.label({"title": "Yellowstone (2018)", "releaseDate": "2018-06-20"}) == "Yellowstone (2018)"
    assert mr.label({"title": "Untitled"}) == "Untitled"
    assert mr.choice_value(hit) == "tv:106379"
    assert mr.parse_choice("tv:106379") == ("tv", 106379)
    assert mr.parse_choice("movie:12") == ("movie", 12)
    assert mr.parse_choice("person:12") is None
    assert mr.parse_choice("fallout") is None


def test_best_result_prefers_the_exact_title():
    results = [{"title": "Mission: Impossible - Fallout"}, {"title": "The Fallout"}, {"name": "Fallout"}]
    assert mr.best_result("fallout", results) == {"name": "Fallout"}
    assert mr.best_result("the fallout", results) == {"title": "The Fallout"}
    assert mr.best_result("mission", results) == results[0]
    assert mr.best_result("x", []) is None
    knights = [{"title": "The Dark Knight Rises"}, {"title": "The Dark Knight"}]
    assert mr.best_result("dark knight", knights) == {"title": "The Dark Knight"}


# --- status ------------------------------------------------------------------

def test_film_statuses():
    assert mr.library_status("movie", movie()) == mr.REQUESTABLE
    assert mr.library_status("movie", movie(mr.MEDIA_AVAILABLE)) == mr.AVAILABLE
    assert mr.library_status("movie", movie(mr.MEDIA_PENDING, [request(mr.REQUEST_PENDING)])) == mr.REQUESTED
    assert mr.library_status("movie", movie(mr.MEDIA_PROCESSING)) == mr.REQUESTED
    assert mr.library_status("movie", movie(mr.MEDIA_BLOCKLISTED)) == mr.BLOCKED
    # Deleted from the library, or the last request declined: it can be asked for again
    assert mr.library_status("movie", movie(mr.MEDIA_DELETED)) == mr.REQUESTABLE
    assert mr.library_status("movie", movie(mr.MEDIA_UNKNOWN, [request(mr.REQUEST_DECLINED)])) == mr.REQUESTABLE
    # A failed request still stands: Seerr would refuse a second one
    assert mr.library_status("movie", movie(mr.MEDIA_UNKNOWN, [request(mr.REQUEST_FAILED)])) == mr.REQUESTED


def test_only_requestable_films_get_a_button():
    assert mr.can_request("movie", movie())
    for status in (mr.MEDIA_AVAILABLE, mr.MEDIA_PROCESSING, mr.MEDIA_BLOCKLISTED):
        assert not mr.can_request("movie", movie(status))


def test_status_lines():
    assert mr.status_line("movie", movie(mr.MEDIA_AVAILABLE)) == "✅ Already in the library"
    assert mr.status_line("movie", movie()) == "➕ Not in the library yet. It can be requested"
    assert mr.status_line("movie", movie(mr.MEDIA_PENDING, [request(mr.REQUEST_PENDING)])) == \
        "⏳ Already requested, waiting for approval"
    assert mr.status_line("movie", movie(mr.MEDIA_PROCESSING, [request(mr.REQUEST_APPROVED)])) == \
        "⏳ Already requested, on its way"
    partial = show(5, mr.MEDIA_PARTIAL, {1: mr.MEDIA_AVAILABLE, 2: mr.MEDIA_AVAILABLE, 3: mr.MEDIA_PARTIAL},
                   [request(mr.REQUEST_APPROVED, [4])])
    assert mr.status_line("tv", partial) == ("🟡 Partly in the library: seasons 1–3\n"
                                            "⏳ Season 4 already requested, on its way\n"
                                            "➕ Season 5 can be requested")


def test_show_seasons_already_there_or_asked_for_are_not_offered():
    # Specials and seasons TMDB lists without episodes are never offered
    details = show(6, mr.MEDIA_PARTIAL, {1: mr.MEDIA_AVAILABLE, 2: mr.MEDIA_PARTIAL, 3: mr.MEDIA_UNKNOWN,
                                         4: mr.MEDIA_DELETED},
                   [request(mr.REQUEST_PENDING, [5]), request(mr.REQUEST_DECLINED, [3], request_id=8),
                    request(mr.REQUEST_PENDING, [3], request_id=9, is4k=True)],
                   episodes={6: 0})
    assert mr.tv_seasons(details) == [1, 2, 3, 4, 5]
    assert mr.open_seasons(details) == [3, 4]
    assert mr.library_status("tv", details) == mr.PARTIAL
    assert mr.can_request("tv", details)


def test_show_statuses():
    assert mr.library_status("tv", show()) == mr.REQUESTABLE
    assert mr.library_status("tv", show(media=mr.MEDIA_AVAILABLE)) == mr.AVAILABLE
    every_season = show(2, mr.MEDIA_PENDING, requests=[request(mr.REQUEST_PENDING, [1, 2])])
    assert mr.library_status("tv", every_season) == mr.REQUESTED
    assert not mr.can_request("tv", every_season)
    some_seasons = show(3, mr.MEDIA_PENDING, requests=[request(mr.REQUEST_PENDING, [1])])
    assert mr.library_status("tv", some_seasons) == mr.REQUESTED
    assert mr.can_request("tv", some_seasons)


# --- season choice -----------------------------------------------------------

def test_new_show_offers_all_or_the_latest():
    assert mr.season_choices(show(5)) == [("Request all seasons", [1, 2, 3, 4, 5]),
                                          ("Request season 5 only", [5])]


def test_partly_there_offers_the_missing_ones():
    details = show(5, mr.MEDIA_PARTIAL, {1: mr.MEDIA_AVAILABLE, 2: mr.MEDIA_AVAILABLE})
    assert mr.season_choices(details) == [("Request the 3 missing seasons", [3, 4, 5]),
                                          ("Request season 5 only", [5])]


def test_latest_already_asked_for():
    details = show(5, mr.MEDIA_PARTIAL, {1: mr.MEDIA_AVAILABLE}, [request(mr.REQUEST_PENDING, [5])])
    assert mr.season_choices(details) == [("Request the 3 missing seasons", [2, 3, 4])]


def test_one_season_left_is_one_button():
    assert mr.season_choices(show(1)) == [("Request season 1", [1])]
    details = show(3, mr.MEDIA_PARTIAL, {1: mr.MEDIA_AVAILABLE, 2: mr.MEDIA_AVAILABLE})
    assert mr.season_choices(details) == [("Request season 3", [3])]
    assert mr.season_choices(show(2, mr.MEDIA_AVAILABLE, {1: mr.MEDIA_AVAILABLE, 2: mr.MEDIA_AVAILABLE})) == []


def test_season_range():
    assert mr.season_range([1, 2, 3, 5]) == "1–3, 5"
    assert mr.season_range([7]) == "7"
    assert mr.seasons_phrase([1, 2]) == "seasons 1–2"
    assert mr.seasons_phrase([4]) == "season 4"


# --- who it is made as -------------------------------------------------------

USERS = [{"id": 1, "jellyfinUsername": "eddy", "displayName": "eddy"},
         {"id": 2, "jellyfinUsername": "Kevin", "displayName": "Kev"},
         {"id": 6, "jellyfinUsername": None, "username": "sarah", "displayName": "Sarah L"}]


def test_seerr_user_matches_the_jellyfin_name():
    assert mr.seerr_user_id(USERS, "kevin") == 2
    assert mr.seerr_user_id(USERS, "Eddy ") == 1
    assert mr.seerr_user_id(USERS, "Sarah") == 6        # a local Seerr user, by user name
    assert mr.seerr_user_id(USERS, "Kev") == 2          # display name as a last resort
    assert mr.seerr_user_id(USERS, "nobody") is None
    assert mr.seerr_user_id(USERS, None) is None


def test_placed_message():
    approved = mr.placed_message({"status": mr.REQUEST_APPROVED}, "Up (2009)", None, "Kevin")
    assert approved.startswith("✅ Requested **Up (2009)** as **Kevin**. It's approved and on its way.")
    assert "/link" not in approved
    pending = mr.placed_message({"status": mr.REQUEST_PENDING}, "Fallout (2024)", [2], None)
    assert pending.startswith("✅ Requested season 2 of **Fallout (2024)**. It's waiting for approval.")
    assert "/link" in pending


def test_refusals():
    assert "already asked" in mr.refusal(409, "Request for this media already exists.")
    assert "already in the library" in mr.refusal(202, "No seasons available to request")
    assert mr.refusal(403, "Movie Quota exceeded.") == "Seerr said no: Movie Quota exceeded."
    assert "500" in mr.refusal(500, "boom")


# --- when it is ready --------------------------------------------------------

def seerr_request(status, media_status, kind="movie", seasons=()):
    return {"id": 7, "type": kind, "status": status,
            "media": {"status": media_status, "jellyfinMediaId": "a" * 32},
            "seasons": [{"seasonNumber": n, "status": s} for n, s in seasons]}


def test_film_is_ready_once_available():
    assert mr.outcome(seerr_request(mr.REQUEST_APPROVED, mr.MEDIA_PROCESSING)) is None
    assert mr.outcome(seerr_request(mr.REQUEST_PENDING, mr.MEDIA_PENDING)) is None
    assert mr.outcome(seerr_request(mr.REQUEST_FAILED, mr.MEDIA_UNKNOWN)) is None
    assert mr.outcome(seerr_request(mr.REQUEST_APPROVED, mr.MEDIA_AVAILABLE)) == mr.READY
    assert mr.outcome(seerr_request(mr.REQUEST_COMPLETED, mr.MEDIA_AVAILABLE)) == mr.READY
    assert mr.outcome(seerr_request(mr.REQUEST_DECLINED, mr.MEDIA_UNKNOWN)) == mr.DECLINED
    assert mr.outcome(None) == mr.GONE


def test_show_is_ready_when_its_seasons_are():
    waiting = seerr_request(mr.REQUEST_APPROVED, mr.MEDIA_PARTIAL, "tv",
                            [(4, mr.REQUEST_APPROVED), (5, mr.REQUEST_APPROVED)])
    assert mr.outcome(waiting) is None
    assert mr.needs_seasons(waiting)
    # Seerr marks a season done only when all its episodes are in; partly in is enough to watch
    assert mr.outcome(waiting, {4: mr.MEDIA_AVAILABLE, 5: mr.MEDIA_UNKNOWN}) is None
    assert mr.outcome(waiting, {4: mr.MEDIA_AVAILABLE, 5: mr.MEDIA_PARTIAL}) == mr.READY
    done = seerr_request(mr.REQUEST_APPROVED, mr.MEDIA_PARTIAL, "tv",
                         [(4, mr.REQUEST_COMPLETED), (5, mr.REQUEST_COMPLETED)])
    assert mr.outcome(done) == mr.READY
    assert not mr.needs_seasons(done)
    # Not yet in at all: nothing to look up
    assert not mr.needs_seasons(seerr_request(mr.REQUEST_APPROVED, mr.MEDIA_PROCESSING, "tv", [(1, 2)]))


# --- the table ---------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "bot.db")
    monkeypatch.setattr(config, "DB_PATH", path)
    asyncio.run(watch_data.init_database())
    return path


def test_each_member_is_noted_once_and_settled_once(db_path):
    async def go():
        async with aiosqlite.connect(db_path) as db:
            assert await mr.track(db, 7, 111, "tv", 106379, "Fallout (2024)", "/p.jpg", [1, 2], 2)
            assert not await mr.track(db, 7, 111, "tv", 106379, "Fallout (2024)", "/p.jpg", [1, 2], 2)
            assert await mr.track(db, 7, 222, "tv", 106379, "Fallout (2024)", "/p.jpg", [1, 2], None)
            assert await mr.track(db, 8, 111, "movie", 1, "Up (2009)", None, None, None)
            await db.commit()
            waiting = await mr.waiting(db)
            assert [r["discord_user_id"] for r in waiting[7]] == ["111", "222"]
            assert waiting[7][0]["seasons"] == [1, 2] and waiting[8][0]["seasons"] == []
            await mr.settle(db, 7, "111", mr.READY)
            await mr.settle(db, 7, "111", mr.DECLINED)      # already settled: no change
            await db.commit()
            assert [r["discord_user_id"] for r in (await mr.waiting(db))[7]] == ["222"]
            async with db.execute("SELECT state FROM seerr_requests WHERE request_id = 7 AND discord_user_id = '111'") as c:
                assert (await c.fetchone())[0] == "ready"
    asyncio.run(go())


def test_seasons_already_in_are_not_waited_for():
    # Fallout: both requested seasons partly in (season 2 still airing)
    airing = show(2, mr.MEDIA_PARTIAL, {1: mr.MEDIA_PARTIAL, 2: mr.MEDIA_PARTIAL},
                  [request(mr.REQUEST_APPROVED, [1, 2])])
    assert mr.status_line("tv", airing) == "🟡 Partly in the library: seasons 1–2"
    assert mr.followable_requests(airing) == []
    assert not mr.can_request("tv", airing)
    # Requested seasons that are partly done read as the ones still missing
    simpsons = show(4, mr.MEDIA_PARTIAL, {1: mr.MEDIA_AVAILABLE, 2: mr.MEDIA_AVAILABLE},
                    [request(mr.REQUEST_APPROVED, [1, 2, 3])])
    assert "⏳ Season 3 already requested, on its way" in mr.status_line("tv", simpsons)
    assert [r["id"] for r in mr.followable_requests(simpsons)] == [7]
