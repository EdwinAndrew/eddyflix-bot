"""#roles for newcomers: the Jellyfin link menu and the getting-started guide."""
from cogs import movie_night as mn
import config


def test_link_menu_lists_only_free_users():
    free, more = mn.link_choices(["eddy", "Alice", "bob", "Carol"], linked={"eddy", "carol"})
    assert free == ["Alice", "bob"] and not more


def test_link_menu_fits_one_select():
    names = [f"user{i:02d}" for i in range(30)]
    free, more = mn.link_choices(names, linked={"user00"})
    assert len(free) == 25 and free[0] == "user01" and more


def test_link_prompt():
    assert "Pick yourself below" in mn.link_prompt("eddy", ["bob"], False)
    assert "Ask the server owner" in mn.link_prompt("eddy", [], False)
    assert mn.link_prompt(None, ["bob"], False).startswith("Which Jellyfin user are you?")
    assert "linked to someone already" in mn.link_prompt(None, [], False)
    assert "/jf-link" in mn.link_prompt(None, ["bob"], True)


def guide_text(embed):
    return "\n".join([embed.description or ""] + [f"{f.name}\n{f.value}" for f in embed.fields])


def test_guide_with_both_addresses(monkeypatch):
    monkeypatch.setattr(config, "JELLYFIN_PUBLIC_URL", "https://eddyflix.duckdns.org:8443")
    monkeypatch.setattr(config, "SEERR_PUBLIC_URL", "https://eddyflix.duckdns.org:5056")
    embed = mn.guide_embed(owner_id=42)
    text = guide_text(embed)
    assert embed.title == mn.GUIDE_TITLE
    assert "<@42>" in text and mn.DIRECTOR_ROLE in text
    assert "https://eddyflix.duckdns.org:8443" in text and "port and all" in text
    assert "Seerr: **https://eddyflix.duckdns.org:5056**" in text
    assert "Jellyfin and Seerr only answer from Canada" in text
    for word in ("LG TV", "webOS", "iPhone", "Browser", mn.LINK_LABEL, "/watch", "/jf-subscribe", "/jf-achievements"):
        assert word in text, word
    assert all(len(f.value) <= 1024 for f in embed.fields) and len(embed) <= 6000


def test_guide_without_seerr_or_address(monkeypatch):
    monkeypatch.setattr(config, "JELLYFIN_PUBLIC_URL", "")
    monkeypatch.setattr(config, "SEERR_PUBLIC_URL", "")
    text = guide_text(mn.guide_embed())
    assert "Seerr" not in text
    assert "Jellyfin only answers from Canada" in text
    assert "the server owner" in text and "Ask for the server address" in text
