"""Watching as a household: /pick with someone, and where the other linked members are
in a show ("Sarah is 3 episodes ahead of you") on /watch, /pick and /continue."""
import asyncio
from types import SimpleNamespace

import pytest

import config
import household
import watch_data
from cogs.watch import Watch, continue_entry, pool_footer, watch_card
from jellyfin_client import JellyfinClient

ME, SARAH, TOM = "u-me", "u-sarah", "u-tom"


def episode(show, season, number, position=0, **extra):
    return {"Type": "Episode", "Id": f"{show}-{season}-{number}", "SeriesId": show, "SeriesName": show.title(),
            "ParentIndexNumber": season, "IndexNumber": number, "Name": f"Episode {number}",
            "UserData": {"PlaybackPositionTicks": position, "Played": False}, **extra}


def series(show, total, unplayed, played=False):
    return {"Type": "Series", "Id": show, "Name": show.title(), "RecursiveItemCount": total,
            "UserData": {"UnplayedItemCount": unplayed, "Played": played}}


def seasons(*sizes):
    """Episodes for seasons of these sizes, as Jellyfin lists them."""
    return [{"ParentIndexNumber": s, "IndexNumber": n} for s, size in enumerate(sizes, 1) for n in range(1, size + 1)]


ORDER = household.episode_order(seasons(10, 10, 5))     # 25 episodes


# --- who else is linked ----------------------------------------------------------

def test_other_members():
    links = [(1, "eddy"), (2, "Sarah"), (3, "sarah"), (4, "gone"), (5, "tom")]
    ids = {"eddy": ME, "sarah": SARAH, "tom": TOM}
    others = household.other_members(links, ME, ids, {2: "Sarah K", 5: "Tom"})
    # Not the caller, one entry per Jellyfin user, links to no Jellyfin user left out
    assert others == [("Sarah K", SARAH), ("Tom", TOM)]
    # Without a Discord name the Jellyfin name is shown
    assert household.other_members([(2, "sarah")], ME, ids, {}) == [("sarah", SARAH)]
    # Someone sharing the caller's Jellyfin user is not "other"
    assert household.other_members([(1, "eddy"), (9, "eddy")], ME, ids, {}) == []


def test_other_members_are_capped():
    links = [(n, f"user{n}") for n in range(30)]
    ids = {f"user{n}": f"id{n}" for n in range(30)}
    assert len(household.other_members(links, None, ids, {})) == household.MAX_OTHERS


def test_all_links(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "bot.db"))
    asyncio.run(watch_data.init_database())
    asyncio.run(watch_data.set_link(111, "eddy"))
    asyncio.run(watch_data.set_link(222, "sarah"))
    assert sorted(asyncio.run(watch_data.all_links())) == [(111, "eddy"), (222, "sarah")]


# --- /pick with someone ------------------------------------------------------------

def test_not_linked_messages():
    assert household.not_linked(True, "Sarah", True) is None
    mine = household.not_linked(False, "Sarah", True)
    assert "link your Jellyfin user first" in mine and "/link" in mine and "👤" in mine
    theirs = household.not_linked(True, "Sarah", False)
    assert theirs.startswith("Sarah isn't linked") and "/link" in theirs and "#roles" in theirs
    both = household.not_linked(False, "Sarah", False)
    assert "you both need to be linked" in both and "ask Sarah" in both


def test_unwatched_by_both():
    mine = [{"Id": "a"}, {"Id": "b"}, {"Id": "c"}]
    theirs = [{"Id": "c"}, {"Id": "a"}, {"Id": "z"}]
    assert household.unwatched_by_both(mine, theirs) == [{"Id": "a"}, {"Id": "c"}]


def test_together_card_and_footer(monkeypatch):
    monkeypatch.setattr(config, "JELLYFIN_PUBLIC_URL", "")
    film = {"Id": "f" * 32, "Type": "Movie", "Name": "Up", "RunTimeTicks": 0}
    fields = {f.name: f.value for f in watch_card(film, "Not watched yet", together="Sarah").fields}
    assert fields["You and Sarah"] == "Neither of you has seen this"
    show = {"Id": "s" * 32, "Type": "Series", "Name": "Fallout"}
    assert household.together_note(show) == "Neither of you has finished this"
    assert pool_footer(12, "Movie", True, True, together=True) == "Picked from 12 films neither of you has watched"
    assert pool_footer(1, "Movie,Series", True, True, together=True) == \
        "Picked from 1 title neither of you has watched"


