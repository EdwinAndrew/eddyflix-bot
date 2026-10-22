"""One message in the dashboard channel, edited every UPDATE_INTERVAL seconds."""
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks

import config
import parties


class Dashboard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.dashboard_message = None

    @commands.Cog.listener()
    async def on_services_ready(self):
        if self.bot.jf and config.DASHBOARD_CHANNEL_ID and not self.update_dashboard.is_running():
            self.update_dashboard.start()

    @tasks.loop(seconds=config.UPDATE_INTERVAL)
    async def update_dashboard(self):
        """Update the dashboard embed every config.UPDATE_INTERVAL seconds"""
        try:
            server_info = await self.bot.jf.get_server_info()
            sessions = await self.bot.jf.get_sessions()
            library_stats = await self.bot.jf.get_library_stats()

            uptime = datetime.now() - self.bot.start_time if self.bot.start_time else timedelta(0)
            uptime_str = f"{int(uptime.total_seconds() // 3600)}h {int((uptime.total_seconds() % 3600) // 60)}m"

            embed = discord.Embed(
                title="🎬 Jellyfin Server Dashboard",
                description=f"**{server_info.get('ServerName', 'Jellyfin')}** - {server_info.get('Version', 'Unknown')}",
                color=discord.Color.blue(),
                timestamp=datetime.now()
            )

            embed.add_field(
                name="📊 Server Status",
                value=f"🟢 Online\n⏱️ Uptime: {uptime_str}",
                inline=True
            )

            if library_stats:
                lib_text = []
                for lib in library_stats:
                    emoji = {"Movie": "🎬", "Series": "📺", "Music": "🎵"}.get(lib['type'], "📁")
                    lib_text.append(f"{emoji} {lib['name']}: {lib['count']} items")

                embed.add_field(
                    name="📚 Libraries",
                    value="\n".join(lib_text) if lib_text else "No libraries",
                    inline=True
                )

            active_streams = [s for s in sessions if s.get('NowPlayingItem')]
            embed.add_field(
                name=f"▶️ Active Streams ({len(active_streams)})",
                value="━━━━━━━━━━━━━━━━━━━",
                inline=False
            )

            # Who each stream is watching with, if it is part of a watch party
            together = {}
            for party in parties.find_parties(sessions).values():
                for session in sessions:
                    if session['Id'] in party['session_ids']:
                        together[session['Id']] = [u for u in party['users'] if u != session.get('UserName')]

            if active_streams:
                for session in active_streams:
                    user = session.get('UserName', 'Unknown')
                    device = session.get('DeviceName', 'Unknown')
                    item = session.get('NowPlayingItem', {})

                    if item.get('Type') == 'Episode':
                        title = f"{item.get('SeriesName', 'Unknown')} - S{item.get('ParentIndexNumber', '?')}E{item.get('IndexNumber', '?')}"
                        subtitle = item.get('Name', '')
                    else:
                        title = item.get('Name', 'Unknown')
                        subtitle = f"{item.get('Type', 'Media')}"

                    pos = session.get('PlayState', {}).get('PositionTicks', 0) // 10000000
                    runtime = item.get('RunTimeTicks', 0) // 10000000
                    progress = int((pos / runtime * 100)) if runtime else 0

                    is_paused = session.get('PlayState', {}).get('IsPaused', False)
                    status_icon = "⏸️" if is_paused else "▶️"

                    with_line = (f"\n🍿 with {', '.join(together[session['Id']])}"
                                 if together.get(session['Id']) else "")
                    embed.add_field(
                        name=f"{status_icon} {user} on {device}",
                        value=f"**{title}**\n{subtitle}\n`{'█' * (progress // 5)}{'░' * (20 - progress // 5)}` {progress}%{with_line}",
                        inline=False
                    )
            else:
                embed.add_field(
                    name="💤 No active streams",
                    value="All quiet on the media front",
                    inline=False
                )

            embed.set_footer(text=f"Last updated")

            channel = self.bot.get_channel(config.DASHBOARD_CHANNEL_ID)
            if not channel:
                print(f"ERROR: Could not find dashboard channel {config.DASHBOARD_CHANNEL_ID}")
                return

            if self.dashboard_message is None:
                async for msg in channel.history(limit=10):
                    if msg.author == self.bot.user and msg.embeds:
                        if "Jellyfin Server Dashboard" in msg.embeds[0].title:
                            self.dashboard_message = msg
                            break

            if self.dashboard_message:
                await self.dashboard_message.edit(embed=embed)
            else:
                self.dashboard_message = await channel.send(embed=embed)

        except Exception as e:
            print(f"Error updating dashboard: {e}")


async def setup(bot):
    await bot.add_cog(Dashboard(bot))
