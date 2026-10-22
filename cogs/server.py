"""Server and library commands: /jf-health, /jf-stats, /jf-status, /jf-recent,
/jf-stats-graph and /jf-popular."""
import io
import shutil
from datetime import datetime, timedelta

import aiosqlite
import discord
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import psutil
from discord import app_commands
from discord.ext import commands

import config


class Server(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="jf-health", description="Show server health and resource usage")
    async def health_command(self, interaction: discord.Interaction):
        """Slash command to check server health"""
        await interaction.response.defer()

        try:
            # Get system stats
            cpu_percent = psutil.cpu_percent(interval=1)
            memory = psutil.virtual_memory()
            # /app/data is bind-mounted from the media drive, so this reports the
            # disk that actually matters. "/" would report the container's overlay
            # filesystem, where a low-space warning could never fire.
            disk = shutil.disk_usage(config.DB_PATH.rsplit("/", 1)[0])

            # Get Jellyfin system info if available
            system_info = await self.bot.jf.get_system_info()

            embed = discord.Embed(
                title="🏥 Server Health Status",
                color=discord.Color.green() if cpu_percent < 80 and memory.percent < 80 else discord.Color.orange(),
                timestamp=datetime.now()
            )

            # CPU
            cpu_emoji = "🟢" if cpu_percent < 60 else "🟡" if cpu_percent < 80 else "🔴"
            embed.add_field(
                name=f"{cpu_emoji} CPU Usage",
                value=f"**{cpu_percent}%**",
                inline=True
            )

            # Memory
            mem_emoji = "🟢" if memory.percent < 60 else "🟡" if memory.percent < 80 else "🔴"
            mem_gb = memory.used / (1024 ** 3)
            mem_total_gb = memory.total / (1024 ** 3)
            embed.add_field(
                name=f"{mem_emoji} Memory Usage",
                value=f"**{memory.percent}%**\n{mem_gb:.1f} / {mem_total_gb:.1f} GB",
                inline=True
            )

            # Disk
            disk_percent = (disk.used / disk.total) * 100
            disk_emoji = "🟢" if disk_percent < 70 else "🟡" if disk_percent < 85 else "🔴"
            disk_gb = disk.used / (1024 ** 3)
            disk_total_gb = disk.total / (1024 ** 3)
            embed.add_field(
                name=f"{disk_emoji} Disk Usage",
                value=f"**{disk_percent:.1f}%**\n{disk_gb:.1f} / {disk_total_gb:.1f} GB",
                inline=True
            )

            # Check sessions for transcode load
            sessions = await self.bot.jf.get_sessions()
            transcoding = len([s for s in sessions if s.get('PlayState', {}).get('PlayMethod') == 'Transcode'])

            if transcoding > 0:
                embed.add_field(
                    name="🔄 Active Transcodes",
                    value=f"**{transcoding}** session(s) transcoding",
                    inline=True
                )

            # A Docker container-count field was removed here: the 'docker' package
            # was never in requirements.txt and this container has no docker.sock
            # mount, so it could only ever raise. Restoring it would mean giving a
            # Discord bot root-equivalent access to the host - not worth one field.
            # Portainer and Uptime Kuma already cover container status.

            # Warnings
            warnings = []
            if cpu_percent > 80:
                warnings.append("⚠️ High CPU usage detected")
            if memory.percent > 80:
                warnings.append("⚠️ High memory usage detected")
            if disk_percent > 85:
                warnings.append("⚠️ Disk space running low")

            if warnings:
                embed.add_field(name="⚠️ Warnings", value="\n".join(warnings), inline=False)

            embed.set_footer(text="Jellyfin MediaServer Health Check")
            await interaction.followup.send(embed=embed)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}")

    @app_commands.command(name="jf-stats", description="Show Jellyfin server statistics")
    async def stats_command(self, interaction: discord.Interaction):
        """Slash command to get server stats"""
        await interaction.response.defer()

        try:
            server_info = await self.bot.jf.get_server_info()
            sessions = await self.bot.jf.get_sessions()
            library_stats = await self.bot.jf.get_library_stats()

            embed = discord.Embed(
                title="📊 Server Statistics",
                color=discord.Color.blue()
            )

            embed.add_field(name="Server", value=server_info.get('ServerName', 'Jellyfin'), inline=True)
            embed.add_field(name="Version", value=server_info.get('Version', 'Unknown'), inline=True)
            embed.add_field(name="Active Sessions", value=len(sessions), inline=True)

            if library_stats:
                for lib in library_stats:
                    embed.add_field(name=f"📁 {lib['name']}", value=f"{lib['count']} items", inline=True)

            await interaction.followup.send(embed=embed)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}")

    @app_commands.command(name="jf-status", description="Show detailed Jellyfin server status with active streams")
    async def status_command(self, interaction: discord.Interaction):
        """Slash command to get comprehensive server status"""
        await interaction.response.defer()

        try:
            server_info = await self.bot.jf.get_server_info()
            sessions = await self.bot.jf.get_sessions()
            library_stats = await self.bot.jf.get_library_stats()

            # Calculate bot uptime
            uptime = datetime.now() - self.bot.start_time if self.bot.start_time else timedelta(0)
            uptime_str = f"{int(uptime.total_seconds() // 3600)}h {int((uptime.total_seconds() % 3600) // 60)}m"

            # Build main embed
            embed = discord.Embed(
                title="🎬 Jellyfin Server Status",
                description=f"**{server_info.get('ServerName', 'Jellyfin')}** - {server_info.get('Version', 'Unknown')}",
                color=discord.Color.green(),
                timestamp=datetime.now()
            )

            # Server status
            embed.add_field(
                name="📊 Server",
                value=f"🟢 Online\n⏱️ Uptime: {uptime_str}",
                inline=True
            )

            # Active users
            total_sessions = len(sessions)
            active_streams = [s for s in sessions if s.get('NowPlayingItem')]
            embed.add_field(
                name="👥 Users",
                value=f"Connected: {total_sessions}\nStreaming: {len(active_streams)}",
                inline=True
            )

            # Library stats
            if library_stats:
                lib_text = []
                for lib in library_stats:
                    emoji = {"Movie": "🎬", "Series": "📺", "Music": "🎵"}.get(lib['type'], "📁")
                    lib_text.append(f"{emoji} {lib['name']}: **{lib['count']}**")

                embed.add_field(
                    name="📚 Libraries",
                    value="\n".join(lib_text) if lib_text else "No libraries",
                    inline=True
                )

            # Active streams section
            embed.add_field(
                name=f"▶️ Active Streams ({len(active_streams)})",
                value="━━━━━━━━━━━━━━━━━━━",
                inline=False
            )

            if active_streams:
                for session in active_streams:
                    user = session.get('UserName', 'Unknown')
                    device = session.get('DeviceName', 'Unknown')
                    item = session.get('NowPlayingItem', {})

                    # Format title based on media type
                    if item.get('Type') == 'Episode':
                        title = f"{item.get('SeriesName', 'Unknown')} - S{item.get('ParentIndexNumber', '?')}E{item.get('IndexNumber', '?')}"
                        subtitle = item.get('Name', '')
                    else:
                        title = item.get('Name', 'Unknown')
                        subtitle = f"{item.get('Type', 'Media')}"

                    # Calculate progress
                    pos = session.get('PlayState', {}).get('PositionTicks', 0) // 10000000
                    runtime = item.get('RunTimeTicks', 0) // 10000000
                    progress = int((pos / runtime * 100)) if runtime else 0

                    # Format time
                    pos_str = f"{pos // 60}:{pos % 60:02d}"
                    runtime_str = f"{runtime // 60}:{runtime % 60:02d}"

                    # Status icon
                    is_paused = session.get('PlayState', {}).get('IsPaused', False)
                    status_icon = "⏸️" if is_paused else "▶️"

                    # Transcoding info
                    play_method = session.get('PlayState', {}).get('PlayMethod', 'Unknown')
                    transcode_info = ""
                    if play_method == "Transcode":
                        transcode_info = " 🔄 Transcoding"
                    elif play_method == "DirectPlay":
                        transcode_info = " ⚡ Direct Play"

                    embed.add_field(
                        name=f"{status_icon} {user} on {device}",
                        value=f"**{title}**\n{subtitle}\n`{'█' * (progress // 5)}{'░' * (20 - progress // 5)}` {progress}%\n⏱️ {pos_str} / {runtime_str}{transcode_info}",
                        inline=False
                    )
            else:
                embed.add_field(
                    name="💤 No active streams",
                    value="All quiet on the media front",
                    inline=False
                )

            # Connected users (not streaming)
            idle_sessions = [s for s in sessions if not s.get('NowPlayingItem')]
            if idle_sessions:
                idle_users = [f"• {s.get('UserName', 'Unknown')} ({s.get('DeviceName', 'Unknown')})" for s in idle_sessions[:5]]
                embed.add_field(
                    name=f"💤 Idle Users ({len(idle_sessions)})",
                    value="\n".join(idle_users) if idle_users else "None",
                    inline=False
                )

            embed.set_footer(text="Jellyfin MediaServer")

            await interaction.followup.send(embed=embed)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}")

    @app_commands.command(name="jf-recent", description="Show recently added content")
    async def recent_command(self, interaction: discord.Interaction):
        """Slash command to show recently added items"""
        await interaction.response.defer()

        try:
            items = await self.bot.jf.get_recently_added(limit=10)

            if not items:
                await interaction.followup.send("No recently added items found.")
                return

            embed = discord.Embed(
                title="📅 Recently Added Content",
                description=f"Last {len(items)} items added to the library",
                color=discord.Color.purple(),
                timestamp=datetime.now()
            )

            for item in items[:10]:
                item_type = item.get('Type', 'Unknown')
                item_name = item.get('Name', 'Unknown')

                # Format based on type
                if item_type == 'Episode':
                    series_name = item.get('SeriesName', 'Unknown')
                    season = item.get('ParentIndexNumber', '?')
                    episode = item.get('IndexNumber', '?')
                    title_text = f"📺 {series_name} - S{season}E{episode}"
                    subtitle = item_name
                elif item_type == 'Movie':
                    title_text = f"🎬 {item_name}"
                    year = item.get('ProductionYear', '')
                    subtitle = f"({year})" if year else "Movie"
                else:
                    title_text = f"📁 {item_name}"
                    subtitle = item_type

                # Add date added
                date_created = item.get('DateCreated', '')
                if date_created:
                    try:
                        dt = datetime.fromisoformat(date_created.replace('Z', '+00:00'))
                        time_ago = datetime.now(dt.tzinfo) - dt
                        if time_ago.days > 0:
                            time_str = f"{time_ago.days}d ago"
                        elif time_ago.seconds // 3600 > 0:
                            time_str = f"{time_ago.seconds // 3600}h ago"
                        else:
                            time_str = f"{time_ago.seconds // 60}m ago"
                    except (ValueError, TypeError, AttributeError):
                        time_str = "Recently"
                else:
                    time_str = "Recently"

                embed.add_field(
                    name=title_text,
                    value=f"{subtitle}\n🕒 {time_str}",
                    inline=True
                )

            embed.set_footer(text="Jellyfin MediaServer")
            await interaction.followup.send(embed=embed)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}")

    @app_commands.command(name="jf-stats-graph", description="Show watch time statistics with charts")
    async def stats_graph_command(self, interaction: discord.Interaction, days: int = 7):
        """Generate watch time charts"""
        await interaction.response.defer()

        try:
            async with aiosqlite.connect(config.DB_PATH) as db:
                # Get watch history for the last N days
                cutoff = datetime.now() - timedelta(days=days)
                cutoff_str = cutoff.isoformat()

                # Use substr(start_time,1,10) to safely extract YYYY-MM-DD regardless of
                # whether the timestamp contains a 'T' or a space separator, and pass
                # an ISO-formatted cutoff string for reliable comparisons.
                # Only count completed sessions (duration_seconds IS NOT NULL)
                async with db.execute("""
                    SELECT substr(start_time,1,10) as date,
                           SUM(duration_seconds) as total_seconds
                    FROM watch_history
                    WHERE start_time >= ? AND duration_seconds IS NOT NULL
                    GROUP BY substr(start_time,1,10)
                    ORDER BY date
                """, (cutoff_str,)) as cursor:
                    rows = await cursor.fetchall()

            if not rows:
                await interaction.followup.send(f"No watch history found for the last {days} days.")
                return

            # Prepare data for matplotlib
            dates = [datetime.strptime(row[0], '%Y-%m-%d') for row in rows]
            hours = [row[1] / 3600 for row in rows]  # Convert seconds to hours

            # Create chart
            plt.figure(figsize=(12, 6))
            plt.bar(dates, hours, color='#7289DA', edgecolor='black', linewidth=0.5)
            plt.xlabel('Date', fontsize=12)
            plt.ylabel('Watch Time (hours)', fontsize=12)
            plt.title(f'Watch Time - Last {days} Days', fontsize=14, fontweight='bold')
            plt.grid(axis='y', alpha=0.3)

            # Format x-axis
            plt.gca().xaxis.set_major_formatter(mdates.DateFormatter('%m/%d'))
            plt.gca().xaxis.set_major_locator(mdates.DayLocator())
            plt.xticks(rotation=45)
            plt.tight_layout()

            # Save to BytesIO
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
            buf.seek(0)
            plt.close()

            # Calculate totals
            total_hours = sum(hours)
            avg_hours = total_hours / len(hours) if hours else 0

            embed = discord.Embed(
                title=f"📊 Watch Statistics - Last {days} Days",
                description=f"**Total Watch Time:** {total_hours:.1f} hours\n**Average per Day:** {avg_hours:.1f} hours",
                color=discord.Color.blue(),
                timestamp=datetime.now()
            )

            file = discord.File(buf, filename="watch_stats.png")
            embed.set_image(url="attachment://watch_stats.png")
            embed.set_footer(text="Jellyfin MediaServer")

            await interaction.followup.send(embed=embed, file=file)

        except Exception as e:
            await interaction.followup.send(f"❌ Error generating stats: {e}")

    @app_commands.command(name="jf-popular", description="Show most popular content based on watch time")
    async def popular_command(self, interaction: discord.Interaction, limit: int = 10):
        """Show most watched content - aggregates TV shows by series"""
        await interaction.response.defer()

        try:
            async with aiosqlite.connect(config.DB_PATH) as db:
                # Get aggregated watch data - groups episodes by series name
                async with db.execute("""
                    SELECT 
                        CASE 
                            WHEN item_type = 'Episode' THEN series_name
                            ELSE item_name
                        END as display_name,
                        CASE 
                            WHEN item_type = 'Episode' THEN 'Series'
                            ELSE item_type
                        END as display_type,
                        SUM(total_watch_time) as total_time,
                        SUM(watch_count) as total_count
                    FROM popular_content
                    GROUP BY 
                        CASE 
                            WHEN item_type = 'Episode' THEN series_name
                            ELSE item_name
                        END
                    ORDER BY total_time DESC
                    LIMIT ?
                """, (limit,)) as cursor:
                    rows = await cursor.fetchall()

            if not rows:
                await interaction.followup.send("No watch history available yet.")
                return

            embed = discord.Embed(
                title=f"🏆 Top {len(rows)} Most Popular Content",
                description="Based on total watch time (TV shows aggregated by series)",
                color=discord.Color.gold(),
                timestamp=datetime.now()
            )

            for i, (name, item_type, total_time, count) in enumerate(rows, 1):
                hours = total_time / 3600
                emoji = {"Movie": "🎬", "Series": "📺", "Episode": "📺"}.get(item_type, "📁")

                embed.add_field(
                    name=f"{i}. {emoji} {name}",
                    value=f"⏱️ {hours:.1f}h total • 👥 {count} episodes watched",
                    inline=False
                )

            embed.set_footer(text="Jellyfin MediaServer")
            await interaction.followup.send(embed=embed)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}")


async def setup(bot):
    await bot.add_cog(Server(bot))
