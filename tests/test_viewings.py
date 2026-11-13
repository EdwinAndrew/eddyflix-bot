"""The feed's viewing posts: one per sitting, edited in place, nothing for short plays."""
from datetime import datetime, timedelta

import viewings
from viewings import Feed

T0 = datetime(2026, 11, 12, 20, 0)
EP1 = {"Id": "e1", "Type": "Episode", "Name": "Pilot", "SeriesName": "Yellowstone",
       "ParentIndexNumber": 1, "IndexNumber": 1}
EP2 = {"Id": "e2", "Type": "Episode", "Name": "Kill the Messenger", "SeriesName": "Yellowstone",
       "ParentIndexNumber": 1, "IndexNumber": 2}
FILM = {"Id": "f1", "Type": "Movie", "Name": "1917", "ProductionYear": 2019}


def playing(sid, user, item, device="TV"):
    return {"Id": sid, "UserName": user, "DeviceName": device, "NowPlayingItem": item}


def idle(sid, user, device="TV"):
    return {"Id": sid, "UserName": user, "DeviceName": device}


class Clock:
    """Polls a Feed the way the cog does, minutes after T0."""

    def __init__(self):
        self.feed = Feed()
        self.seeded = False

    def poll(self, minutes, *sessions, in_party=()):
        changed = self.feed.update(list(sessions), T0 + timedelta(minutes=minutes), set(in_party), self.seeded)
        self.seeded = True
        for v in changed:
            v.message = v.message or "sent"
        return changed


def test_a_short_play_is_never_posted():
    c = Clock()
    c.poll(0)
    assert c.poll(1, playing("a", "eddy", EP1)) == []
    assert c.poll(4, playing("a", "eddy", EP1)) == []
    assert c.poll(4.5, idle("a", "eddy")) == []
    assert c.poll(20, idle("a", "eddy")) == []
    assert c.feed.viewings == {}                       # forgotten after the gap


def test_posted_once_a_play_passes_the_threshold():
    c = Clock()
    c.poll(0)
    c.poll(1, playing("a", "eddy", FILM))
    changed = c.poll(6, playing("a", "eddy", FILM))
    assert len(changed) == 1
    embed = viewings.embed(changed[0], T0 + timedelta(minutes=6))
    assert embed.title == "▶️ Now watching"
    assert embed.description == "**eddy** is watching **1917 (2019)**"
    assert c.poll(6.5, playing("a", "eddy", FILM)) == []   # nothing changed, no edit


def test_one_post_from_start_to_switch_to_stop():
    c = Clock()
    c.poll(0)
    c.poll(0.5, playing("a", "eddy", EP1))
    [first] = c.poll(10, playing("a", "eddy", EP1))
    [switched] = c.poll(50, playing("a", "eddy", EP2))
    assert switched is first                           # the same post, edited
    live = viewings.embed(switched, T0 + timedelta(minutes=50))
    assert live.description == "**eddy** is watching **Yellowstone S1E2** · Kill the Messenger"
    assert live.fields[0].name == "Earlier" and "Yellowstone S1E1" in live.fields[0].value

    [stopped] = c.poll(100, idle("a", "eddy"))
    assert stopped is first
    done = viewings.embed(stopped, T0 + timedelta(minutes=100))
    assert done.title == "⏹️ Watched"
    assert done.description == "**eddy** watched 2 titles, for 1h 39m"
    assert "S1E1" in done.fields[0].value and "S1E2" in done.fields[0].value


def test_a_short_last_play_is_left_off_when_they_stop():
    c = Clock()
    c.poll(0)
    c.poll(0, playing("a", "eddy", EP1))
    c.poll(45, playing("a", "eddy", EP1))
    c.poll(46, playing("a", "eddy", EP2))              # autoplay started the next one...
    [stopped] = c.poll(47, idle("a", "eddy"))          # ...and they stopped it
    done = viewings.embed(stopped, T0 + timedelta(minutes=47))
    assert done.description == "**eddy** watched **Yellowstone S1E1** · Pilot, for 46m"


