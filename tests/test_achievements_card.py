"""The /jf-achievements card."""
from cogs.achievements import achievement_card, earned_unix

SUMMARY = {
    "hours": 512.4,
    "hour_milestones": [10, 50, 100, 250, 500],
    "streak_milestones": [7],
    "series": [("Breaking Bad", 62, 62, "2025-12-15 03:00:00"), ("Landman", 20, None, "2026-02-08T21:00:00")],
    "streak": (2, 7),
}


def card(summary, linked=True, **kw):
    return achievement_card("eddy", summary, linked, **kw)[0]


def fields(embed):
    return {f.name: f.value for f in embed.fields}


def test_card_groups_everything_in_three_fields():
    shown = fields(card(SUMMARY))
    assert shown["⏱️ Watch time"].splitlines() == [
        "🧂 **Salted** · 512 h", "▱▱▱▱▱▱▱▱▱▱", "Next: Buttery Popcorn in 488 h", "🌽🟡💥🍿🧂▫️"]
    assert shown["🔥 Streak"] == "Current **2** · Best **7** days\n🏅 7-day"
    series = shown["📺 Finished series (2)"].splitlines()
    assert series[0].startswith("**Breaking Bad** · 62/62 episodes · <t:")
    assert series[1].startswith("**Landman** · 20 episodes · <t:")      # older rows have no total


def test_current_tier_badge_in_the_corner():
    embed, badge = achievement_card("eddy", SUMMARY, linked=True)
    assert badge.filename == "tier-salted.png"
    assert embed.thumbnail.url == "attachment://tier-salted.png"
    assert embed.colour.value == 0x5CC8FF


def test_footer_only_when_not_linked():
    assert card(SUMMARY).footer.text is None
    assert "/jf-link" in card(SUMMARY, linked=False).footer.text


def test_empty_parts():
    embed, badge = achievement_card("new", {"hours": 0, "hour_milestones": [], "streak_milestones": [],
                                            "series": [], "streak": None}, linked=True)
    shown = fields(embed)
    assert badge is None and embed.thumbnail.url is None
    assert shown["⏱️ Watch time"].splitlines() == ["**0 h** watched", "▱▱▱▱▱▱▱▱▱▱", "Next: Corn in 10 h", "▫️" * 6]
    assert shown["🔥 Streak"] == "No streak yet"
    assert shown["📺 Finished series (0)"] == "None yet"


def test_long_series_lists_are_cut_to_fit():
    many = {**SUMMARY, "series": [(f"A rather long series name number {n}", 10, 10, "2026-01-01 00:00:00") for n in range(60)]}
    value = fields(card(many, limit=60))["📺 Finished series (60)"]
    assert len(value) <= 1024 and value.endswith("more")


def test_timestamps_utc_vs_local():
    # SQLite's CURRENT_TIMESTAMP is UTC; the bot's own isoformat() is Toronto time.
    assert earned_unix("2026-10-22 18:00:00") == earned_unix("2026-10-22T14:00:00")
    assert earned_unix("garbage") is None
