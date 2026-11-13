"""The weekly admin summary in ADMIN_CHANNEL_ID: Sundays at 6 PM, or now with
/jf-admin-summary (owner only). What goes in it is in admin_summary.py."""
import os
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

import admin_summary
import config
from checks import owner_only

SUMMARY_TIME = time(hour=18, minute=0, tzinfo=ZoneInfo(os.getenv("TZ", "America/New_York")))
MAX_PAGES = 20     # pages of 100 when looking back a week; a library rebuild could list everything


class Admin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_services_ready(self):
        if config.ADMIN_CHANNEL_ID and not self.weekly_summary.is_running():
            self.weekly_summary.start()

    async def added_this_week(self, fetch, since: datetime) -> list[dict] | None:
        """Everything a newest-first Jellyfin listing has added since then, or None
        if Jellyfin does not answer."""
        found = []
        try:
            for page in range(MAX_PAGES):
                items = await fetch(start=page * 100, limit=100)
                newer, done = admin_summary.added_since(items, since)
                found += newer
                if done or len(items) < 100:
                    break
        except Exception as e:
            print(f"Admin summary: could not list what was added: {e}")
            return None
        return found

    async def sections(self, now: datetime) -> list[tuple[str, str]]:
        """The summary, one embed field each. Sonarr, Radarr and Seerr would each
        add one here once the bot has their API keys."""
        start, _ = admin_summary.week_window(now)
        async with aiosqlite.connect(config.DB_PATH) as db:
            plays = await admin_summary.plays_since(db, start)
            linked = await admin_summary.linked_users(db)
            open_plays = await admin_summary.open_plays(db, now - timedelta(days=1))

        jf = self.bot.jf
        since_utc = datetime.now(timezone.utc) - timedelta(days=7)
        films = episodes = users = system = storage = None
        if jf is not None:
            films = await self.added_this_week(jf.movies_by_date_added, since_utc)
            episodes = await self.added_this_week(jf.episodes_by_date_added, since_utc)
            try:
                users = await jf.get_user_names()
            except Exception as e:
                print(f"Admin summary: no Jellyfin users: {e}")
            system = await jf.get_system_info() or None
            try:
                storage = await jf.get_storage()
            except Exception as e:
                print(f"Admin summary: no storage info: {e}")

        health_cog = self.bot.get_cog("Health")
        health = {
            "bot_started": getattr(health_cog, "started", None),
            "jellyfin_outages": getattr(health_cog, "jellyfin_outages", 0),
            "system": system,
            "storage": storage,
            "open_plays": open_plays,
        }
        return [
            ("▶️ Playback", admin_summary.playback_section(plays)),
            ("📚 Library", admin_summary.library_section(films, episodes)),
            ("👥 People", admin_summary.people_section(plays, users, linked)),
            ("🩺 Health", admin_summary.health_section(health, now)),
        ]

    async def build(self) -> discord.Embed:
        now = datetime.now()
        start, end = admin_summary.week_window(now)
        return admin_summary.summary_embed(await self.sections(now), start, end)

    @tasks.loop(time=SUMMARY_TIME)
    async def weekly_summary(self):
        if datetime.now().weekday() != 6:  # Sundays
            return
        channel = self.bot.get_channel(config.ADMIN_CHANNEL_ID)
        if channel is None:
            print("Admin summary: could not find the admin channel")
            return
        try:
            await channel.send(embed=await self.build())
        except Exception as e:
            print(f"Error sending the admin summary: {e}")

    @app_commands.command(name="jf-admin-summary", description="(Owner) Post the weekly admin summary now")
    @owner_only()
    async def admin_summary_command(self, interaction: discord.Interaction):
        """In the admin channel, or only to you when there is none."""
        await interaction.response.defer(ephemeral=True)
        embed = await self.build()
        channel = self.bot.get_channel(config.ADMIN_CHANNEL_ID) if config.ADMIN_CHANNEL_ID else None
        if channel is None:
            await interaction.followup.send("No admin channel is set (`ADMIN_CHANNEL_ID`), so here it is:",
                                            embed=embed, ephemeral=True)
            return
        await channel.send(embed=embed)
        await interaction.followup.send(f"Posted in {channel.mention}.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Admin(bot))
