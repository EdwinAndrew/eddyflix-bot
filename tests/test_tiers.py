"""Watch-time tiers and their badges."""
import io

import pytest
from PIL import Image

import tiers


def test_tiers_and_milestones():
    assert tiers.MILESTONES == [10, 50, 100, 250, 500, 1000]
    assert [t.name for t in tiers.TIERS] == ["Corn", "Kernel", "Popping", "Popcorn", "Salted", "Buttery Popcorn"]
    assert tiers.tier_for(100).name == "Popping"
    assert tiers.tier_for(42) is None


@pytest.mark.parametrize("hours, current, nxt", [
    (0, None, "Corn"), (9.9, None, "Corn"), (10, "Corn", "Kernel"),
    (249, "Popping", "Popcorn"), (999, "Salted", "Buttery Popcorn"), (5000, "Buttery Popcorn", None),
])
def test_current_and_next(hours, current, nxt):
    assert getattr(tiers.current_tier(hours), "name", None) == current
    assert getattr(tiers.next_tier(hours), "name", None) == nxt


def test_progress_bar_runs_between_tiers():
    assert tiers.progress_bar(0) == "▱" * 10
    assert tiers.progress_bar(5) == "▰" * 5 + "▱" * 5          # halfway to Corn
    assert tiers.progress_bar(75) == "▰" * 5 + "▱" * 5         # halfway from Kernel to Popping
    assert tiers.progress_bar(1500) == "▰" * 10


@pytest.mark.parametrize("tier", tiers.TIERS, ids=lambda t: t.key)
def test_every_badge_draws(tier):
    img = Image.open(io.BytesIO(tiers.badge_png(tier.key, 128)))
    assert img.size == (128, 128) and img.mode == "RGBA"
    assert img.getpixel((64, 64))[3] == 255            # solid in the middle
    assert img.getpixel((0, 0))[3] == 0                # see-through in the corner
