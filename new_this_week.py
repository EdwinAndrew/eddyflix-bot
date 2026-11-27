"""The weekly "New on EddyFlix" post: what was added in the last 7 days.

New films, new shows, and shows with new episodes grouped into one line each,
with a poster grid drawn here and uploaded (Jellyfin's public address answers
Canada only, so Discord cannot fetch posters from it).

Sonarr and Radarr replace files with better ones, which Jellyfin lists as newly
added. Those upgrades (library_seen.py tells them apart) get a blue "Upgraded"
badge and a section of their own, after everything new, and are not counted as
new films or episodes. A show with new and upgraded episodes shows the new ones,
with the upgrades on a second, smaller badge. A week with only upgrades is not
posted.

Jellyfin 12 ignores MinDateCreated on /Items, so added_since pages newest first
and stops once it is past the cutoff. Everything else here is pure, for the tests.
"""
import io
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from PIL import Image, ImageDraw, ImageFont, ImageOps

import tiers
from new_episodes import episode_key, episode_range, normalize, parse_date

WINDOW = timedelta(days=7)
PAGE = 100
MAX_PAGES = 50          # 5,000 items: a rebuilt library should not page forever
MAX_TILES = 12          # posters in the grid; past that the last tile says "+N more"
MAX_COLS = 6
MIN_COLS = 3            # the canvas is never narrower than this, so the heading fits
MAX_UPGRADE_TILES = 3   # upgrades come after everything new, and take at most this many posters
MAX_RANGE = 30          # longer episode ranges are left out of the line
DESCRIPTION_LIMIT = 3900

COLOUR = 0xF4C430


NEW_KINDS = ("film", "show", "episodes")
UPGRADE_KINDS = ("upgraded_film", "upgraded_show")


@dataclass
class Entry:
    # "film", "show" (new this week) or "episodes" (new episodes of an older show);
    # "upgraded_film" or "upgraded_show" when all it got this week was better files
    kind: str
    item_id: str                # the film or the series: poster and link
    name: str
    year: int | None = None
    added: datetime | None = None
    episodes: list = field(default_factory=list)    # new episodes
    upgrades: list = field(default_factory=list)    # upgraded episodes

    @property
    def count(self) -> int:
        """New episodes, or for a show with only upgrades, upgraded ones."""
        return len(self.upgrades) if self.kind == "upgraded_show" else len(self.episodes)

    @property
    def new(self) -> bool:
        return self.kind in NEW_KINDS

    @property
    def label(self) -> str:
        """The name with its year, unless the name carries it already: "Yellowstone (2018)"."""
        year = self.year
        return f"{self.name} ({year})" if year and not self.name.endswith(f"({year})") else self.name


# --- fetching -------------------------------------------------------------------

def cutoff(now: datetime) -> datetime:
    return now - WINDOW


def added_after(item: dict, cut: datetime) -> bool:
    added = parse_date(item.get("DateCreated"))
    return added is not None and added > cut


async def added_since(fetch, cut: datetime, page: int = PAGE) -> list[dict]:
    """Items added after cut. fetch(start, limit) gives one page, newest first."""
    found = []
    for n in range(MAX_PAGES):
        batch = await fetch(n * page, page)
        found += [i for i in batch if added_after(i, cut)]
        if len(batch) < page:
            break
        oldest = parse_date(batch[-1].get("DateCreated"))
        if oldest is None or oldest <= cut:
            break
    return found


# --- grouping -------------------------------------------------------------------

