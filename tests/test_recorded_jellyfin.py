"""/watch and the new-episode DMs against real Jellyfin responses, recorded from the
server by tests/fixtures/record_jellyfin.py (trimmed, no user names or ids).

These catch Jellyfin answering differently from what the code expects, which
hand-written test data cannot. After re-recording, update the values below.
"""
import json
from datetime import timedelta
from pathlib import Path

import pytest

import config
import new_episodes
from cogs.subscriptions import notification_embed
from cogs.watch import film_progress, series_progress, watch_card

FIXTURES = Path(__file__).parent / "fixtures" / "jellyfin"


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def fields(embed):
    return {f.name: f.value for f in embed.fields}


@pytest.fixture(autouse=True)
def no_links(monkeypatch):
    monkeypatch.setattr(config, "JELLYFIN_PUBLIC_URL", "")


def test_film_stopped_part_way():
    item = load("film_in_progress")["item"]
    shown = fields(watch_card(item, film_progress(item)))
    assert shown == {"Runtime": "1h 38m", "Genre": "Action, Comedy, Crime",
                     "Where you left off": "▶️ Stopped at **6:20** of 1:37:49"}


def test_show_with_a_half_watched_next_episode():
    rec = load("series_resume")
    embed = watch_card(rec["item"], series_progress(rec["item"], rec["next"]))
    assert embed.title == "📺 Yellowstone (2018)"          # the year is in the name already
    assert fields(embed) == {
        "Show": "5 seasons · 53 episodes · ~51m each · ended",
        "Genre": "Drama, Western",
        "Where you left off": "▶️ Resume **S01E02 · Kill the Messenger** at **9:01**\n1 of 53 episodes watched",
    }


def test_show_with_the_next_episode_not_started():
    rec = load("series_up_next")
    assert series_progress(rec["item"], rec["next"]) == (
        "⏭️ Up next: **S02E10 · Bart Gets Hit by a Car**\n22 of 225 episodes watched")


def test_show_for_someone_not_linked():
    rec = load("series_no_user")
    # Asked without a user, Jellyfin leaves out the counts: /watch fills in the episodes
    assert "RecursiveItemCount" not in rec["item"] and "ChildCount" not in rec["item"]
    embed = watch_card({**rec["item"], "RecursiveItemCount": rec["episode_count"]}, None)
    assert fields(embed)["Show"] == "54 episodes · ~23m each"
    assert "/jf-link" in embed.footer.text


def test_a_season_import_is_one_message_per_show():
    episodes = load("recent_episodes")
    newest = new_episodes.newest_date(episodes, None)
    groups = new_episodes.pick_new(episodes, known=set(), now=newest + timedelta(hours=1))
    by_show = {g[0]["SeriesName"]: g for g in groups}
    fallout = by_show["Fallout"]
    # Imported out of order (E06 first, 40 seconds early) and still one message, in order
    assert [e["IndexNumber"] for e in fallout] == list(range(1, 9))
    assert new_episodes.episode_range(fallout) == "S02E01–E08"
    assert notification_embed(fallout).title == "📺 8 new episodes of Fallout"


def test_the_newest_import_is_held_until_it_settles():
    episodes = load("recent_episodes")
    newest = new_episodes.newest_date(episodes, None)
    groups = new_episodes.pick_new(episodes, known=set(), now=newest + timedelta(minutes=2))
    assert "Fallout" not in {g[0]["SeriesName"] for g in groups}


def test_every_recorded_episode_has_what_the_poll_reads():
    for ep in load("recent_episodes"):
        assert new_episodes.parse_date(ep["DateCreated"]), ep
        assert ep.get("SeriesId") and ep.get("SeriesName"), ep
