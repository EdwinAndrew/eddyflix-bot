"""The weekly admin summary (#51), and why Jellyfin transcodes.

For the server owner, in ADMIN_CHANNEL_ID: how much is transcoded and why, by
device; what was added to the library; who watched and who has not used
/jf-link; how the bot and Jellyfin are doing. All of it comes from the bot's own
database and Jellyfin. Sonarr, Radarr and Seerr need API keys the bot does not
have yet; each would be one more section (see Admin.sections in cogs/admin.py).

Everything above the database helpers is pure, for the tests.
"""
import re
from collections import Counter
from datetime import datetime, timedelta

import discord

from new_episodes import parse_date
from viewings import SHORT_PLAY

COLOUR = 0x5865F2
FIELD_LIMIT = 1024        # Discord's limit for one embed field
MAX_DEVICES = 6
MAX_NAMES = 12
LOW_DISK = 0.10           # flag a disk with less than this share free

# Jellyfin's TranscodeReasons, in words. Anything else is split from its CamelCase.
REASONS = {
    "ContainerNotSupported": "container",
    "VideoCodecNotSupported": "video codec",
    "AudioCodecNotSupported": "audio codec",
    "SubtitleCodecNotSupported": "subtitles",
    "AudioIsExternal": "external audio",
    "SecondaryAudioNotSupported": "second audio track",
    "VideoProfileNotSupported": "video profile",
    "VideoLevelNotSupported": "video level",
    "VideoResolutionNotSupported": "resolution",
    "VideoBitDepthNotSupported": "bit depth",
    "VideoFramerateNotSupported": "frame rate",
    "VideoRangeTypeNotSupported": "HDR",
    "VideoCodecTagNotSupported": "codec tag",
    "AudioChannelsNotSupported": "audio channels",
    "AudioProfileNotSupported": "audio profile",
    "AudioSampleRateNotSupported": "audio sample rate",
    "AudioBitDepthNotSupported": "audio bit depth",
    "ContainerBitrateExceedsLimit": "bitrate",
    "VideoBitrateNotSupported": "video bitrate",
    "AudioBitrateNotSupported": "audio bitrate",
    "DirectPlayError": "direct play failed",
}


# --- transcode reasons ------------------------------------------------------------

def transcode_reasons(session: dict) -> list[str]:
    """TranscodingInfo.TranscodeReasons from a Jellyfin session: a list in
    Jellyfin 10.9 and later, a comma-separated string before. [] if none."""
    reasons = (session.get('TranscodingInfo') or {}).get('TranscodeReasons') or []
    if isinstance(reasons, str):
        reasons = reasons.split(',')
    return [r.strip() for r in reasons if r and r.strip()]


def reason_name(reason: str) -> str:
    return REASONS.get(reason) or re.sub(r"(?<!^)(?=[A-Z])", " ", reason).lower()


def transcode_alert_lines(sessions) -> list[str]:
    """One line per transcoding session for the real-time warning."""
    lines = []
    for s in sessions:
        reasons = ", ".join(reason_name(r) for r in transcode_reasons(s)) or "reason not given"
        lines.append(f"• **{s.get('UserName', 'Unknown')}** on {s.get('DeviceName', 'Unknown')}: {reasons}")
    return lines


# --- sections -----------------------------------------------------------------------

def clip(text: str, limit: int = FIELD_LIMIT) -> str:
    """Cut at a line break to fit a field."""
    if len(text) <= limit:
        return text
    cut = text[:limit - 2].rsplit("\n", 1)[0]
    return cut + "\n…"


def names(items: list[str], limit: int = MAX_NAMES) -> str:
    shown = ", ".join(items[:limit])
    return shown + (f" and {len(items) - limit} more" if len(items) > limit else "")


def peak_overlap(intervals) -> tuple[int, datetime | None]:
    """The most (start, end) intervals open at the same moment, and when that first happened."""
    events = sorted([(start, 1) for start, end in intervals] + [(end, -1) for start, end in intervals],
                    key=lambda e: (e[0], e[1]))     # an end before a start at the same instant
    best, when, open_now = 0, None, 0
    for moment, step in events:
        open_now += step
        if open_now > best:
            best, when = open_now, moment
    return best, when


