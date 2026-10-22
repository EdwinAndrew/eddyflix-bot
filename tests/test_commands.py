"""The command set and the owner check. Changing a command name or dropping one should
fail here first, not in Discord."""
import asyncio
from types import SimpleNamespace

import checks
from bot import EddyFlixBot

EXPECTED = {
    "jf-achievements", "jf-achievements-all", "jf-debug-sessions", "jf-health", "jf-help", "jf-link",
    "jf-popular", "jf-recent", "jf-stats", "jf-stats-graph", "jf-status", "jf-streaks",
    "jf-streaks-leaderboard", "jf-subscribe", "jf-subscriptions", "jf-unsubscribe",
    "jf-weekly-digest", "movienight", "movievote",
}
OWNER_ONLY = {"jf-achievements-all", "jf-debug-sessions", "jf-weekly-digest"}


def loaded_tree():
    async def go():
        bot = EddyFlixBot()
        await bot.setup_hook()
        return bot.tree.get_commands()
    return asyncio.run(go())


def test_all_commands_are_registered():
    assert {c.name for c in loaded_tree()} == EXPECTED


def test_owner_only_commands_are_hidden_and_checked():
    for command in loaded_tree():
        hidden = bool(command.default_permissions and command.default_permissions.administrator)
        assert hidden == (command.name in OWNER_ONLY), command.name
        assert bool(command.checks) == (command.name in OWNER_ONLY), command.name


def interaction(user_id, owner_id=100, app_owner=False):
    async def is_owner(user):
        return app_owner
    return SimpleNamespace(user=SimpleNamespace(id=user_id), guild=SimpleNamespace(owner_id=owner_id),
                           client=SimpleNamespace(is_owner=is_owner))


def test_owner_check():
    assert asyncio.run(checks.is_owner(interaction(100)))                  # server owner
    assert asyncio.run(checks.is_owner(interaction(5, app_owner=True)))    # bot application owner
    assert not asyncio.run(checks.is_owner(interaction(5)))                # anyone else
