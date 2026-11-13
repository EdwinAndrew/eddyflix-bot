#!/usr/bin/env python3
"""Checks against the live Jellyfin and the bot's database. Read-only.

Run after every deploy, inside the container:

    docker exec jellyfin-discord-bot python selftest.py

It prints one line per check and exits 1 if any failed. /admin selftest runs the
same checks, then the ones that need Discord (cogs/selftest.py).
"""
import asyncio
import sqlite3
import sys
from dataclasses import dataclass, field

import config
import new_episodes
from cogs.movie_night import film_label
from cogs.watch import clock, episode_label

TABLES = ("watch_history", "user_links", "subscriptions", "movie_nights",
          "movie_night_attendance", "bot_state", "known_episodes", "subscription_offers")


@dataclass
class Check:
    name: str
    ok: bool | None          # None: skipped, with the reason in detail
    detail: str = ""

    @property
    def line(self) -> str:
        icon = {True: "✅", False: "❌", None: "➖"}[self.ok]
        return f"{icon} {self.name}" + (f": {self.detail}" if self.detail else "")


@dataclass
class Findings:
    checks: list[Check] = field(default_factory=list)
    sample_episodes: list[dict] = field(default_factory=list)   # one show's newest episodes
    poster: bytes | None = None                                  # that show's poster

    def add(self, name: str, ok: bool | None, detail: str = "") -> bool | None:
        self.checks.append(Check(name, ok, detail))
        return ok

    @property
    def failed(self) -> bool:
        return any(c.ok is False for c in self.checks)


async def jellyfin_checks(jf, findings: Findings | None = None) -> Findings:
    """Everything the bot asks Jellyfin, asked once. A failing check never stops
    the others, except that nothing works without the first."""
    f = findings or Findings()
    try:
        users = await jf.get_users()
        f.add("Jellyfin answers with the API key", bool(users), f"{len(users)} users")
    except Exception as e:
        f.add("Jellyfin answers with the API key", False, str(e))
        return f

    f.add("Public link for buttons", bool(config.JELLYFIN_PUBLIC_URL),
          config.JELLYFIN_PUBLIC_URL or "JELLYFIN_PUBLIC_URL is empty: no Open in Jellyfin links")

    try:
        found = await jf.search_titles("", limit=50)
        types = {i.get("Type") for i in found}
        f.add("Search lists films and shows", bool(found) and types <= {"Movie", "Series"},
              f"{len(found)} titles, types {', '.join(sorted(t or '?' for t in types))}")
    except Exception as e:
        f.add("Search lists films and shows", False, str(e))

    await _progress_checks(jf, users, f)

    try:
        page = await jf.episodes_by_date_added(0, 50)
        dates = [new_episodes.parse_date(e.get("DateCreated")) for e in page]
        ordered = all(a and b and a >= b for a, b in zip(dates, dates[1:]))
        if page and ordered:
            newest = page[0]
            f.sample_episodes = sorted((e for e in page if e.get("SeriesId") == newest.get("SeriesId")),
                                       key=lambda e: (e.get("ParentIndexNumber") or 0, e.get("IndexNumber") or 0))
            f.add("New-episode poll query", True,
                  f"newest is {newest.get('SeriesName')} {new_episodes.episode_code(newest)}, "
                  f"added {dates[0]:%Y-%m-%d}")
        else:
            f.add("New-episode poll query", False, "no episodes" if not page else "not newest first")
    except Exception as e:
        f.add("New-episode poll query", False, str(e))

    series_id = f.sample_episodes[0].get("SeriesId") if f.sample_episodes else None
    if series_id:
        try:
            f.poster = await jf.get_image(series_id, "Primary", max_width=300)
            f.add("Posters download", bool(f.poster), f"{len(f.poster or b'') // 1024} KB")
        except Exception as e:
            f.add("Posters download", False, str(e))
    return f


async def _progress_checks(jf, users: list[dict], f: Findings):
    """Where someone left off (/watch): a half-watched item, and Next Up for one show."""
    try:
        for user in users:
            resume = await jf.resume_items(user["Id"], limit=1)
            if not resume:
                continue
            item = await jf.get_item(resume[0]["Id"], user["Id"])
            position = (item.get("UserData") or {}).get("PlaybackPositionTicks") or 0
            label = (f"{item.get('SeriesName')} {episode_label(item)}" if item.get("Type") == "Episode"
                     else film_label(item))
            f.add("A user's progress (/watch)", position > 0, f"{label} stopped at {clock(position)}")
            break
        else:
            f.add("A user's progress (/watch)", None, "nobody has anything half-watched")
    except Exception as e:
        f.add("A user's progress (/watch)", False, str(e))

    try:
        for user in users:
            shows = await jf.next_up_items(user["Id"], limit=1)
            if not shows:
                continue
            show = shows[0]
            again = await jf.next_up(user["Id"], show["SeriesId"])
            same = bool(again) and again.get("SeriesId") == show["SeriesId"]
            f.add("Next Up for one show (/watch)", same,
                  f"{show.get('SeriesName')}: {episode_label(again or show)}")
            break
        else:
            f.add("Next Up for one show (/watch)", None, "nobody is part-way through a show")
    except Exception as e:
        f.add("Next Up for one show (/watch)", False, str(e))


def database_checks(path: str | None = None, findings: Findings | None = None) -> Findings:
    """The tables exist and the new-episode poll has its starting point. Opened read-only."""
    f = findings or Findings()
    try:
        with sqlite3.connect(f"file:{path or config.DB_PATH}?mode=ro", uri=True) as db:
            have = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            missing = [t for t in TABLES if t not in have]
            f.add("Database tables", not missing, f"missing {', '.join(missing)}" if missing else f"{len(TABLES)} present")
            if "watch_history" in have:
                columns = {r[1] for r in db.execute("PRAGMA table_info(watch_history)")}
                f.add("Transcode reasons are recorded", "transcode_reasons" in columns,
                      "column present" if "transcode_reasons" in columns else "no transcode_reasons column")
            row = db.execute("SELECT value FROM bot_state WHERE key = ?", (new_episodes.MARK_KEY,)).fetchone() \
                if "bot_state" in have else None
            f.add("New-episode poll has run", bool(row), f"seen up to {row[0][:16]}" if row else "no mark yet")
    except Exception as e:
        f.add("Database tables", False, str(e))
    return f


async def main() -> int:
    from jellyfin_client import JellyfinClient
    if not config.JELLYFIN_API_KEY:
        print("❌ JELLYFIN_API_KEY is not set")
        return 1
    jf = JellyfinClient(config.JELLYFIN_URL, config.JELLYFIN_API_KEY)
    try:
        f = await jellyfin_checks(jf)
    finally:
        await jf.close()
    database_checks(findings=f)
    for check in f.checks:
        print(check.line)
    print("FAILED" if f.failed else "All good")
    return 1 if f.failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
