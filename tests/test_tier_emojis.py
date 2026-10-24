"""Uploading the tier badges as the bot's application emojis."""
import asyncio

import tiers
from cogs.achievements import sync_tier_emojis


class FakeEmoji:
    def __init__(self, name, id_, bot):
        self.name, self.id, self.bot = name, id_, bot

    def __str__(self):
        return f"<:{self.name}:{self.id}>"

    async def delete(self):
        self.bot.deleted.append(self.name)


class FakeBot:
    def __init__(self, existing=()):
        self.existing = [FakeEmoji(n, i, self) for i, n in enumerate(existing)]
        self.created, self.deleted = [], []

    async def fetch_application_emojis(self):
        return self.existing

    async def create_application_emoji(self, *, name, image):
        assert image.startswith(b"\x89PNG")
        self.created.append(name)
        return FakeEmoji(name, 1000 + len(self.created), self)


def test_first_start_uploads_all_twelve():
    bot = FakeBot()
    tags = asyncio.run(sync_tier_emojis(bot))
    assert len(bot.created) == 12 and bot.deleted == []
    assert set(tags) == set(tiers.wanted_emojis())


def test_restart_reuses_and_cleans_up():
    current = list(tiers.wanted_emojis())
    bot = FakeBot(existing=current + ["tier_corn_000000", "lock_gold_abcdef", "someone_elses"])
    tags = asyncio.run(sync_tier_emojis(bot))
    assert bot.created == []
    assert sorted(bot.deleted) == ["lock_gold_abcdef", "tier_corn_000000"]   # old art only
    assert tags[current[0]] == "<:%s:0>" % current[0]
