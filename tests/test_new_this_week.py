"""The weekly "New on EddyFlix" post: the 7-day cutoff and paging, grouping into
films, new shows and new episodes, the text, and the poster grid's layout."""
import asyncio
import io
from datetime import datetime, timedelta, timezone

from PIL import Image

import new_this_week as nw

NOW = datetime(2026, 11, 15, 23, 0, tzinfo=timezone.utc)
CUT = nw.cutoff(NOW)


def stamp(days_ago: float) -> str:
    """A DateCreated the way Jellyfin writes it, seven decimal places and a Z."""
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "0Z"


def film(name, days_ago, year=2026, **extra):
    return {"Id": f"film-{name}", "Name": name, "ProductionYear": year, "DateCreated": stamp(days_ago),
            "LocationType": "FileSystem", **extra}


def series(sid, name, days_ago, year=2020):
    return {"Id": sid, "Name": name, "ProductionYear": year, "DateCreated": stamp(days_ago)}


def ep(sid, show, season, number, days_ago=1, **extra):
    return {"Id": f"{sid}-{season}-{number}", "SeriesId": sid, "SeriesName": show, "ParentIndexNumber": season,
            "IndexNumber": number, "DateCreated": stamp(days_ago), "LocationType": "FileSystem", **extra}


# --- the cutoff and paging ---------------------------------------------------------

class Pages:
    """A fake Jellyfin listing, newest first, that ignores any date filter."""
    def __init__(self, items):
        self.items, self.calls = items, 0

    async def __call__(self, start, limit):
        self.calls += 1
        return self.items[start:start + limit]


def test_cutoff_is_seven_days():
    assert CUT == NOW - timedelta(days=7)
    assert nw.added_after({"DateCreated": stamp(6.9)}, CUT)
    assert not nw.added_after({"DateCreated": stamp(7)}, CUT)
    assert not nw.added_after({"DateCreated": None}, CUT)


def test_paging_stops_once_past_the_cutoff():
    items = [film(f"f{n}", n * 0.5) for n in range(40)]          # 0 to 19.5 days old
    fetch = Pages(items)
    found = asyncio.run(nw.added_since(fetch, CUT, page=5))
    assert [f["Name"] for f in found] == [f"f{n}" for n in range(14)]   # under 7 days
    assert fetch.calls == 3                                           # the third page reaches 7 days


def test_paging_ends_on_a_short_page():
    fetch = Pages([film("a", 1), film("b", 2)])
    assert len(asyncio.run(nw.added_since(fetch, CUT, page=5))) == 2
    assert fetch.calls == 1


# --- grouping ----------------------------------------------------------------------

def test_films_new_shows_and_new_episodes():
    films = [film("Old Film", 2, year=1984), film("The Odyssey", 1)]
    new = [series("curb", "Curb Your Enthusiasm", 3, 2000), series("empty", "Still Downloading", 1)]
    episodes = ([ep("curb", "Curb Your Enthusiasm", 1, n) for n in range(1, 11)]
                + [ep("bear", "The Bear", 4, n) for n in range(1, 4)]
                + [ep("simpsons", "The Simpsons", 9, n) for n in range(1, 26)])
    entries = nw.collect(films, new, episodes)
    assert [(e.kind, e.name, e.count) for e in entries] == [
        ("film", "The Odyssey", 0),                 # newest first
        ("film", "Old Film", 0),
        ("show", "Curb Your Enthusiasm", 10),       # new series; one with no episodes yet is left out
        ("episodes", "The Simpsons", 25),           # most episodes first
        ("episodes", "The Bear", 3),
    ]
    assert entries[2].item_id == "curb" and entries[2].label == "Curb Your Enthusiasm (2000)"


def test_one_episode_twice_and_virtual_items_count_once_or_not_at_all():
    episodes = [ep("bear", "The Bear", 4, 1), ep("bear", "The Bear", 4, 1, Id="other-file"),
                ep("bear", "The Bear", 4, 2, LocationType="Virtual")]
    films = [film("Missing", 1, LocationType="Virtual")]
    entries = nw.collect(films, [], episodes)
    assert [(e.name, e.count) for e in entries] == [("The Bear", 1)]


