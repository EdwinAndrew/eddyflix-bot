"""Following a movie night: when to remind, start and end, who attended, and what gets posted."""
from datetime import datetime, timedelta, timezone

import pytest

import screenings
from cogs.movie_night import film_ref, syncplay_steps
from screenings import before_start, ordinal, recap_embed, split_attendance, time_to_end

START = datetime(2026, 11, 13, 1, 0, tzinfo=timezone.utc)   # 8 PM in Toronto
END = START + timedelta(minutes=135)                         # a 2-hour film + 15 minutes
EARLY = START - timedelta(days=2)                            # announced days ahead


def at(minutes):
    return START + timedelta(minutes=minutes)


@pytest.mark.parametrize("minutes, reminded, expected", [
    (-60, False, None),          # too early
    (-15, False, "remind"),
    (-5, False, "remind"),       # back from a restart inside the window: still worth it
    (-5, True, None),            # already sent, before or after a restart
    (0, False, "start"),         # no reminder once it has started
    (60, True, "start"),         # the bot came back mid-film
    (135, True, "missed"),       # the bot was down for all of it
])
def test_before_start(minutes, reminded, expected):
    assert before_start(at(minutes), START, END, EARLY, reminded) == expected


def test_no_reminder_right_after_the_announcement():
    # Scheduled 20 minutes ahead: the announcement pinged people a moment ago
    assert before_start(at(-10), START, END, at(-20), False) is None
    assert before_start(at(-10), START, END, at(-30), False) == "remind"


def test_ends_at_the_end_time():
    assert not time_to_end(at(134), START, END, None)
    assert time_to_end(at(135), START, END, None)


def test_ends_early_once_the_theatre_has_been_empty_in_the_second_half():
    assert time_to_end(at(100), START, END, at(85))            # empty 15 min, past halfway
    assert not time_to_end(at(95), START, END, at(85))         # only 10 min
    assert not time_to_end(at(40), START, END, at(10))         # a break in the first half


def test_attendance_takes_fifteen_minutes():
    attended, dropped = split_attendance({1: 120, 2: 15, 3: 14, 4: 1, 5: 0})
    assert attended == [1, 2]
    assert dropped == [3, 4]


@pytest.mark.parametrize("n, text", [(1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"), (11, "11th"),
                                     (12, "12th"), (13, "13th"), (21, "21st"), (102, "102nd")])
def test_ordinal(n, text):
    assert ordinal(n) == text


def test_recap():
    embed = recap_embed("1917 (2019)", [(1, 1), (2, 3)], [3])
    fields = {f.name: f.value for f in embed.fields}
    assert embed.title == "🎬 That's a wrap: 1917 (2019)"
    assert fields["🎥 In the Theatre (2)"] == "<@1> · 1st movie night\n<@2> · 3rd movie night"
    assert fields["Dropped by"] == "<@3>"
    assert str(screenings.ATTEND_MINUTES) in embed.footer.text


def test_recap_when_nobody_stayed():
    fields = {f.name: f.value for f in recap_embed("1917", [], [3]).fields}
    assert fields["🎥 In the Theatre (0)"] == "Nobody stayed"


def test_syncplay_steps_shared_by_announcement_and_reminder():
    steps = syncplay_steps(film_ref("1917", "https://jf.example/x"), "Eddy", "<#9>")
    assert steps.splitlines()[0] == "1. **Everyone** opens [1917](https://jf.example/x) in Jellyfin. SyncPlay won't open it for you"
    assert "join **Eddy**'s group" in steps
    assert steps.splitlines()[-1] == "4. Hop into <#9> to talk along"
    # Nights found in Discord rather than made here have no host or link
    unknown = syncplay_steps(film_ref("1917", None), None, "<#9>")
    assert "**1917**" in unknown and "join the group, or start one" in unknown
    embed = screenings.reminder_embed("1917 (2019)", None, unknown)
    assert embed.title == "🍿 Starting soon: 1917 (2019)" and embed.description.endswith(unknown)