def playback_section(plays: list[dict]) -> str:
    """plays: rows with device, play_method, reasons ("A,B" or None), start, end, seconds."""
    if not plays:
        return "Nothing played this week."
    transcoded = [p for p in plays if p['play_method'] == 'Transcode']
    finished = [p for p in plays if p['seconds'] is not None]
    short = [p for p in finished if p['seconds'] < SHORT_PLAY.total_seconds()]
    lines = [f"**{len(plays)} plays**, {len(transcoded)} transcoded ({len(transcoded) / len(plays):.0%})"
             f" · {len(short)} under {int(SHORT_PLAY.total_seconds() // 60)} minutes"]

    peak, when = peak_overlap([(p['start'], p['end']) for p in transcoded if p['end']])
    if peak > 1:
        lines.append(f"Most transcodes at once: {peak} ({when:%a %-d %b, %-I:%M %p})")

    by_device: dict[str, list[dict]] = {}
    for p in plays:
        by_device.setdefault(p['device'] or 'Unknown', []).append(p)
    ranked = sorted(by_device.items(),
                    key=lambda d: (-sum(p['play_method'] == 'Transcode' for p in d[1]), -len(d[1]), d[0]))
    for device, group in ranked[:MAX_DEVICES]:
        moved = [p for p in group if p['play_method'] == 'Transcode']
        line = f"**{device}**: {len(moved)} of {len(group)} transcoded"
        if moved:
            reasons = Counter(reason_name(r) for p in moved for r in (p['reasons'] or '').split(',') if r)
            unknown = sum(1 for p in moved if not p['reasons'])
            parts = [f"{name} {count}" for name, count in reasons.most_common(4)]
            if unknown:
                parts.append(f"reason not recorded {unknown}")
            line += " · " + ", ".join(parts)
        lines.append(line)
    if len(ranked) > MAX_DEVICES:
        lines.append(f"…and {len(ranked) - MAX_DEVICES} more devices")
    return clip("\n".join(lines))


def library_section(films: list[dict] | None, episodes: list[dict] | None) -> str:
    """films and episodes added this week, as Jellyfin lists them. None: Jellyfin did not answer."""
    if films is None or episodes is None:
        return "Jellyfin did not answer."
    if not films and not episodes:
        return "Nothing added this week."
    lines = []
    if films:
        titles = [f"{f.get('Name', '?')} ({f['ProductionYear']})" if f.get('ProductionYear') else f.get('Name', '?')
                  for f in films]
        lines.append(f"**{len(films)} film{'s' if len(films) != 1 else ''}:** {names(titles)}")
    if episodes:
        shows = Counter(e.get('SeriesName') or 'Unknown show' for e in episodes)
        listed = [f"{show} {count}" for show, count in sorted(shows.items(), key=lambda s: (-s[1], s[0]))]
        lines.append(f"**{len(episodes)} episode{'s' if len(episodes) != 1 else ''}** of "
                     f"{len(shows)} show{'s' if len(shows) != 1 else ''}: {names(listed)}")
    return clip("\n".join(lines))


def people_section(plays: list[dict], jellyfin_users: list[str] | None, linked: set[str]) -> str:
    lines = []
    watched: dict[str, list[dict]] = {}
    for p in plays:
        watched.setdefault(p['user'], []).append(p)
    if watched:
        ranked = sorted(watched.items(), key=lambda u: (-sum(p['seconds'] or 0 for p in u[1]), u[0].lower()))
        listed = [f"{user} {sum(p['seconds'] or 0 for p in group) / 3600:.1f}h" for user, group in ranked]
        lines.append(f"**Active ({len(watched)}):** {names(listed)}")
    else:
        lines.append("**Active:** nobody")
    if jellyfin_users is None:
        lines.append("**Not linked:** Jellyfin did not answer")
    else:
        linked_lower = {u.lower() for u in linked}
        unlinked = [u for u in jellyfin_users if u.lower() not in linked_lower]
        lines.append(f"**Not linked with /jf-link ({len(unlinked)}):** {names(unlinked)}" if unlinked
                     else "**Not linked with /jf-link:** nobody")
    return clip("\n".join(lines))


def size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


