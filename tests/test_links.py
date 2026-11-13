"""Who may link to which Jellyfin user (links.py, /link) and the admin-channel notice."""
import asyncio
from types import SimpleNamespace

import pytest

import config
import jellyfin_client
import links
import watch_data
from cogs.achievements import Achievements

OWNER, SARAH, FRIEND = 1, 2, 3
USERS = [
    {"Name": "eddy", "Policy": {"IsAdministrator": False, "IsHidden": False}},
    {"Name": "Sarah", "Policy": {"IsAdministrator": False}},
    {"Name": "William", "Policy": {"IsAdministrator": False, "IsHidden": True}},   # hidden is fine
    {"Name": "Cried3200", "Policy": {"IsAdministrator": True, "IsHidden": True}},
    {"Name": "Old", "Policy": {"IsAdministrator": False, "IsDisabled": True}},
]


# --- the rules ---------------------------------------------------------------------

LINKABLE = ["eddy", "Sarah", "William"]


def test_a_free_account_can_be_taken():
    assert links.claim_problem("William", None, {"eddy"}, LINKABLE, owner=False) is None


def test_admin_and_disabled_accounts_are_the_owners():
    assert "only be linked by the server owner" in links.claim_problem("Cried3200", None, set(), LINKABLE, False)
    assert "only be linked by the server owner" in links.claim_problem("Old", None, set(), LINKABLE, False)


def test_someone_elses_account_is_refused():
    assert "someone else already" in links.claim_problem("eddy", None, {"eddy"}, LINKABLE, False)


def test_your_own_account_again_is_fine():
    assert links.claim_problem("Sarah", "sarah", {"sarah"}, LINKABLE, False) is None


def test_the_owner_may_link_anything():
    assert links.claim_problem("Cried3200", None, {"cried3200"}, LINKABLE, owner=True) is None


def test_notice_text():
    m = SimpleNamespace(id=SARAH, mention="<@2>")
    o = SimpleNamespace(id=OWNER, mention="<@1>")
    assert links.notice(m, "Sarah", None, "with the 👤 button") == "🔗 <@2> linked as **Sarah**, with the 👤 button"
    assert links.notice(m, "Sarah", "William", "with /link", by=o) == \
        "🔗 <@2> linked as **Sarah** (was **William**), with /link, by <@1>"


def test_linkable_names_skip_admins_and_disabled(monkeypatch):
    jf = jellyfin_client.JellyfinClient("http://jf", "key")

    async def fake_get(path):
        return USERS
    monkeypatch.setattr(jf, "_get", fake_get)
    assert asyncio.run(jf.get_linkable_user_names()) == ["eddy", "Sarah", "William"]


# --- /link -------------------------------------------------------------------------

class Jellyfin:
    async def get_user_names(self):
        return sorted((u["Name"] for u in USERS), key=str.lower)

    async def get_linkable_user_names(self):
        return [u["Name"] for u in USERS if not u["Policy"].get("IsAdministrator")
                and not u["Policy"].get("IsDisabled")]


class Channel:
    def __init__(self):
        self.sent = []

    async def send(self, text, allowed_mentions=None):
        self.sent.append(text)


class Response:
    def __init__(self):
        self.text = None

    async def send_message(self, text, ephemeral=False, allowed_mentions=None):
        self.text = text


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "bot.db"))
    monkeypatch.setattr(config, "ADMIN_CHANNEL_ID", 9)
    asyncio.run(watch_data.init_database())
    admin = Channel()

    async def is_owner(user):
        return user.id == OWNER
    bot = SimpleNamespace(jf=Jellyfin(), get_channel=lambda i: admin if i == 9 else None, is_owner=is_owner)
    cog = Achievements(bot)

    def link(caller, name, member=None):
        user = SimpleNamespace(id=caller, mention=f"<@{caller}>")
        interaction = SimpleNamespace(user=user, guild=SimpleNamespace(owner_id=OWNER), client=bot,
                                      response=Response())
        asyncio.run(cog.link_command.callback(cog, interaction, name, member))
        return interaction.response.text
    return link, admin


def test_a_member_cannot_take_the_admin_account(setup):
    link, admin = setup
    assert "only be linked by the server owner" in link(FRIEND, "Cried3200")
    assert asyncio.run(watch_data.get_link(FRIEND)) is None
    assert admin.sent == []


def test_a_member_cannot_take_a_claimed_account(setup):
    link, admin = setup
    link(SARAH, "Sarah")
    assert "someone else already" in link(FRIEND, "sarah")
    assert asyncio.run(watch_data.get_link(FRIEND)) is None
    assert admin.sent == ["🔗 <@2> linked as **Sarah**, with /link"]


def test_the_owner_can_link_anyone_and_it_is_noted(setup):
    link, admin = setup
    friend = SimpleNamespace(id=FRIEND, mention=f"<@{FRIEND}>")
    assert "is now **Cried3200**" in link(OWNER, "Cried3200", member=friend)
    assert asyncio.run(watch_data.get_link(FRIEND)) == "Cried3200"
    assert admin.sent == ["🔗 <@3> linked as **Cried3200**, with /link, by <@1>"]


def test_linking_again_to_the_same_account_is_quiet(setup):
    link, admin = setup
    link(SARAH, "Sarah")
    link(SARAH, "Sarah")
    assert len(admin.sent) == 1
