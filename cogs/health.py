"""The heartbeat the container's health check reads, and the optional Uptime Kuma push."""
from datetime import datetime

import aiohttp
from discord.ext import commands, tasks

import config

HEARTBEAT_FILE = "/tmp/bot_heartbeat"
# How long authenticated Jellyfin calls may keep failing before the heartbeat
# stops. Long enough to ride out a Jellyfin restart for an update.
JELLYFIN_STALE_SECONDS = 300


class Health(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.jellyfin_stale_reported = False
        # Its own session: the Jellyfin client's carries the API key in its headers.
        self.push_session: aiohttp.ClientSession | None = None

    @commands.Cog.listener()
    async def on_services_ready(self):
        if not self.heartbeat.is_running():
            self.heartbeat.start()

    async def aclose(self):
        if self.push_session is not None and not self.push_session.closed:
            await self.push_session.close()

    async def push_uptime_kuma(self):
        if not config.UPTIME_KUMA_PUSH_URL:
            return
        if self.push_session is None or self.push_session.closed:
            self.push_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        try:
            async with self.push_session.get(config.UPTIME_KUMA_PUSH_URL) as resp:
                if resp.status != 200:
                    print(f"Uptime Kuma push failed: {resp.status}")
        except Exception as e:
            print(f"Uptime Kuma push failed: {e}")

    def _polling(self) -> bool:
        """Only the polling loops call Jellyfin on a schedule, so without them
        there is nothing to judge freshness by."""
        activity, dashboard = self.bot.get_cog("Activity"), self.bot.get_cog("Dashboard")
        return bool((activity and activity.check_sessions.is_running())
                    or (dashboard and dashboard.update_dashboard.is_running()))

    @tasks.loop(seconds=30)
    async def heartbeat(self):
        """Touch a file so the container healthcheck can detect a wedged bot.

        Restart-on-exit only catches a crashed process; this also catches the
        case where the process is alive but no longer connected to Discord, or
        connected but unable to reach Jellyfin. The second case ran unnoticed for
        six days in October 2026: every call got a 401 while the container stayed
        healthy.
        """
        if self.bot.is_closed():
            return

        jf = self.bot.jf
        if jf and self._polling():
            stale = jf.seconds_since_auth_success()
            if stale > JELLYFIN_STALE_SECONDS:
                if not self.jellyfin_stale_reported:
                    print(f"Heartbeat withheld: no successful Jellyfin call for {int(stale)}s")
                    self.jellyfin_stale_reported = True
                return
            if self.jellyfin_stale_reported:
                print("Heartbeat resumed: Jellyfin calls succeeding again")
                self.jellyfin_stale_reported = False

        try:
            with open(HEARTBEAT_FILE, "w") as fh:
                fh.write(str(datetime.now()))
        except OSError as e:
            print(f"Could not write heartbeat: {e}")
        await self.push_uptime_kuma()


async def setup(bot):
    await bot.add_cog(Health(bot))
