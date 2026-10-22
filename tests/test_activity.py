"""Playback posts get a poster: the show's for an episode, the film's otherwise."""
import asyncio
from types import SimpleNamespace

from cogs.activity import Activity


class FakeJellyfin:
    def __init__(self, fail=False):
        self.fail, self.asked = fail, []

    async def get_image(self, item_id, image_type="Primary", max_width=1000):
        self.asked.append((item_id, image_type, max_width))
        if self.fail:
            raise TimeoutError("Jellyfin did not answer")
        return b"\xff\xd8 jpeg"


def poster(item, jf):
    return asyncio.run(Activity(SimpleNamespace(jf=jf)).poster_for(item))


def test_episode_uses_the_show_poster():
    jf = FakeJellyfin()
    assert poster({"Id": "ep1", "Type": "Episode", "SeriesId": "show1"}, jf) == b"\xff\xd8 jpeg"
    assert jf.asked == [("show1", "Primary", 300)]


def test_film_uses_its_own_poster():
    jf = FakeJellyfin()
    poster({"Id": "film1", "Type": "Movie"}, jf)
    assert jf.asked == [("film1", "Primary", 300)]


def test_episode_without_series_falls_back_to_itself():
    jf = FakeJellyfin()
    poster({"Id": "ep1", "Type": "Episode"}, jf)
    assert jf.asked == [("ep1", "Primary", 300)]


def test_no_poster_when_jellyfin_fails():
    assert poster({"Id": "film1", "Type": "Movie"}, FakeJellyfin(fail=True)) is None


def test_no_poster_without_jellyfin():
    assert poster({"Id": "film1", "Type": "Movie"}, None) is None
