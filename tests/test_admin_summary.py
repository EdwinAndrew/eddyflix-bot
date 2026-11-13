"""The weekly admin summary, transcode reasons, and the transcode warning."""
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import aiosqlite
import pytest

import admin_summary as summary
import config
import watch_data
from cogs.activity import Activity

T0 = datetime(2026, 11, 9, 20, 0)


def play(user="eddy", device="Chrome", method="DirectPlay", reasons=None, start=0, minutes=30):
    begin = T0 + timedelta(minutes=start)
    end = begin + timedelta(minutes=minutes) if minutes is not None else None
    return {"user": user, "device": device, "play_method": method, "reasons": reasons,
            "start": begin, "end": end, "seconds": minutes * 60 if minutes is not None else None}


# --- reasons ----------------------------------------------------------------------

def test_reasons_from_a_session():
    session = {"TranscodingInfo": {"TranscodeReasons": ["SubtitleCodecNotSupported", "AudioCodecNotSupported"]}}
    assert summary.transcode_reasons(session) == ["SubtitleCodecNotSupported", "AudioCodecNotSupported"]
    # Older Jellyfin sends one string
    assert summary.transcode_reasons({"TranscodingInfo": {"TranscodeReasons": "ContainerNotSupported, VideoCodecNotSupported"}}) \
        == ["ContainerNotSupported", "VideoCodecNotSupported"]
    assert summary.transcode_reasons({"TranscodingInfo": None}) == []
    assert summary.transcode_reasons({}) == []


def test_reason_names():
    assert summary.reason_name("SubtitleCodecNotSupported") == "subtitles"
    assert summary.reason_name("ContainerBitrateExceedsLimit") == "bitrate"
    assert summary.reason_name("SomethingNewInJellyfin") == "something new in jellyfin"


# --- sections -----------------------------------------------------------------------

def test_playback_by_device_with_reasons():
    plays = [
        play(device="Chrome", method="Transcode", reasons="SubtitleCodecNotSupported", start=0),
        play(device="Chrome", method="Transcode", reasons="SubtitleCodecNotSupported,AudioCodecNotSupported", start=10),
        play(device="Chrome", start=100),
        play(device="iPhone", method="Transcode", reasons=None, start=200),   # from before reasons were kept
        play(device="LG Smart TV", start=300, minutes=2),
        play(device="LG Smart TV", start=400),
    ]
    text = summary.playback_section(plays)
    lines = text.split("\n")
    assert lines[0] == "**6 plays**, 3 transcoded (50%) · 1 under 5 minutes"
    assert lines[1] == "Most transcodes at once: 2 (Mon 9 Nov, 8:10 PM)"
    assert lines[2] == "**Chrome**: 2 of 3 transcoded · subtitles 2, audio codec 1"
    assert lines[3] == "**iPhone**: 1 of 1 transcoded · reason not recorded 1"
    assert lines[4] == "**LG Smart TV**: 0 of 2 transcoded"


def test_playback_when_nothing_played():
    assert summary.playback_section([]) == "Nothing played this week."


def test_peak_overlap():
    t = lambda m: T0 + timedelta(minutes=m)
    assert summary.peak_overlap([]) == (0, None)
    assert summary.peak_overlap([(t(0), t(10)), (t(10), t(20))]) == (1, t(0))   # back to back is not at once
    assert summary.peak_overlap([(t(0), t(30)), (t(5), t(10)), (t(8), t(40))]) == (3, t(8))


def test_library_section():
    films = [{"Name": "The Odyssey", "ProductionYear": 2026}, {"Name": "Untitled"}]
    episodes = [{"SeriesName": "Curb Your Enthusiasm"}] * 10 + [{"SeriesName": "Fallout"}] * 2
    assert summary.library_section(films, episodes) == (
        "**2 films:** The Odyssey (2026), Untitled\n"
        "**12 episodes** of 2 shows: Curb Your Enthusiasm 10, Fallout 2")
    assert summary.library_section([], []) == "Nothing added this week."
    assert summary.library_section(None, []) == "Jellyfin did not answer."


def test_people_section():
    plays = [play(user="eddy", minutes=120), play(user="Sarah", minutes=30), play(user="eddy", minutes=None)]
    text = summary.people_section(plays, ["Aldrin", "eddy", "Julie", "Sarah"], {"EDDY", "Sarah"})
    assert text == "**Active (2):** eddy 2.0h, Sarah 0.5h\n**Not linked with /jf-link (2):** Aldrin, Julie"
    assert "Jellyfin did not answer" in summary.people_section([], None, set())