def collect(films: list[dict], series: list[dict], episodes: list[dict],
            upgraded: set[str] | frozenset = frozenset()) -> list[Entry]:
    """Films, new shows, shows with new episodes, then upgrades: upgraded films,
    and shows that only got upgraded episodes. All three lists hold only what
    was added in the window; upgraded has the ids of films and episodes that
    are better copies of ones the library had before it (library_seen.upgrades).

    A show is new when its series was added in the window and it has a new
    episode. A new series with no episodes yet (still downloading) is left out.
    """
    films = [f for f in films if f.get("LocationType") != "Virtual"]
    entries = [Entry("upgraded_film" if f["Id"] in upgraded else "film", f["Id"], f.get("Name") or "Unknown",
                     f.get("ProductionYear"), parse_date(f.get("DateCreated")))
               for f in films]
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    entries.sort(key=lambda e: e.added or oldest, reverse=True)

    new_series = {s["Id"]: s for s in series}
    shows: dict[str, Entry] = {}
    seen = set()
    for ep in episodes:
        if ep.get("LocationType") == "Virtual":
            continue
        key = episode_key(ep)
        if key in seen:              # two files of one episode count once
            continue
        seen.add(key)
        series_id = ep.get("SeriesId")
        group = series_id or normalize(ep.get("SeriesName"))
        if group not in shows:
            info = new_series.get(series_id, {})
            shows[group] = Entry("show" if info else "episodes", series_id or "", ep.get("SeriesName") or "Unknown",
                                 info.get("ProductionYear"), parse_date(info.get("DateCreated")))
        (shows[group].upgrades if ep.get("Id") in upgraded else shows[group].episodes).append(ep)
    for show in shows.values():
        if not show.episodes:
            show.kind = "upgraded_show"

    def most_first(e: Entry):
        return (-e.count, normalize(e.name))
    new_films = [e for e in entries if e.kind == "film"]
    upgraded_films = [e for e in entries if e.kind == "upgraded_film"]
    return (new_films
            + sorted((e for e in shows.values() if e.kind == "show"), key=most_first)
            + sorted((e for e in shows.values() if e.kind == "episodes"), key=most_first)
            + upgraded_films
            + sorted((e for e in shows.values() if e.kind == "upgraded_show"), key=most_first))


def has_new(entries: list[Entry]) -> bool:
    """Whether there is anything new at all: a week of upgrades only is not posted."""
    return any(e.new for e in entries)


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def caption(entry: Entry) -> str:
    """The badge on the poster."""
    if entry.kind == "film":
        return "New film"
    if entry.kind == "show":
        return "New show"
    if entry.kind == "upgraded_film":
        return "Upgraded"
    if entry.kind == "upgraded_show":
        return f"+{entry.count} upgraded"
    return f"+{plural(entry.count, 'episode')}"


def second_caption(entry: Entry) -> str | None:
    """The smaller badge under it, for a show with new and upgraded episodes."""
    return f"+{len(entry.upgrades)} upgraded" if entry.new and entry.upgrades else None


def _with_range(text: str, episodes: list) -> str:
    codes = episode_range(episodes)
    return f"{text} · {codes}" if codes and len(codes) <= MAX_RANGE else text


def line(entry: Entry, link: str | None) -> str:
    """One line of the post: "[The Bear](…) · 10 new episodes · S04E01–E10"."""
    name = f"[{entry.label}]({link})" if link else f"**{entry.label}**"
    if entry.kind in ("film", "upgraded_film"):
        return name
    if entry.kind == "upgraded_show":
        return _with_range(f"{name} · {plural(entry.count, 'episode')}", entry.upgrades)
    if entry.kind == "show":
        text = f"{name} · {plural(entry.count, 'episode')}"
    else:
        text = _with_range(f"{name} · {entry.count} new episode{'' if entry.count == 1 else 's'}", entry.episodes)
    return f"{text} · {len(entry.upgrades)} upgraded" if entry.upgrades else text


SECTIONS = [(("film",), "🎬 New films"), (("show",), "📺 New shows"), (("episodes",), "➕ New episodes"),
            (UPGRADE_KINDS, "⬆️ Upgraded to a better copy")]


def request_line(command_on: bool, seerr_url: str) -> str | None:
    """The last line, pointing to requests: /request when the bot can reach Seerr,
    a link to Seerr when its public address is set, None when neither."""
    if command_on and seerr_url:
        return f"🙋 Missing something? Ask for it with `/request`, or on [Seerr]({seerr_url})."
    if command_on:
        return "🙋 Missing something? Ask for it with `/request`."
    if seerr_url:
        return f"🙋 Missing something? Ask for it on [Seerr]({seerr_url})."
    return None