def test_nothing_new_is_nothing():
    assert nw.collect([], [series("x", "X", 1)], []) == []


# --- the text ----------------------------------------------------------------------

def entry(kind, name, count=0, season=1, year=None):
    return nw.Entry(kind, f"id-{name}", name, year,
                    episodes=[ep("s", name, season, n) for n in range(1, count + 1)])


def test_lines_and_captions():
    bear = entry("episodes", "The Bear", 10, season=4)
    assert nw.line(bear, "https://jf/bear") == "[The Bear](https://jf/bear) · 10 new episodes · S04E01–E10"
    assert nw.caption(bear) == "+10 episodes"
    assert nw.line(entry("episodes", "Fallout", 1), None) == "**Fallout** · 1 new episode · S01E01"
    assert nw.caption(entry("episodes", "Fallout", 1)) == "+1 episode"
    assert nw.line(entry("show", "Curb", 120, year=2000), None) == "**Curb (2000)** · 120 episodes"
    assert nw.line(entry("film", "Yellowstone (2018)", year=2018), None) == "**Yellowstone (2018)**"
    assert nw.caption(entry("film", "Dune")) == "New film"


def test_a_long_episode_range_is_left_out():
    gappy = nw.Entry("episodes", "s", "The Simpsons",
                     episodes=[ep("s", "The Simpsons", 9, n) for n in range(1, 30, 2)])
    assert nw.line(gappy, None) == "**The Simpsons** · 15 new episodes"


def test_description_sections_and_counts():
    entries = [entry("film", "Dune", year=2021), entry("show", "Curb", 3), entry("episodes", "The Bear", 2)]
    start, end = datetime(2026, 11, 8), datetime(2026, 11, 15)
    text = nw.description(entries, lambda i: None, start, end)
    assert text == ("Added Nov 8 – Nov 15\n\n**🎬 New films**\n**Dune (2021)**\n\n**📺 New shows**\n"
                    "**Curb** · 3 episodes\n\n**➕ New episodes**\n**The Bear** · 2 new episodes · S01E01–E02")
    assert nw.counts(entries) == "1 film · 1 new show · new episodes of 1 show"
    assert nw.counts(entries[:1] * 2) == "2 films"


def test_description_is_cut_short_under_discords_limit():
    entries = [entry("film", f"A Film With Quite A Long Title Number {n}") for n in range(200)]
    text = nw.description(entries, lambda i: f"https://jellyfin.example.com/web/#/details?id={i}",
                          datetime(2026, 11, 8), datetime(2026, 11, 15))
    assert len(text) <= nw.DESCRIPTION_LIMIT
    shown = text.count("](https://")
    assert text.endswith(f"…and {200 - shown} more")


# --- the grid ----------------------------------------------------------------------

def test_tiles_keep_the_last_one_for_more():
    entries = [entry("film", str(n)) for n in range(20)]
    shown, more = nw.tiles(entries, max_tiles=12)
    assert len(shown) == 11 and more == 9
    assert nw.tiles(entries[:12], max_tiles=12) == (entries[:12], 0)


