"""The weekly "New on EddyFlix" post: Sundays just after the digest, in the same
channel, or now with /admin weekly-new (owner only). Skipped on a week with nothing new
(upgraded files alone are not new). Ends with a line pointing to /request and Seerr,
when Seerr is set up.

Also keeps the library record that tells upgrades from new things up to date,
every hour (library_seen.py). The first run notes the whole library."""
import io
from datetime import datetime, time, timezone

import aiosqlite
import discord
from discord.ext import commands, tasks

import config
import library_seen
import new_this_week as nw
from cogs.digest import DIGEST_TIME
from cogs.movie_night import TZ, details_link

# A minute after the digest, so the two always come in the same order.
NEW_TIME = time(DIGEST_TIME.hour, DIGEST_TIME.minute + 1, tzinfo=DIGEST_TIME.tzinfo)


class WeeklyNew(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def cog_unload(self):
        self.weekly.cancel()
        self.library_sync.cancel()

    @commands.Cog.listener()
    async def on_services_ready(self):
        if self.bot.jf and config.ACTIVITY_CHANNEL_ID and not self.weekly.is_running():
            self.weekly.start()
        if self.bot.jf and not self.library_sync.is_running():
            self.library_sync.start()

    @tasks.loop(hours=1)
    async def library_sync(self):
        """The library record, so a file Sonarr replaces later is known as an upgrade.
        The first run (right after start) notes the whole library."""
        try:
            async with aiosqlite.connect(config.DB_PATH) as db:
                fresh = await library_seen.sync(db, self.bot.jf)
            if fresh:
                print(f"Library record: {fresh} new films or episodes noted")
        except Exception as e:
            print(f"Library record: could not sync: {e}")

    async def gather(self, now: datetime) -> list[nw.Entry]:
        jf, cut = self.bot.jf, nw.cutoff(now)
        films = await nw.added_since(lambda s, n: jf.added_by_date("Movie", s, n), cut)
        series = await nw.added_since(lambda s, n: jf.added_by_date("Series", s, n), cut)
        episodes = await nw.added_since(jf.episodes_by_date_added, cut)
        try:
            async with aiosqlite.connect(config.DB_PATH) as db:
                await library_seen.sync(db, jf, now)
                upgraded = await library_seen.upgrades(
                    db, jf, films + episodes, cut, {s["Id"]: library_seen.series_keys(s) for s in series})
                await db.commit()
        except Exception as e:
            # Better a post that calls an upgrade new than no post
            print(f"New on EddyFlix: could not tell upgrades apart: {e}")
            upgraded = set()
        return nw.collect(films, series, episodes, upgraded)

    async def build(self, now: datetime | None = None):
        """(embed, file) for the post, or None when nothing was added."""
        now = now or datetime.now(timezone.utc)
        entries = await self.gather(now)
        if not nw.has_new(entries):
            return None
        start, end = nw.cutoff(now).astimezone(TZ), now.astimezone(TZ)
        shown, _ = nw.tiles(entries)
        posters = {}
        for entry in shown:
            try:
                posters[entry.item_id] = await self.bot.jf.get_image(entry.item_id, "Primary", max_width=400)
            except Exception as e:
                print(f"New on EddyFlix: no poster for {entry.name}: {e}")
        png = nw.render_grid(entries, posters, start, end)
        embed = discord.Embed(title="🆕 New on EddyFlix this week",
                              description=nw.description(entries, details_link, start, end,
                                                         nw.request_line(bool(config.SEERR_URL and config.SEERR_API_KEY),
                                                                         config.SEERR_PUBLIC_URL)),
                              colour=discord.Colour(nw.COLOUR))
        embed.set_image(url="attachment://new-this-week.png")
        embed.set_footer(text=nw.counts(entries))
        return embed, discord.File(io.BytesIO(png), filename="new-this-week.png")

    async def post(self) -> str:
        """Post to the activity channel. Says what happened, for the command."""
        channel = self.bot.get_channel(config.ACTIVITY_CHANNEL_ID)
        if channel is None:
            return "The activity channel is not set or not found."
        built = await self.build()
        if built is None:
            print("New on EddyFlix: nothing new this week, not posted")
            return "Nothing new was added in the last 7 days (upgraded files don't count), so nothing was posted."
        embed, file = built
        await channel.send(embed=embed, file=file)
        print("New on EddyFlix posted")
        return f"Posted in {channel.mention}."

    @tasks.loop(time=NEW_TIME)
    async def weekly(self):
        if datetime.now().weekday() == 6:  # Sunday
            try:
                await self.post()
            except Exception as e:
                print(f"New on EddyFlix: {e}")

    async def post_now(self, interaction: discord.Interaction):
        """/admin weekly-new"""
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            message = await self.post()
        except Exception as e:
            print(f"New on EddyFlix: {e}")
            message = f"Could not post it: {e}"
        await interaction.followup.send(message, ephemeral=True)


async def setup(bot):
    await bot.add_cog(WeeklyNew(bot))
