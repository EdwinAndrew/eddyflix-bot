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