def description(entries: list[Entry], link_for, start: datetime, end: datetime, tail: str | None = None) -> str:
    """The embed's text: the dates, then a section per kind, then tail if given
    (the request line). link_for(item_id) gives a link or None. Cut short with
    "…and N more" past Discord's limit, always leaving room for the tail."""
    limit = DESCRIPTION_LIMIT - (len(tail) + 2 if tail else 0)
    end_with = [f"\n{tail}"] if tail else []
    out = [f"Added {start:%b} {start.day} – {end:%b} {end.day}"]
    shown = 0
    for kinds, heading in SECTIONS:
        group = [e for e in entries if e.kind in kinds]
        if not group:
            continue
        out.append(f"\n**{heading}**")
        for e in group:
            text = line(e, link_for(e.item_id) if e.item_id else None)
            if len("\n".join(out + [text])) > limit:
                if out[-1].startswith("\n**"):
                    out.pop()
                return "\n".join(out + [f"…and {len(entries) - shown} more"] + end_with)
            out.append(text)
            shown += 1
    return "\n".join(out + end_with)


def counts(entries: list[Entry]) -> str:
    """"1 film · 2 new shows · new episodes of 3 shows · upgrades for 1 show".
    Upgrades count apart: a show with new and upgraded episodes is in both."""
    n = {kind: sum(1 for e in entries if e.kind == kind) for kind in NEW_KINDS + UPGRADE_KINDS}
    upgraded_shows = n["upgraded_show"] + sum(1 for e in entries if e.new and e.upgrades)
    bits = []
    if n["film"]:
        bits.append(plural(n["film"], "film"))
    if n["show"]:
        bits.append(plural(n["show"], "new show"))
    if n["episodes"]:
        bits.append(f"new episodes of {plural(n['episodes'], 'show')}")
    upgraded = [plural(c, w) for c, w in ((n["upgraded_film"], "film"), (upgraded_shows, "show")) if c]
    if upgraded:
        bits.append(f"upgrades for {' and '.join(upgraded)}")
    return " · ".join(bits)


# --- the poster grid ----------------------------------------------------------

POSTER_W, POSTER_H = 240, 360
CAPTION_H = 56          # the title under each poster
GAP = 24
PAD = 40
HEADER_H = 170

BG = (20, 22, 27)
TILE_BG = (38, 41, 49)
TEXT = (240, 240, 240)
MUTED = (150, 155, 165)
GOLD = (244, 196, 48)
BLUE = (77, 166, 255)
BADGE_COLOURS = {"film": (231, 76, 60), "show": (46, 204, 113), "episodes": GOLD,
                 "upgraded_film": BLUE, "upgraded_show": BLUE}


def tiles(entries: list[Entry], max_tiles: int = MAX_TILES,
          max_upgrades: int = MAX_UPGRADE_TILES) -> tuple[list[Entry], int]:
    """The entries that get a poster, and how many more there are. Upgrades come
    last and get at most max_upgrades posters. With too many, the last tile is
    given to "+N more"."""
    candidates = [e for e in entries if e.new] + [e for e in entries if not e.new][:max_upgrades]
    if len(candidates) == len(entries) and len(entries) <= max_tiles:
        return entries, 0
    shown = candidates[:max_tiles - 1]
    return shown, len(entries) - len(shown)


def grid_layout(n: int, max_cols: int = MAX_COLS, min_cols: int = MIN_COLS):
    """(width, height, [(x, y), …]) for n tiles. Rows are balanced (7 is 4 and 3,
    not 6 and 1) and a short last row, or a canvas wider than the tiles, is centred."""
    rows = max(1, math.ceil(n / max_cols))
    cols = max(1, math.ceil(n / rows))
    tile_h = POSTER_H + CAPTION_H
    width = 2 * PAD + max(cols, min_cols) * POSTER_W + (max(cols, min_cols) - 1) * GAP
    height = HEADER_H + rows * tile_h + (rows - 1) * GAP + PAD
    positions = []
    for i in range(n):
        row, col = divmod(i, cols)
        in_row = min(cols, n - row * cols)
        row_w = in_row * POSTER_W + (in_row - 1) * GAP
        x = (width - row_w) // 2 + col * (POSTER_W + GAP)
        positions.append((x, HEADER_H + row * (tile_h + GAP)))
    return width, height, positions


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(tiers._font_path(), size)


