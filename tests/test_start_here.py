"""The guide and picker live in #start-here; a server still on #roles keeps working."""
from types import SimpleNamespace

from cogs import movie_night


def guild(*names):
    return SimpleNamespace(text_channels=[SimpleNamespace(name=n) for n in names])


def test_start_here_is_found():
    assert movie_night.find_roles_channel(guild("general", "start-here")).name == "start-here"


def test_the_old_name_still_works():
    assert movie_night.find_roles_channel(guild("general", "roles")).name == "roles"


def test_start_here_wins_over_roles():
    assert movie_night.find_roles_channel(guild("roles", "start-here")).name == "start-here"


def test_neither_means_none():
    assert movie_night.find_roles_channel(guild("general")) is None
