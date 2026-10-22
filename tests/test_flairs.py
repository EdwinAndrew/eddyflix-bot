"""The role picker: the flair list itself, and what picking does to a member's roles."""
import asyncio
from types import SimpleNamespace

from cogs import movie_night as mn


def test_flair_list_fits_discord_limits():
    assert 1 <= len(mn.FLAIRS) <= 25                       # one select menu
    for f in mn.FLAIRS:
        assert len(f.label) <= 100 and len(f.blurb) <= 100   # select option limits
        assert len(f.role_name) <= 100                       # role name limit


def test_role_names_are_unique():
    names = [f.role_name for f in mn.FLAIRS] + [mn.TICKET_ROLE, mn.DIRECTOR_ROLE, mn.USHER_ROLE]
    assert len(names) == len(set(names))


class Role:
    def __init__(self, name):
        self.name = name


class Member:
    def __init__(self, roles):
        self.roles = list(roles)

    async def add_roles(self, *roles, reason=None):
        self.roles += [r for r in roles if r not in self.roles]

    async def remove_roles(self, *roles, reason=None):
        self.roles = [r for r in self.roles if r not in roles]


class Response:
    def __init__(self):
        self.sent = []

    async def send_message(self, content, ephemeral=False):
        self.sent.append((content, ephemeral))


def interaction_for(member, guild_roles):
    return SimpleNamespace(guild=SimpleNamespace(roles=guild_roles), user=member, response=Response())


def setup():
    roles = {f.label: Role(f.role_name) for f in mn.FLAIRS}
    ticket = Role(mn.TICKET_ROLE)
    return roles, ticket, list(roles.values()) + [ticket]


def test_picking_sets_exactly_the_chosen_flairs():
    roles, ticket, guild_roles = setup()
    first, second, third = mn.FLAIRS[:3]
    member = Member([roles[first.label], ticket])
    interaction = interaction_for(member, guild_roles)

    asyncio.run(mn.set_flairs(interaction, [second, third]))

    assert set(member.roles) == {roles[second.label], roles[third.label], ticket}   # ticket untouched
    content, ephemeral = interaction.response.sent[0]
    assert ephemeral and second.role_name in content


def test_clearing_removes_only_flairs():
    roles, ticket, guild_roles = setup()
    member = Member([roles[f.label] for f in mn.FLAIRS[:4]] + [ticket])
    interaction = interaction_for(member, guild_roles)

    asyncio.run(mn.set_flairs(interaction, []))

    assert member.roles == [ticket]
    assert interaction.response.sent[0] == ("Flairs cleared.", True)