def disks(storage: dict) -> list[tuple[str, int, int]]:
    """(what is on it, free bytes, total bytes) for each disk in /System/Info/Storage.

    Jellyfin lists every folder separately, and gives one filesystem different
    device ids by mount point, so folders with the same free and used space
    are taken as one disk."""
    found: dict[tuple, list[str]] = {}

    def add(folder, label):
        if folder and folder.get('FreeSpace') is not None and folder.get('UsedSpace') is not None:
            labels = found.setdefault((folder['FreeSpace'], folder['UsedSpace']), [])
            if label not in labels:
                labels.append(label)

    for library in storage.get('Libraries') or []:
        for folder in library.get('Folders') or []:
            add(folder, library.get('Name', '?'))
    add(storage.get('ProgramDataFolder'), "Jellyfin data")
    add(storage.get('TranscodingTempFolder'), "transcodes")
    return [(", ".join(labels), free, free + used) for (free, used), labels in found.items()]


def health_section(health: dict, now: datetime) -> str:
    """health: bot_started, jellyfin_outages, system (/System/Info or None),
    storage (/System/Info/Storage or None), open_plays."""
    lines = []
    if health.get('bot_started'):
        up = now - health['bot_started']
        days, hours = up.days, up.seconds // 3600
        lines.append(f"Bot running since {health['bot_started']:%a %-d %b, %-I:%M %p} ({days}d {hours}h)")
    outages = health.get('jellyfin_outages', 0)
    lines.append(f"Jellyfin stopped answering {outages} time{'s' if outages != 1 else ''} since then"
                 if outages else "Jellyfin answered throughout")
    system = health.get('system')
    if system:
        notes = [f"Jellyfin {system.get('Version', '?')}"]
        if system.get('HasPendingRestart'):
            notes.append("restart pending")
        if system.get('HasUpdateAvailable'):
            notes.append("update available")
        lines.append(" · ".join(notes))
    else:
        lines.append("Jellyfin did not answer just now")
    for label, free, total in disks(health.get('storage') or {}):
        warn = " ⚠️" if total and free / total < LOW_DISK else ""
        lines.append(f"Disk ({label}): {size(free)} free of {size(total)}{warn}")
    if health.get('open_plays'):
        lines.append(f"{health['open_plays']} plays never closed in the database (/jf-debug-sessions)")
    return clip("\n".join(lines))


def summary_embed(sections: list[tuple[str, str]], start: datetime, end: datetime) -> discord.Embed:
    embed = discord.Embed(title="🛠️ Weekly admin summary",
                          description=f"{start:%b %-d} – {end:%b %-d}", colour=COLOUR, timestamp=end)
    for name, value in sections:
        embed.add_field(name=name, value=value or "—", inline=False)
    embed.set_footer(text="Sonarr, Radarr and Seerr are not connected yet")
    return embed


def added_since(items: list[dict], since: datetime) -> tuple[list[dict], bool]:
    """Items (newest DateCreated first) added after since, and whether the page
    reached older ones, so there is no need to fetch the next."""
    newer = [i for i in items if (d := parse_date(i.get('DateCreated'))) and d >= since]
    done = any((d := parse_date(i.get('DateCreated'))) and d < since for i in items)
    return newer, done


# --- the database -----------------------------------------------------------------

async def plays_since(db, since: datetime) -> list[dict]:
    async with db.execute("""
        SELECT user_name, device_name, play_method, transcode_reasons, start_time, end_time, duration_seconds
        FROM watch_history WHERE start_time >= ?
    """, (since,)) as cursor:
        rows = await cursor.fetchall()
    return [{"user": r[0], "device": r[1], "play_method": r[2], "reasons": r[3],
             "start": datetime.fromisoformat(r[4]), "end": datetime.fromisoformat(r[5]) if r[5] else None,
             "seconds": r[6]} for r in rows]


async def linked_users(db) -> set[str]:
    async with db.execute("SELECT jellyfin_user FROM user_links") as cursor:
        return {r[0] for r in await cursor.fetchall()}


async def open_plays(db, before: datetime) -> int:
    """Plays started before then and never closed: a stop the bot missed."""
    async with db.execute("SELECT COUNT(*) FROM watch_history WHERE end_time IS NULL AND start_time < ?",
                          (before,)) as cursor:
        return (await cursor.fetchone())[0]


def week_window(now: datetime) -> tuple[datetime, datetime]:
    return now - timedelta(days=7), now
