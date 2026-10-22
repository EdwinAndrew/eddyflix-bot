#!/usr/bin/env python3
"""EddyFlix Bot: entry point. The features live in cogs/, one file each."""
import asyncio
from datetime import datetime

import discord
from discord.ext import commands

import config
import watch_data
from checks import on_app_command_error
from cogs import movie_night
from jellyfin_client import JellyfinClient

# Loaded in this order. Each module has an async setup(bot) that adds its cog.
EXTENSIONS = [
    "cogs.health",
    "cogs.dashboard",
    "cogs.activity",
    "cogs.digest",
    "cogs.server",
    "cogs.subscriptions",
    "cogs.achievements",
    "cogs.movie_night",
    "cogs.help",
]


class EddyFlixBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        super().__init__(command_prefix='!jf ', intents=intents)
        self.jf = JellyfinClient(config.JELLYFIN_URL, config.JELLYFIN_API_KEY) if config.JELLYFIN_API_KEY else None
        self.start_time: datetime | None = None
        self.tree.on_error = on_app_command_error

    async def setup_hook(self):
        for extension in EXTENSIONS:
            await self.load_extension(extension)
        # Re-attach the #roles picker's buttons, which outlive the process.
        self.add_view(movie_night.RolePicker())

    async def on_ready(self):
        self.start_time = datetime.now()
        print(f'Bot connected as {self.user}')
        print(f'Dashboard channel: {config.DASHBOARD_CHANNEL_ID}')
        print(f'Activity channel: {config.ACTIVITY_CHANNEL_ID}')

        await watch_data.init_database()
        if self.jf:
            # One-off: drop "Finished" achievements the old guess handed out (#24)
            await watch_data.recheck_series_completions(self.jf)

        try:
            synced = await self.tree.sync()
            print(f'Synced {len(synced)} slash command(s)')
        except Exception as e:
            print(f'Failed to sync commands: {e}')

        # Movie night roles, channels and the #roles picker: created if missing
        try:
            await movie_night.ensure_structure(self)
        except Exception as e:
            print(f'Movie night setup failed: {e}')

        # Wait 10 seconds before starting activity tracking (avoid startup spam)
        await asyncio.sleep(10)

        # The cogs start their loops on this event. on_ready fires again after
        # every reconnect, so each one checks that its loop is not already running.
        self.dispatch("services_ready")

    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return
        print(f"Command error: {error}")

    async def close(self):
        """Release the HTTP sessions before shutting down."""
        if self.jf is not None:
            await self.jf.close()
        for cog in self.cogs.values():
            if hasattr(cog, "aclose"):
                await cog.aclose()
        await super().close()


def main():
    if not config.DISCORD_TOKEN:
        print("ERROR: DISCORD_TOKEN not set!")
        exit(1)
    if not config.JELLYFIN_API_KEY:
        print("ERROR: JELLYFIN_API_KEY not set!")
        exit(1)
    EddyFlixBot().run(config.DISCORD_TOKEN)


if __name__ == "__main__":
    main()