# --- where everyone is in a show ------------------------------------------------------

def test_episode_order():
    episodes = [{"ParentIndexNumber": 2, "IndexNumber": 1}, {"ParentIndexNumber": 1, "IndexNumber": 2},
                {"ParentIndexNumber": 1, "IndexNumber": 1}, {"ParentIndexNumber": 1, "IndexNumber": 1},  # 2 copies
                {"ParentIndexNumber": 0, "IndexNumber": 1},                                             # special
                {"ParentIndexNumber": 1, "IndexNumber": None},
                {"ParentIndexNumber": 2, "IndexNumber": 2, "LocationType": "Virtual"}]                  # missing
    assert household.episode_order(episodes) == [(1, 1), (1, 2), (2, 1)]


def test_position():
    # Season 2 episode 4: ten in season 1 and three in season 2 before it
    assert household.position(ORDER, series("x", 25, 12), episode("x", 2, 4)) == 13
    assert household.position(ORDER, series("x", 25, 0, played=True), None) == 25
    # Half-way through the first episode counts as started
    assert household.position(ORDER, series("x", 25, 25), episode("x", 1, 1, position=5)) == 0
    # Not started, even when Jellyfin offers the first episode as next up
    assert household.position(ORDER, series("x", 25, 25), episode("x", 1, 1)) is None
    assert household.position(ORDER, {}, None) is None
    # Started, but on a special or with no next-up episode: cannot be placed
    assert household.position(ORDER, series("x", 25, 20), episode("x", 0, 1)) is None
    assert household.position(ORDER, series("x", 25, 20), None) is None
    # The caller not having started is the start of the show
    assert household.my_position(ORDER, series("x", 25, 25), None) == 0
    assert household.my_position(ORDER, series("x", 25, 12), episode("x", 2, 4)) == 13


def test_ahead_lines():
    assert household.ahead_line("Sarah", 16, 13, 25) == "Sarah is 3 episodes ahead of you"
    assert household.ahead_line("Sarah", 14, 13, 25) == "Sarah is 1 episode ahead of you"
    assert household.ahead_line("Sarah", 11, 13, 25) == "Sarah is 2 episodes behind you"
    assert household.ahead_line("Sarah", 13, 13, 25) == "Sarah is at the same episode"
    assert household.ahead_line("Sarah", 25, 25, 25) == "Sarah has finished it too"
    assert household.ahead_lines(10, [("tom", 4), ("Sarah", 12)], 25) == [
        "Sarah is 2 episodes ahead of you", "tom is 6 episodes behind you"]
    assert household.ahead_lines(None, [("Sarah", 12)], 25) == []


def test_continue_entry(monkeypatch):
    monkeypatch.setattr(config, "JELLYFIN_PUBLIC_URL", "")
    item = episode("fallout", 2, 4)
    assert continue_entry(item, None) == "⏭️ **Fallout** · S02E04 · Episode 4"
    assert continue_entry(item, ["Sarah is 3 episodes ahead of you", "Tom is at the same episode"]) == (
        "⏭️ **Fallout** · S02E04 · Episode 4\n↳ Sarah is 3 episodes ahead of you · Tom is at the same episode")


def test_new_queries():
    client = JellyfinClient("http://jellyfin:8096", "key")
    seen = []

    async def fake_get(endpoint):
        seen.append(endpoint)
        return {"Items": []}

    client._get = fake_get
    asyncio.run(client.series_episodes("s1"))
    assert "ParentId=s1" in seen[-1] and "IncludeItemTypes=Episode" in seen[-1]
    assert "IsMissing=false" in seen[-1] and "EnableUserData=false" in seen[-1]
    asyncio.run(client.user_items(["a", "b"], "u1"))
    assert "Ids=a,b" in seen[-1] and "userId=u1" in seen[-1] and "RecursiveItemCount" in seen[-1]
    calls = len(seen)
    assert asyncio.run(client.user_items([], "u1")) == [] and len(seen) == calls


# --- the commands, against a fake Jellyfin ------------------------------------------

FILMS = {"a": {"Id": "a", "Type": "Movie", "Name": "A", "RunTimeTicks": 0},
         "b": {"Id": "b", "Type": "Movie", "Name": "B", "RunTimeTicks": 0},
         "c": {"Id": "c", "Type": "Movie", "Name": "C", "RunTimeTicks": 0}}