STORAGE = {
    "ProgramDataFolder": {"Path": "/config", "FreeSpace": 4_000_000_000, "UsedSpace": 46_000_000_000},
    "TranscodingTempFolder": {"Path": "/cache/transcodes", "FreeSpace": 4_000_000_000, "UsedSpace": 46_000_000_000},
    "Libraries": [
        {"Name": "Shows", "Folders": [{"Path": "/media/tv", "FreeSpace": 12_300_000_000_000, "UsedSpace": 2_400_000_000_000}]},
        {"Name": "Movies", "Folders": [{"Path": "/media/movies", "FreeSpace": 12_300_000_000_000, "UsedSpace": 2_400_000_000_000}]},
        {"Name": "Collections", "Folders": [{"Path": "/config/data/collections", "FreeSpace": 4_000_000_000, "UsedSpace": 46_000_000_000}]},
    ],
}


def test_disks_are_merged_by_space():
    assert summary.disks(STORAGE) == [
        ("Shows, Movies", 12_300_000_000_000, 14_700_000_000_000),
        ("Collections, Jellyfin data, transcodes", 4_000_000_000, 50_000_000_000),
    ]


def test_health_section():
    health = {"bot_started": T0 - timedelta(days=2, hours=3), "jellyfin_outages": 1,
              "system": {"Version": "12.4.0", "HasPendingRestart": True}, "storage": STORAGE, "open_plays": 71}
    assert summary.health_section(health, T0).split("\n") == [
        "Bot running since Sat 7 Nov, 5:00 PM (2d 3h)",
        "Jellyfin stopped answering 1 time since then",
        "Jellyfin 12.4.0 · restart pending",
        "Disk (Shows, Movies): 12.3 TB free of 14.7 TB",
        "Disk (Collections, Jellyfin data, transcodes): 4.0 GB free of 50.0 GB ⚠️",
        "71 plays never closed in the database (/jf-debug-sessions)",
    ]
    assert "Jellyfin did not answer just now" in summary.health_section({}, T0)


def test_long_fields_are_cut_at_a_line():
    text = "\n".join(f"line {n}" for n in range(500))
    clipped = summary.clip(text)
    assert len(clipped) <= summary.FIELD_LIMIT and clipped.endswith("\n…")


def test_added_since_pages():
    since = datetime(2026, 11, 5, tzinfo=timezone.utc)
    page = [{"Name": "new", "DateCreated": "2026-11-08T06:03:04.9192985Z"},
            {"Name": "old", "DateCreated": "2026-10-31T09:44:09.3550144Z"}]
    newer, done = summary.added_since(page, since)
    assert [i["Name"] for i in newer] == ["new"] and done
    assert summary.added_since(page[:1], since) == (page[:1], False)


def test_summary_embed():
    embed = summary.summary_embed([("▶️ Playback", "x"), ("📚 Library", "")], T0 - timedelta(days=7), T0)
    assert embed.description == "Nov 2 – Nov 9"
    assert [f.name for f in embed.fields] == ["▶️ Playback", "📚 Library"]
    assert "Sonarr" in embed.footer.text


# --- the database -------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "bot.db")
    monkeypatch.setattr(config, "DB_PATH", path)
    return path


def test_the_reasons_column_is_added_to_an_old_database(db_path):
    with sqlite3.connect(db_path) as db:
        db.execute("""CREATE TABLE watch_history (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            user_name TEXT NOT NULL, item_id TEXT NOT NULL, item_name TEXT NOT NULL, item_type TEXT NOT NULL,
            series_name TEXT, season_number INTEGER, episode_number INTEGER, start_time TIMESTAMP NOT NULL,
            end_time TIMESTAMP, duration_seconds INTEGER, play_method TEXT, device_name TEXT)""")
        db.execute("INSERT INTO watch_history (session_id, user_name, item_id, item_name, item_type, start_time) "
                   "VALUES ('s', 'eddy', 'i', 'Film', 'Movie', '2026-11-01 20:00:00')")
    asyncio.run(watch_data.init_database())
    asyncio.run(watch_data.init_database())          # and running it again is fine
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT user_name, transcode_reasons FROM watch_history").fetchall() == [("eddy", None)]


def test_plays_since_reads_reasons(db_path):
    asyncio.run(watch_data.init_database())
    session = {"UserName": "eddy", "DeviceName": "Chrome", "PlayState": {"PlayMethod": "DirectPlay"},
               "NowPlayingItem": {"Id": "f1", "Name": "1917", "Type": "Movie"}}
    asyncio.run(watch_data.record_watch_start("s1", session))
    asyncio.run(watch_data.record_transcode("s1", "f1", "Transcode", ["SubtitleCodecNotSupported"]))

    async def read():
        async with aiosqlite.connect(db_path) as db:
            return await summary.plays_since(db, datetime.now() - timedelta(days=7))
    [row] = asyncio.run(read())
    assert (row["user"], row["device"], row["play_method"], row["reasons"], row["end"]) == \
        ("eddy", "Chrome", "Transcode", "SubtitleCodecNotSupported", None)


# --- the activity cog: recording reasons and the warning -----------------------------

