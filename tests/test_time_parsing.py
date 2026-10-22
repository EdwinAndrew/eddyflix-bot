"""/movienight's day and time parsing, all in Toronto time."""
from datetime import datetime

import pytest

from cogs.movie_night import TZ, parse_day, parse_time, parse_when

# A Thursday afternoon.
NOW = datetime(2026, 10, 22, 14, 30, tzinfo=TZ)


@pytest.mark.parametrize("text, expected", [
    ("8pm", (20, 0)),
    ("8 PM", (20, 0)),
    ("8:30pm", (20, 30)),
    ("8.30pm", (20, 30)),
    ("20:00", (20, 0)),
    ("8", (20, 0)),          # a bare hour means the evening
    ("8:30", (20, 30)),
    ("11", (23, 0)),
    ("12", (12, 0)),          # 12 without am/pm is noon
    ("0:15", (0, 15)),
    ("9am", (9, 0)),
    ("12am", (0, 0)),
    ("12pm", (12, 0)),
])
def test_parse_time(text, expected):
    assert parse_time(text) == expected


@pytest.mark.parametrize("text", ["25:00", "13pm", "0am", "8:60", "eight", "", "8:5"])
def test_parse_time_rejects(text):
    with pytest.raises(ValueError):
        parse_time(text)


@pytest.mark.parametrize("text, expected_day, is_weekday", [
    ("today", 22, False),
    ("tonight", 22, False),
    ("Tomorrow", 23, False),
    ("friday", 23, True),
    ("fri", 23, True),
    ("thursday", 22, True),   # today's weekday is today, not next week
    ("wed", 28, True),        # already past this week: next Wednesday
    ("2026-10-31", 31, False),
])
def test_parse_day(text, expected_day, is_weekday):
    date, weekday = parse_day(text, NOW)
    assert date.day == expected_day
    assert weekday == is_weekday


@pytest.mark.parametrize("text", ["fr", "someday", "31/10/2026"])
def test_parse_day_rejects(text):
    with pytest.raises(ValueError):
        parse_day(text, NOW)


def test_tonight():
    assert parse_when("tonight", "8pm", NOW) == datetime(2026, 10, 22, 20, 0, tzinfo=TZ)


def test_weekday_later_today_is_today():
    assert parse_when("thursday", "8pm", NOW) == datetime(2026, 10, 22, 20, 0, tzinfo=TZ)


def test_weekday_already_past_today_means_next_week():
    assert parse_when("thursday", "9am", NOW) == datetime(2026, 10, 29, 9, 0, tzinfo=TZ)


def test_today_in_the_past_is_refused():
    with pytest.raises(ValueError, match="past"):
        parse_when("today", "9am", NOW)


def test_date_in_the_past_is_refused():
    with pytest.raises(ValueError, match="past"):
        parse_when("2026-10-01", "8pm", NOW)


def test_result_is_toronto_time():
    when = parse_when("2026-12-25", "8pm", NOW)
    assert when.utcoffset().total_seconds() == -5 * 3600   # EST in December