class FakeJellyfin:
    """Three linked users. Each one's unwatched films, and where they are in Fallout
    (25 episodes) and Yellowstone (25)."""

    def __init__(self):
        self.calls = []
        self.unwatched = {ME: ["a", "b"], SARAH: ["b", "c"], TOM: ["a", "b", "c"]}
        self.shows = {   # user -> show -> (series item, next-up episode)
            ME: {"fallout": (series("fallout", 25, 12), episode("fallout", 2, 4)),
                 "yellowstone": (series("yellowstone", 25, 25), episode("yellowstone", 1, 1, position=5))},
            SARAH: {"fallout": (series("fallout", 25, 9), episode("fallout", 2, 7)),
                    "yellowstone": (series("yellowstone", 25, 25), None)},
            TOM: {"fallout": (series("fallout", 25, 0, played=True), None),
                  "yellowstone": (series("yellowstone", 25, 25), None)},
        }

    async def get_users(self):
        self.calls.append("users")
        return [{"Name": "eddy", "Id": ME}, {"Name": "sarah", "Id": SARAH}, {"Name": "tom", "Id": TOM}]

    async def pick_pool(self, types, genre=None, user_id=None, unwatched=False):
        self.calls.append(("pool", user_id, unwatched))
        ids = self.unwatched[user_id] if unwatched else list(FILMS)
        return [FILMS[i] for i in ids]

    async def get_item(self, item_id, user_id=None):
        self.calls.append(("item", item_id, user_id))
        if item_id in FILMS:
            return FILMS[item_id]
        return {**self.shows[user_id][item_id][0], "Overview": ""} if user_id else series(item_id, 25, 25)

    async def next_up(self, user_id, series_id):
        self.calls.append(("next_up", user_id, series_id))
        return self.shows[user_id][series_id][1]

    async def next_up_items(self, user_id, limit=5):
        self.calls.append(("next_up_items", user_id))
        return [e for _, e in self.shows[user_id].values() if e]

    async def user_items(self, item_ids, user_id):
        self.calls.append(("user_items", user_id))
        return [self.shows[user_id][i][0] for i in item_ids]

    async def series_episodes(self, series_id):
        self.calls.append(("episodes", series_id))
        return seasons(10, 10, 5)

    async def resume_items(self, user_id, limit=5):
        return [self.shows[user_id]["yellowstone"][1]]

    async def played_episodes(self, user_id, limit=100):
        return []

    async def get_image(self, *args, **kwargs):
        return None

    def count(self, kind):
        return sum(1 for c in self.calls if c == kind or (isinstance(c, tuple) and c[0] == kind))


class Sent:
    def __init__(self):
        self.messages, self.edits = [], []


def interaction_for(user_id, sent):
    async def defer(**kwargs):
        pass

    async def send(content=None, **kwargs):
        sent.messages.append((content, kwargs))

    async def edit(**kwargs):
        sent.edits.append(kwargs)

    names = {1: "Eddy", 2: "Sarah", 3: "Tom"}
    guild = SimpleNamespace(get_member=lambda d: SimpleNamespace(display_name=names[d]) if d in names else None)
    return SimpleNamespace(user=SimpleNamespace(id=user_id), guild=guild, response=SimpleNamespace(defer=defer),
                           followup=SimpleNamespace(send=send), edit_original_response=edit)


def member(discord_id, name, bot=False):
    return SimpleNamespace(id=discord_id, display_name=name, bot=bot)


@pytest.fixture
def cog(monkeypatch):
    monkeypatch.setattr(config, "JELLYFIN_PUBLIC_URL", "")
    links = [(1, "eddy"), (2, "sarah"), (3, "tom")]

    async def all_links():
        return list(links)

    monkeypatch.setattr(watch_data, "all_links", all_links)
    watch = Watch(SimpleNamespace(jf=FakeJellyfin()))
    watch.links = links
    return watch


def pick(cog, user_id, **options):
    sent = Sent()
    defaults = dict(kind=None, genre=None, max_length=None, include_watched=False, partner=None)

    async def go():
        await cog.pick.callback(cog, interaction_for(user_id, sent), **{**defaults, **options})
    asyncio.run(go())
    return sent


def fields(embed):
    return {f.name: f.value for f in embed.fields}