class Channel:
    def __init__(self):
        self.sent = []

    async def send(self, embed=None, files=None):
        self.sent.append(embed)
        return SimpleNamespace(attachments=[], edit=None)


class Jellyfin:
    def __init__(self):
        self.sessions = []

    async def get_sessions(self):
        return self.sessions

    async def get_image(self, *args, **kwargs):
        return None


def transcoding(sid, user, item_id, reasons=("SubtitleCodecNotSupported",)):
    return {"Id": sid, "UserName": user, "DeviceName": "Chrome",
            "NowPlayingItem": {"Id": item_id, "Name": item_id, "Type": "Movie"},
            "PlayState": {"PlayMethod": "Transcode", "PositionTicks": 0},
            "TranscodingInfo": {"TranscodeReasons": list(reasons)}}


@pytest.fixture
def cog(db_path, monkeypatch):
    asyncio.run(watch_data.init_database())
    monkeypatch.setattr(config, "ACTIVITY_CHANNEL_ID", 1)
    channels = {1: Channel(), 2: Channel()}
    jf = Jellyfin()
    activity = Activity(SimpleNamespace(jf=jf, get_channel=lambda i: channels.get(i)))

    def poll(*sessions):
        jf.sessions = list(sessions)
        asyncio.run(activity.check_sessions.coro(activity))
    return poll, channels


def test_reasons_are_recorded_with_the_play(cog, db_path):
    poll, _ = cog
    poll()
    poll(transcoding("a", "eddy", "f1"))
    poll(transcoding("a", "eddy", "f1"))
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT play_method, transcode_reasons FROM watch_history").fetchall() == \
            [("Transcode", "SubtitleCodecNotSupported")]


def test_the_warning_goes_to_the_admin_channel_once(cog, monkeypatch):
    poll, channels = cog
    monkeypatch.setattr(config, "ADMIN_CHANNEL_ID", 2)
    three = [transcoding("a", "eddy", "f1"), transcoding("b", "Sarah", "f2"), transcoding("c", "Kevin", "f3")]
    poll()
    poll(*three)
    poll(*three)
    assert [e.title for e in channels[2].sent] == ["⚠️ High Transcode Load"]
    assert "**Kevin** on Chrome: subtitles" in channels[2].sent[0].fields[0].value
    assert channels[1].sent == []
    poll(*three[:2])
    poll(*three)                                       # dropped below, then back up: warns again
    assert len(channels[2].sent) == 2


def test_without_an_admin_channel_the_warning_stays_in_the_feed(cog, monkeypatch):
    poll, channels = cog
    monkeypatch.setattr(config, "ADMIN_CHANNEL_ID", 0)
    poll()
    poll(transcoding("a", "eddy", "f1"), transcoding("b", "Sarah", "f2"), transcoding("c", "Kevin", "f3"))
    assert [e.title for e in channels[1].sent] == ["⚠️ High Transcode Load"]


# --- the admin cog, end to end -----------------------------------------------------

class SummaryJellyfin:
    def __init__(self, fail=False):
        self.fail = fail
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        self.films = [{"Name": "The Odyssey", "ProductionYear": 2026, "DateCreated": recent},
                      {"Name": "Gremlins", "DateCreated": old}]
        self.episodes = [{"SeriesName": "Fallout", "DateCreated": recent}]

    async def movies_by_date_added(self, start=0, limit=50):
        if self.fail:
            raise TimeoutError("Jellyfin did not answer")
        return self.films[start:start + limit]

    async def episodes_by_date_added(self, start=0, limit=100):
        return self.episodes[start:start + limit]

    async def get_user_names(self):
        return ["eddy", "Kevin"]

    async def get_system_info(self):
        return {"Version": "12.4.0"}

    async def get_storage(self):
        return STORAGE


def test_the_admin_cog_puts_it_together(db_path):
    from cogs.admin import Admin
    asyncio.run(watch_data.init_database())
    health = SimpleNamespace(started=datetime.now() - timedelta(hours=5), jellyfin_outages=0)
    bot = SimpleNamespace(jf=SummaryJellyfin(), get_cog=lambda name: health if name == "Health" else None)
    embed = asyncio.run(Admin(bot).build())
    fields = {f.name: f.value for f in embed.fields}
    assert fields["▶️ Playback"] == "Nothing played this week."
    assert fields["📚 Library"] == "**1 film:** The Odyssey (2026)\n**1 episode** of 1 show: Fallout 1"
    assert "**Not linked with /jf-link (2):** eddy, Kevin" in fields["👥 People"]
    assert "Jellyfin 12.4.0" in fields["🩺 Health"]

    bot.jf = SummaryJellyfin(fail=True)
    embed = asyncio.run(Admin(bot).build())
    assert {f.name: f.value for f in embed.fields}["📚 Library"] == "Jellyfin did not answer."