def test_grid_rows_are_balanced_and_centred():
    w, h, pos = nw.grid_layout(7, max_cols=6)
    assert [y for _, y in pos].count(pos[0][1]) == 4            # 4 then 3, not 6 then 1
    assert len({y for _, y in pos}) == 2
    step = nw.POSTER_W + nw.GAP
    assert w == 2 * nw.PAD + 4 * nw.POSTER_W + 3 * nw.GAP
    assert h == nw.HEADER_H + 2 * (nw.POSTER_H + nw.CAPTION_H) + nw.GAP + nw.PAD
    assert pos[0][0] == nw.PAD and pos[4][0] == nw.PAD + step // 2   # the short row is centred

    w, h, pos = nw.grid_layout(12)
    assert len({x for x, _ in pos}) == 6 and len({y for _, y in pos}) == 2

    w, _, pos = nw.grid_layout(1)                                # never narrower than three posters
    assert w == 2 * nw.PAD + 3 * nw.POSTER_W + 2 * nw.GAP
    assert pos == [((w - nw.POSTER_W) // 2, nw.HEADER_H)]


def test_render_grid_draws_posters_and_placeholders():
    poster = io.BytesIO()
    Image.new("RGB", (300, 450), (200, 30, 30)).save(poster, format="JPEG")
    entries = [entry("film", "Dune"), entry("episodes", "A Show With No Poster At All", 3)]
    png = nw.render_grid(entries, {"id-Dune": poster.getvalue()}, datetime(2026, 11, 8), datetime(2026, 11, 15))
    img = Image.open(io.BytesIO(png))
    w, h, pos = nw.grid_layout(2)
    assert img.size == (w, h)
    x, y = pos[0]
    assert img.getpixel((x + nw.POSTER_W // 2, y + nw.POSTER_H // 2))[0] > 150     # the red poster

    many = [entry("film", str(n)) for n in range(15)]
    assert Image.open(io.BytesIO(nw.render_grid(many, {}, datetime(2026, 11, 8), datetime(2026, 11, 15)))).size \
        == nw.grid_layout(12)[:2]


# --- the request line ----------------------------------------------------------------

def test_request_line_only_says_what_is_set_up():
    assert nw.request_line(True, "https://seerr.example") == \
        "🙋 Missing something? Ask for it with `/request`, or on [Seerr](https://seerr.example)."
    assert nw.request_line(True, "") == "🙋 Missing something? Ask for it with `/request`."
    assert nw.request_line(False, "https://seerr.example") == \
        "🙋 Missing something? Ask for it on [Seerr](https://seerr.example)."
    assert nw.request_line(False, "") is None


def test_the_request_line_closes_the_post():
    entries = [entry("film", "Dune", year=2021)]
    start, end = datetime(2026, 11, 8), datetime(2026, 11, 15)
    tail = nw.request_line(True, "https://seerr.example")
    assert nw.description(entries, lambda i: None, start, end, tail) == \
        nw.description(entries, lambda i: None, start, end) + "\n\n" + tail
    assert nw.description(entries, lambda i: None, start, end, None) == nw.description(entries, lambda i: None, start, end)


def test_a_long_post_keeps_room_for_the_request_line():
    entries = [entry("film", f"A Film With Quite A Long Title Number {n}") for n in range(200)]
    tail = nw.request_line(True, "https://seerr.example")
    text = nw.description(entries, lambda i: f"https://jellyfin.example.com/web/#/details?id={i}",
                          datetime(2026, 11, 8), datetime(2026, 11, 15), tail)
    assert len(text) <= nw.DESCRIPTION_LIMIT
    assert text.endswith("more\n\n" + tail)


# --- upgrades: better files of what the library had ------------------------------------

def test_upgrades_are_grouped_apart_and_after_everything_new():
    films = [film("Dune", 1, year=2021), film("Sinners", 2, year=2025)]
    new = [series("rebuilt", "Rebuilt Show", 1)]
    episodes = ([ep("pitt", "The Pitt", 1, n) for n in range(1, 16)]
                + [ep("bear", "The Bear", 4, n) for n in range(1, 5)]
                + [ep("rebuilt", "Rebuilt Show", 1, n) for n in range(1, 3)])
    upgraded = ({"film-Dune"} | {f"pitt-1-{n}" for n in range(1, 16)} | {"bear-4-1", "bear-4-2", "bear-4-3"}
                | {"rebuilt-1-1", "rebuilt-1-2"})
    entries = nw.collect(films, new, episodes, upgraded)
    assert [(e.kind, e.name, len(e.episodes), len(e.upgrades)) for e in entries] == [
        ("film", "Sinners", 0, 0),
        ("episodes", "The Bear", 1, 3),             # new and upgraded: the new one counts
        ("upgraded_film", "Dune", 0, 0),
        ("upgraded_show", "The Pitt", 0, 15),
        ("upgraded_show", "Rebuilt Show", 0, 2),    # a new series of known episodes is no new show
    ]
    assert nw.has_new(entries) and not nw.has_new(entries[2:])
    assert nw.collect(films, new, episodes) == nw.collect(films, new, episodes, set())


def test_upgrade_badges_and_lines():
    pitt = nw.Entry("upgraded_show", "id-pitt", "The Pitt", upgrades=[ep("p", "The Pitt", 1, n) for n in range(1, 16)])
    assert nw.caption(pitt) == "+15 upgraded" and nw.second_caption(pitt) is None
    assert nw.line(pitt, None) == "**The Pitt** · 15 episodes · S01E01–E15"
    dune = nw.Entry("upgraded_film", "id-dune", "Dune", 2021)
    assert nw.caption(dune) == "Upgraded" and nw.line(dune, None) == "**Dune (2021)**"

    bear = entry("episodes", "The Bear", 1, season=4)
    bear.upgrades = [ep("s", "The Bear", 4, n) for n in range(2, 5)]
    assert nw.caption(bear) == "+1 episode" and nw.second_caption(bear) == "+3 upgraded"
    assert nw.line(bear, None) == "**The Bear** · 1 new episode · S04E01 · 3 upgraded"
    assert nw.second_caption(entry("episodes", "Fallout", 2)) is None


def test_counts_and_description_with_upgrades():
    pitt = nw.Entry("upgraded_show", "id-pitt", "The Pitt", upgrades=[ep("p", "The Pitt", 1, n) for n in range(1, 16)])
    bear = entry("episodes", "The Bear", 1)
    entries = [entry("film", "Sinners", year=2025), entry("show", "Invincible", 32), bear, pitt]
    assert nw.counts(entries) == "1 film · 1 new show · new episodes of 1 show · upgrades for 1 show"
    bear.upgrades = [ep("s", "The Bear", 1, 2)]
    dune = nw.Entry("upgraded_film", "id-dune", "Dune", 2021)
    assert nw.counts(entries + [dune]) == \
        "1 film · 1 new show · new episodes of 1 show · upgrades for 1 film and 2 shows"
    assert nw.counts([dune, dune]) == "upgrades for 2 films"

    text = nw.description([entry("film", "Sinners"), pitt, dune], lambda i: None,
                          datetime(2026, 11, 20), datetime(2026, 11, 27))
    assert text == ("Added Nov 20 – Nov 27\n\n**🎬 New films**\n**Sinners**\n\n"
                    "**⬆️ Upgraded to a better copy**\n**The Pitt** · 15 episodes · S01E01–E15\n**Dune (2021)**")


def test_upgrades_take_at_most_three_posters_and_never_push_out_something_new():
    new = [entry("film", f"new{n}") for n in range(4)]
    ups = [nw.Entry("upgraded_film", f"id-up{n}", f"up{n}") for n in range(5)]
    shown, more = nw.tiles(new + ups, max_tiles=12)
    assert shown == new + ups[:3] and more == 2
    shown, more = nw.tiles(new + ups[:3], max_tiles=12)
    assert shown == new + ups[:3] and more == 0
    many = [entry("film", f"new{n}") for n in range(12)]
    shown, more = nw.tiles(many + ups, max_tiles=12)
    assert shown == many[:11] and more == 1 + 5


def test_render_grid_draws_the_blue_badges():
    pitt = nw.Entry("upgraded_show", "id-pitt", "The Pitt", upgrades=[ep("p", "The Pitt", 1, 1)])
    bear = entry("episodes", "The Bear", 1)
    bear.upgrades = [ep("s", "The Bear", 1, 2)]
    img = Image.open(io.BytesIO(nw.render_grid([bear, pitt], {}, datetime(2026, 11, 20), datetime(2026, 11, 27))))
    _, _, pos = nw.grid_layout(2)
    (bx, by), (px, py) = pos
    assert img.getpixel((bx + 13, by + 29)) == nw.GOLD           # the main badge
    assert img.getpixel((bx + 13, by + 69)) == nw.BLUE           # "+1 upgraded" under it
    assert img.getpixel((px + 13, py + 29)) == nw.BLUE