def test_sampling_before_the_real_thing_is_left_out():
    c = Clock()
    c.poll(0)
    c.poll(0, playing("a", "eddy", FILM))
    c.poll(2, playing("a", "eddy", EP1))               # gave up on the film after two minutes
    [posted] = c.poll(8, playing("a", "eddy", EP1))
    embed = viewings.embed(posted, T0 + timedelta(minutes=8))
    assert [f.name for f in embed.fields] == ["Started", "Device"]   # no "Earlier": the film was a sample


def test_stopping_and_starting_again_soon_keeps_the_post():
    c = Clock()
    c.poll(0)
    c.poll(0, playing("a", "eddy", EP1))
    [post] = c.poll(10, playing("a", "eddy", EP1))
    c.poll(40, idle("a", "eddy"))
    assert c.poll(45, playing("a", "eddy", EP2)) == [post]   # picked the next episode by hand
    assert post.stopped is None


def test_moving_to_another_device_keeps_the_post():
    c = Clock()
    c.poll(0)
    c.poll(0, playing("a", "eddy", FILM, device="Chrome"))
    [post] = c.poll(10, playing("a", "eddy", FILM, device="Chrome"))
    c.poll(30)                                         # the laptop's session is gone
    assert c.poll(33, playing("b", "eddy", FILM, device="LG Smart TV")) == [post]
    assert post.device == "LG Smart TV"
    assert set(c.feed.viewings) == {"b"}


def test_a_long_break_starts_a_new_post():
    c = Clock()
    c.poll(0)
    c.poll(0, playing("a", "eddy", EP1))
    [first] = c.poll(10, playing("a", "eddy", EP1))
    c.poll(40, idle("a", "eddy"))
    c.poll(60, idle("a", "eddy"))
    c.poll(61, playing("a", "eddy", EP2))
    [second] = c.poll(67, playing("a", "eddy", EP2))
    assert second is not first


def test_two_people_get_their_own_posts():
    c = Clock()
    c.poll(0)
    c.poll(0, playing("a", "eddy", EP1), playing("b", "Sarah", FILM))
    posted = c.poll(6, playing("a", "eddy", EP1), playing("b", "Sarah", FILM))
    assert sorted(v.user for v in posted) == ["Sarah", "eddy"]


def test_passing_the_threshold_between_polls_then_stopping_still_posts():
    c = Clock()
    c.poll(0)
    c.poll(0, playing("a", "eddy", FILM))
    c.poll(4.9, playing("a", "eddy", FILM))
    [posted] = c.poll(5.4, idle("a", "eddy"))
    assert viewings.embed(posted, T0 + timedelta(minutes=5.4)).title == "⏹️ Watched"


def test_already_playing_at_start_is_quiet_until_the_next_title():
    c = Clock()
    c.poll(0, playing("a", "eddy", EP1))               # the bot restarted mid-episode
    assert c.poll(30, playing("a", "eddy", EP1)) == []
    assert c.poll(40, playing("a", "eddy", EP2)) == []
    [posted] = c.poll(46, playing("a", "eddy", EP2))
    assert [p.item["Id"] for p in posted.plays] == ["e2"]


def test_a_watch_party_has_its_own_post():
    c = Clock()
    c.poll(0)
    c.poll(0, playing("a", "eddy", FILM), playing("b", "Sarah", FILM), in_party={"a", "b"})
    assert c.poll(30, playing("a", "eddy", FILM), playing("b", "Sarah", FILM), in_party={"a", "b"}) == []
    assert c.poll(130, idle("a", "eddy"), idle("b", "Sarah")) == []


def test_after_the_party_a_solo_episode_is_posted_without_it():
    c = Clock()
    c.poll(0)
    c.poll(0, playing("a", "eddy", FILM), in_party={"a"})
    c.poll(120, playing("a", "eddy", EP1))
    [posted] = c.poll(126, playing("a", "eddy", EP1))
    assert [p.item["Id"] for p in posted.shown(T0 + timedelta(minutes=126))] == ["e1"]


def test_long_lists_are_cut():
    plays = [viewings.Play({"Id": str(n), "Name": f"Film {n}", "Type": "Movie"}, T0) for n in range(15)]
    text = viewings.listing(plays)
    assert text.startswith("…and 5 earlier")
    assert text.count("• ") == viewings.MAX_LISTED
