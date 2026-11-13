"""/watch: the card and the "where you left off" lines."""
import config
from cogs.watch import best_match, choice_label, clock, details, episode_label, film_progress, series_progress, watch_card

MIN = 60 * 10_000_000      # a minute in Jellyfin ticks

FILM = {"Id": "f" * 32, "Type": "Movie", "Name": "Rush Hour", "ProductionYear": 1998,
        "RunTimeTicks": 98 * MIN, "Overview": "Two cops.", "Genres": ["Action", "Comedy"]}
SHOW = {"Id": "s" * 32, "Type": "Series", "Name": "The Simpsons", "ProductionYear": 1989,
        "RunTimeTicks": 24 * MIN, "ChildCount": 11, "RecursiveItemCount": 225, "Status": "Continuing"}
EPISODE = {"Id": "e" * 32, "ParentIndexNumber": 2, "IndexNumber": 10, "Name": "Bart Gets Hit by a Car",
           "UserData": {"PlaybackPositionTicks": 0}}


def with_user(item, **data):
    return {**item, "UserData": data}


def test_labels():
    assert choice_label(FILM) == "🎬 Rush Hour (1998)"
    assert choice_label(SHOW) == "📺 The Simpsons (1989)"
    assert episode_label(EPISODE) == "S02E10 · Bart Gets Hit by a Car"
    assert episode_label({"Name": "Special"}) == "Special"
    assert clock(63 * MIN + 22 * 10_000_000) == "1:03:22"
    assert clock(26 * MIN + 37 * 10_000_000) == "26:37"


def test_details():
    assert details(FILM) == "1h 38m"
    assert details(SHOW) == "11 seasons · 225 episodes · ~24m each"
    assert details({**SHOW, "ChildCount": 1, "Status": "Ended"}).startswith("1 season · ")
    assert details({**SHOW, "Status": "Ended"}).endswith("· ended")


def test_film_progress():
    assert film_progress(with_user(FILM, Played=True)) == "✅ Watched"
    assert film_progress(with_user(FILM, PlaybackPositionTicks=63 * MIN, Played=False)) == "▶️ Stopped at **1:03:00** of 1:38:00"
    assert film_progress(with_user(FILM, PlaybackPositionTicks=0, Played=False)) == "Not watched yet"


def test_series_progress(monkeypatch):
    monkeypatch.setattr(config, "JELLYFIN_PUBLIC_URL", "")
    show = with_user(SHOW, UnplayedItemCount=203, Played=False)
    assert series_progress(show, EPISODE) == "⏭️ Up next: **S02E10 · Bart Gets Hit by a Car**\n22 of 225 episodes watched"
    half = {**EPISODE, "UserData": {"PlaybackPositionTicks": 26 * MIN}}
    assert series_progress(show, half).startswith("▶️ Resume **S02E10 · Bart Gets Hit by a Car** at **26:00**")
    assert series_progress(with_user(SHOW, UnplayedItemCount=225, Played=False), None) == "Not started yet"
    assert series_progress(with_user(SHOW, UnplayedItemCount=0, Played=True), None) == "✅ All caught up"


def test_next_episode_links_to_jellyfin(monkeypatch):
    monkeypatch.setattr(config, "JELLYFIN_PUBLIC_URL", "https://jf.example")
    line = series_progress(with_user(SHOW, UnplayedItemCount=225, Played=False), EPISODE)
    assert f"(https://jf.example/web/#/details?id={'e' * 32})" in line


def test_card(monkeypatch):
    monkeypatch.setattr(config, "JELLYFIN_PUBLIC_URL", "https://jf.example")
    embed = watch_card(FILM, "✅ Watched")
    assert embed.title == "🎬 Rush Hour (1998)"
    assert embed.url == f"https://jf.example/web/#/details?id={'f' * 32}"
    assert {f.name: f.value for f in embed.fields} == {
        "Runtime": "1h 38m", "Genre": "Action, Comedy", "Where you left off": "✅ Watched"}
    assert embed.footer.text is None


def test_card_without_link_or_progress(monkeypatch):
    monkeypatch.setattr(config, "JELLYFIN_PUBLIC_URL", "")
    embed = watch_card(SHOW, None)
    assert embed.url is None
    assert "Where you left off" not in {f.name for f in embed.fields}
    assert "/jf-link" in embed.footer.text


def test_year_is_not_doubled():
    assert choice_label({**SHOW, "Name": "Yellowstone (2018)", "ProductionYear": 2018}) == "📺 Yellowstone (2018)"


def test_typed_title_prefers_the_exact_one():
    rises, knight = {"Name": "The Dark Knight Rises"}, {"Name": "The Dark Knight"}
    assert best_match("dark knight", [rises, knight]) is knight
    assert best_match("The Dark Knight", [rises, knight]) is knight
    assert best_match("dark", [rises, knight]) is rises      # no exact title: Jellyfin's first hit
    assert best_match("zzz", []) is None