def _fit(text: str, max_width: int, start: int, smallest: int = 18):
    """The largest font that fits, or the smallest one with the text cut to fit."""
    size = start
    while size > smallest:
        font = _font(size)
        if font.getlength(text) <= max_width:
            return font, text
        size -= 2
    font = _font(smallest)
    while text and font.getlength(text + "…") > max_width:
        text = text[:-1]
    return font, text.rstrip() + "…"


def _placeholder(name: str) -> Image.Image:
    img = Image.new("RGB", (POSTER_W, POSTER_H), TILE_BG)
    draw = ImageDraw.Draw(img)
    words, lines = name.upper().split(), [""]
    font = _font(40)
    for word in words:   # wrap onto as many lines as it takes
        trial = f"{lines[-1]} {word}".strip()
        if font.getlength(trial) <= POSTER_W - 30 or not lines[-1]:
            lines[-1] = trial
        else:
            lines.append(word)
    y = (POSTER_H - 44 * len(lines)) // 2
    for text in lines[:6]:
        draw.text((POSTER_W // 2, y), text, font=font, fill=MUTED, anchor="mt")
        y += 44
    return img


def _poster(data: bytes | None, name: str) -> Image.Image:
    if data:
        try:
            img = Image.open(io.BytesIO(data)).convert("RGB")
            return ImageOps.fit(img, (POSTER_W, POSTER_H), Image.LANCZOS)
        except Exception:
            pass
    return _placeholder(name)


def render_grid(entries: list[Entry], posters: dict[str, bytes | None], start: datetime, end: datetime) -> bytes:
    """The poster grid as PNG bytes. posters maps item ids to image bytes."""
    shown, more = tiles(entries)
    width, height, positions = grid_layout(len(shown) + (1 if more else 0))
    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)

    draw.text((PAD, PAD - 6), "NEW ON EDDYFLIX", font=_font(72), fill=GOLD)
    font, text = _fit(f"{start:%b} {start.day} – {end:%b} {end.day}  ·  {counts(entries)}".upper(),
                      width - 2 * PAD, 34)
    draw.text((PAD, PAD + 70), text, font=font, fill=MUTED)

    badge_font, small_font = _font(30), _font(24)
    for entry, (x, y) in zip(shown, positions):
        img.paste(_poster(posters.get(entry.item_id), entry.name), (x, y))
        text = caption(entry).upper()
        w = int(badge_font.getlength(text)) + 20
        colour = BADGE_COLOURS[entry.kind]
        draw.rounded_rectangle((x + 10, y + 10, x + 10 + w, y + 48), radius=8, fill=colour)
        draw.text((x + 20, y + 29), text, font=badge_font, fill=BG, anchor="lm")
        if extra := second_caption(entry):
            text = extra.upper()
            w = int(small_font.getlength(text)) + 16
            draw.rounded_rectangle((x + 10, y + 54, x + 10 + w, y + 84), radius=7, fill=BLUE)
            draw.text((x + 18, y + 69), text, font=small_font, fill=BG, anchor="lm")
        font, title = _fit(entry.name.upper(), POSTER_W, 40)
        draw.text((x + POSTER_W // 2, y + POSTER_H + 10), title, font=font, fill=TEXT, anchor="mt")
    if more:
        x, y = positions[-1]
        draw.rounded_rectangle((x, y, x + POSTER_W, y + POSTER_H), radius=12, fill=TILE_BG)
        draw.text((x + POSTER_W // 2, y + POSTER_H // 2), f"+{more}", font=_font(110), fill=GOLD, anchor="mm")
        draw.text((x + POSTER_W // 2, y + POSTER_H // 2 + 70), "MORE", font=_font(40), fill=MUTED, anchor="mt")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
