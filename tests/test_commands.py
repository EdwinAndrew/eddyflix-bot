"""The command set and the owner check. Changing a command name or dropping one should
fail here first, not in Discord."""
import asyncio
from types import SimpleNamespace

import discord

import checks
from bot import EddyFlixBot

# What a regular member sees: no default permissions, no checks
MEMBER = {
    "watch", "pick", "continue", "request", "stats", "achievements", "streaks", "link",
    "subscribe", "unsubscribe", "subscriptions", "movienight-history", "help",
}
# Directors and Ushers: listed only for Timeout Members, checked with can_host in the command
STAFF = {"movienight", "movievote", "spoiler", "unspoiler"}
# The owner's, all under /admin
ADMIN = {"summary", "weekly-digest", "weekly-new", "selftest", "health", "sessions", "achievements"}


def loaded_tree():
    async def go():
        bot = EddyFlixBot()
        await bot.setup_hook()
        return bot.tree.get_commands()
    return asyncio.run(go())


def test_all_commands_are_registered():
    tree = {c.name: c for c in loaded_tree()}
    assert set(tree) == MEMBER | STAFF | {"admin"}
    assert {c.name for c in tree["admin"].commands} == ADMIN


def test_no_old_names():
    assert not [c.name for c in loaded_tree() if c.name.startswith("jf-")]


def test_members_see_only_their_commands():
    for command in loaded_tree():
        if command.name in MEMBER:
            assert command.default_permissions is None, command.name
            assert not command.checks, command.name


def test_staff_commands_are_hidden_from_members():
    for command in loaded_tree():
        if command.name in STAFF:
            assert command.default_permissions == discord.Permissions(moderate_members=True), command.name
            assert command.guild_only, command.name


def test_admin_group_is_hidden_and_every_command_checked():
    admin = next(c for c in loaded_tree() if c.name == "admin")
    assert admin.default_permissions == discord.Permissions(administrator=True)
    assert admin.guild_only
    for command in admin.commands:
        assert checks.is_owner in command.checks, command.name


def interaction(user_id, owner_id=100, app_owner=False):
    async def is_owner(user):
        return app_owner
    return SimpleNamespace(user=SimpleNamespace(id=user_id), guild=SimpleNamespace(owner_id=owner_id),
                           client=SimpleNamespace(is_owner=is_owner))


def test_owner_check():
    assert asyncio.run(checks.is_owner(interaction(100)))                  # server owner
    assert asyncio.run(checks.is_owner(interaction(5, app_owner=True)))    # bot application owner
    assert not asyncio.run(checks.is_owner(interaction(5)))                # anyone else