def test_pick_with_someone_picks_what_neither_has_seen(cog):
    picked = set()

    async def go():
        sent = Sent()
        interaction = interaction_for(1, sent)
        await cog.pick.callback(cog, interaction, kind=None, genre=None, max_length=None,
                                include_watched=False, partner=member(2, "Sarah"))
        _, kwargs = sent.messages[0]
        embed, view = kwargs["embed"], kwargs["view"]
        picked.add(embed.title)
        assert fields(embed)["You and Sarah"] == "Neither of you has seen this"
        assert embed.footer.text == "Picked from 1 title neither of you has watched"
        # A reroll keeps the filter
        for _ in range(3):
            await view.show(interaction)
            assert fields(sent.edits[-1]["embed"])["You and Sarah"] == "Neither of you has seen this"
            picked.add(sent.edits[-1]["embed"].title)
        view.stop()
    asyncio.run(go())
    assert picked == {"🎬 B"}          # eddy has not seen a or b, Sarah not b or c
    assert ("pool", ME, True) in cog.bot.jf.calls and ("pool", SARAH, True) in cog.bot.jf.calls


def test_pick_with_someone_not_linked(cog):
    cog.links.remove((2, "sarah"))
    sent = pick(cog, 1, partner=member(2, "Sarah"))
    text, kwargs = sent.messages[0]
    assert text.startswith("Sarah isn't linked") and kwargs["ephemeral"]
    assert cog.bot.jf.count("pool") == 0


def test_pick_with_someone_when_you_are_not_linked(cog):
    sent = pick(cog, 9, partner=member(2, "Sarah"))
    assert "link your Jellyfin user first" in sent.messages[0][0]


def test_pick_with_a_bot(cog):
    assert "not a bot" in pick(cog, 1, partner=member(7, "EddyFlix", bot=True)).messages[0][0]


def test_include_watched_turns_the_filter_off_for_both(cog):
    sent = pick(cog, 1, include_watched=True, partner=member(2, "Sarah"))
    embed = sent.messages[0][1]["embed"]
    assert not any(n.startswith("You and") for n in fields(embed))
    assert embed.footer.text == "Picked from 3 films and shows"
    assert [c for c in cog.bot.jf.calls if c[0] == "pool"] == [("pool", ME, False)]
    sent.messages[0][1]["view"].stop()


def test_pick_with_nothing_left(cog):
    cog.bot.jf.unwatched[SARAH] = ["c"]
    text = pick(cog, 1, partner=member(2, "Sarah")).messages[0][0]
    assert text.startswith("Nothing that neither you nor Sarah has seen matches that")


def test_watch_card_says_where_the_others_are(cog):
    item, embed, _ = asyncio.run(cog.answer({"Id": "fallout"}, ME, [("Sarah", SARAH), ("Tom", TOM)]))
    assert fields(embed)["Others watching"] == "Tom is 12 episodes ahead of you\nSarah is 3 episodes ahead of you"
    # Yellowstone: eddy is half-way through E01, nobody else has started it: no lines
    _, embed, _ = asyncio.run(cog.answer({"Id": "yellowstone"}, ME, [("Sarah", SARAH), ("Tom", TOM)]))
    assert "Others watching" not in fields(embed)
    # Two calls per other member, and the episode list only for a show someone else has started
    assert cog.bot.jf.count("episodes") == 1


def test_others_behind_you(cog):
    _, embed, _ = asyncio.run(cog.answer({"Id": "fallout"}, SARAH, [("Eddy", ME)]))
    assert fields(embed)["Others watching"] == "Eddy is 3 episodes behind you"


def test_continue_says_where_the_others_are_with_bounded_calls(cog):
    sent = Sent()
    asyncio.run(cog.continue_watching.callback(cog, interaction_for(1, sent)))
    lines = sent.messages[0][1]["embed"].description.split("\n")
    fallout = next(n for n, line in enumerate(lines) if "Fallout" in line)
    assert lines[fallout + 1] == "↳ Tom is 12 episodes ahead of you · Sarah is 3 episodes ahead of you"
    # Nobody else has started Yellowstone
    yellowstone = next(n for n, line in enumerate(lines) if "Yellowstone" in line)
    assert not lines[yellowstone + 1:yellowstone + 2] or not lines[yellowstone + 1].startswith("↳")
    jf = cog.bot.jf
    # One batch of shows and one next-up list per other member, whatever the number of shows
    assert jf.count("user_items") == 2 and jf.count("next_up_items") == 3      # the caller's own list too
    assert jf.count("next_up") == 0 and jf.count("episodes") == 1
    assert jf.count("users") == 1
