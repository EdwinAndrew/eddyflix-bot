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


def test_spoiler_time_keeps_spoiler_alert_on():
    roles, ticket, guild_roles = setup()
    spoiler, other = mn.SPOILER_FLAIR, mn.FLAIRS[0]
    member = Member([roles[spoiler.label], ticket])
    interaction = interaction_for(member, guild_roles)

    asyncio.run(mn.set_flairs(interaction, [other], locked_until=1_800_000_000))

    assert set(member.roles) == {roles[spoiler.label], roles[other.label], ticket}   # the rest still applies
    content, ephemeral = interaction.response.sent[0]
    assert ephemeral and "stays on until <t:1800000000:f>" in content and other.role_name in content


def test_spoiler_time_survives_remove_my_flairs():
    roles, ticket, guild_roles = setup()
    member = Member([roles[f.label] for f in mn.FLAIRS[:6]])   # Spoiler Alert is fifth
    interaction = interaction_for(member, guild_roles)

    asyncio.run(mn.set_flairs(interaction, [], locked_until=1_800_000_000))

    assert member.roles == [roles[mn.SPOILER_FLAIR.label]]
    assert interaction.response.sent[0][0].startswith("Flairs cleared, but")


def test_spoiler_alert_can_go_once_the_time_is_up():
    roles, ticket, guild_roles = setup()
    member = Member([roles[mn.SPOILER_FLAIR.label]])
    interaction = interaction_for(member, guild_roles)

    asyncio.run(mn.set_flairs(interaction, [], locked_until=None))

    assert member.roles == []
    assert interaction.response.sent[0] == ("Flairs cleared.", True)


def test_keeping_spoiler_alert_by_choice_says_nothing_extra():
    roles, ticket, guild_roles = setup()
    member = Member([])
    interaction = interaction_for(member, guild_roles)

    asyncio.run(mn.set_flairs(interaction, [mn.SPOILER_FLAIR], locked_until=1_800_000_000))

    assert member.roles == [roles[mn.SPOILER_FLAIR.label]]
    assert "stays on" not in interaction.response.sent[0][0]


def test_picker_custom_ids_are_stable():
    async def make():
        return mn.RolePicker()
    view = asyncio.run(make())
    # Changing these breaks the buttons on the message already posted in #roles
    assert {c.custom_id for c in view.children} == {
        "movienight:flairs", "movienight:ticket", "movienight:clear", "movienight:link"}
    assert view.timeout is None
