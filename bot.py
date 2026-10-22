#!/usr/bin/env python3
import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiohttp
import os
import asyncio
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from jellyfin_client import JellyfinClient
import movie_night
from PIL import Image, ImageDraw, ImageFont
import io
import psutil
import shutil
import matplotlib
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import aiosqlite
from typing import List, Dict


def generate_badge(title: str, subtitle: str = ""):
    """Generate a simple PNG badge for an achievement and return BytesIO"""
    img = Image.new('RGBA', (600, 200), (40, 44, 52, 255))
    draw = ImageDraw.Draw(img)
    # arial.ttf does not exist in python:*-slim, so this always fell back to
    # load_default() - a bitmap font far too small to read on a 600x200 image.
    # DejaVu ships with matplotlib, which is already a dependency.
    font = ImageFont.load_default()
    for candidate in (
        f"{matplotlib.get_data_path()}/fonts/ttf/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        try:
            font = ImageFont.truetype(candidate, 28)
            break
        except Exception:
            continue

    # Title
    draw.text((20, 30), title, font=font, fill=(255, 215, 0))
    # Subtitle
    draw.text((20, 90), subtitle, font=font, fill=(255, 255, 255))

    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf

# Configuration from environment variables
JELLYFIN_URL = os.getenv("JELLYFIN_URL", "http://jellyfin:8096")
JELLYFIN_API_KEY = os.getenv("JELLYFIN_API_KEY")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DASHBOARD_CHANNEL_ID = int(os.getenv("DASHBOARD_CHANNEL_ID", "0"))
ACTIVITY_CHANNEL_ID = int(os.getenv("ACTIVITY_CHANNEL_ID", "0"))
UPDATE_INTERVAL = int(os.getenv("UPDATE_INTERVAL", "60"))
# Achievement thresholds configurable via env var (comma-separated hours)
ACHIEVEMENT_THRESHOLDS = os.getenv('ACHIEVEMENT_THRESHOLDS', '10,50,100,250,500,1000')
try:
    ACHIEVEMENT_MILESTONES = [int(x) for x in ACHIEVEMENT_THRESHOLDS.split(',') if x.strip()]
except Exception:
    ACHIEVEMENT_MILESTONES = [10,50,100,250,500,1000]

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True


class MediaServerBot(commands.Bot):
    async def setup_hook(self):
        # Re-attach the #roles picker's buttons, which outlive the process.
        self.add_view(movie_night.RolePicker())

    async def close(self):
        """Release the shared Jellyfin HTTP session before shutting down."""
        if jf is not None:
            await jf.close()
        if push_session is not None and not push_session.closed:
            await push_session.close()
        await super().close()


bot = MediaServerBot(command_prefix='!jf ', intents=intents)

# Jellyfin client
jf = JellyfinClient(JELLYFIN_URL, JELLYFIN_API_KEY) if JELLYFIN_API_KEY else None

# /movienight, /movievote and the #roles picker
movie_night.register(bot, jf)

# State tracking
active_sessions = {}
sessions_seeded = False   # set after the first session poll; see check_sessions
dashboard_message = None
server_start_time = None
db_path = "/app/data/jellyfin_bot.db"

# Database functions
async def init_database():
    """Initialize SQLite database with tables for watch history and subscriptions"""
    async with aiosqlite.connect(db_path) as db:
        # Watch history table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS watch_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                user_name TEXT NOT NULL,
                item_id TEXT NOT NULL,
                item_name TEXT NOT NULL,
                item_type TEXT NOT NULL,
                series_name TEXT,
                season_number INTEGER,
                episode_number INTEGER,
                start_time TIMESTAMP NOT NULL,
                end_time TIMESTAMP,
                duration_seconds INTEGER,
                play_method TEXT,
                device_name TEXT
            )
        """)

        # Subscriptions table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_user_id TEXT NOT NULL,
                discord_username TEXT NOT NULL,
                show_name TEXT NOT NULL,
                show_id TEXT,
                subscribed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(discord_user_id, show_name)
            )
        """)

        # Popular content cache table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS popular_content (
                item_id TEXT PRIMARY KEY,
                item_name TEXT NOT NULL,
                item_type TEXT NOT NULL,
                series_name TEXT,
                total_watch_time INTEGER DEFAULT 0,
                watch_count INTEGER DEFAULT 0,
                last_watched TIMESTAMP
            )
        """)

        # Migration: ensure `series_name` column exists for older databases
        try:
            async with db.execute("PRAGMA table_info(popular_content)") as cursor:
                cols = await cursor.fetchall()
                col_names = [c[1] for c in cols]
            if 'series_name' not in col_names:
                await db.execute("ALTER TABLE popular_content ADD COLUMN series_name TEXT")
        except Exception:
            # If anything goes wrong here, continue - table will be created above for new DBs
            pass

        # Achievements/milestones table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS achievements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_name TEXT NOT NULL,
                achievement_type TEXT NOT NULL,
                achievement_name TEXT NOT NULL,
                item_name TEXT,
                value INTEGER,
                earned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                reported BOOLEAN DEFAULT 0
            )
        """)

        # Streaks table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS streaks (
                user_name TEXT PRIMARY KEY,
                current_streak INTEGER DEFAULT 0,
                longest_streak INTEGER DEFAULT 0,
                last_watch_date TEXT,
                streak_start_date TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.commit()
        print("Database initialized successfully")

async def record_watch_start(session_id: str, session: dict):
    """Record when a user starts watching content"""
    now_playing = session.get('NowPlayingItem')
    if not now_playing:
        return

    user_name = session.get('UserName', 'Unknown')
    item_id = now_playing.get('Id')
    item_name = now_playing.get('Name', 'Unknown')
    item_type = now_playing.get('Type', 'Unknown')
    device_name = session.get('DeviceName', 'Unknown')
    play_method = session.get('PlayState', {}).get('PlayMethod', 'Unknown')

    # Series info
    series_name = now_playing.get('SeriesName')
    season_number = now_playing.get('ParentIndexNumber')
    episode_number = now_playing.get('IndexNumber')

    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            INSERT INTO watch_history
            (session_id, user_name, item_id, item_name, item_type, series_name,
             season_number, episode_number, start_time, play_method, device_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (session_id, user_name, item_id, item_name, item_type, series_name,
              season_number, episode_number, datetime.now(), play_method, device_name))
        await db.commit()

async def record_watch_end(session_id: str, item_id: str):
    """Record when a user stops watching content"""
    async with aiosqlite.connect(db_path) as db:
        # Find the most recent watch entry for this session and item
        async with db.execute("""
            SELECT id, start_time, user_name, series_name, item_name, item_type FROM watch_history
            WHERE session_id = ? AND item_id = ? AND end_time IS NULL
            ORDER BY start_time DESC LIMIT 1
        """, (session_id, item_id)) as cursor:
            row = await cursor.fetchone()

            if row:
                entry_id, start_time, user_name, series_name, item_name, item_type = row
                end_time = datetime.now()
                start_dt = datetime.fromisoformat(start_time)
                duration = int((end_time - start_dt).total_seconds())

                await db.execute("""
                    UPDATE watch_history
                    SET end_time = ?, duration_seconds = ?
                    WHERE id = ?
                """, (end_time, duration, entry_id))

                # Update popular content cache
                await db.execute("""
                    INSERT INTO popular_content (item_id, item_name, item_type, series_name, total_watch_time, watch_count, last_watched)
                    SELECT item_id, item_name, item_type, series_name, ?, 1, ?
                    FROM watch_history WHERE id = ?
                    ON CONFLICT(item_id) DO UPDATE SET
                        total_watch_time = total_watch_time + ?,
                        watch_count = watch_count + 1,
                        last_watched = ?
                """, (duration, end_time, entry_id, duration, end_time))

                # Check for achievements/milestones
                await check_achievements(user_name, series_name, item_name, item_type, item_id, db)

                # Update streaks
                await update_streak(user_name, db)

                await db.commit()

async def update_streak(user_name: str, db):
    """Update user's watch streak and check for milestone achievements"""
    from datetime import date, timedelta
    
    today = date.today().isoformat()
    
    # Get current streak info
    async with db.execute("""
        SELECT current_streak, longest_streak, last_watch_date, streak_start_date
        FROM streaks WHERE user_name = ?
    """, (user_name,)) as cursor:
        row = await cursor.fetchone()
    
    if row:
        current_streak, longest_streak, last_watch_date, streak_start_date = row
        
        # Check if user already watched today
        if last_watch_date == today:
            return  # Already counted for today
        
        # Check if streak continues (watched yesterday)
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        if last_watch_date == yesterday:
            # Continue streak
            new_streak = current_streak + 1
        else:
            # Streak broken, start new one
            new_streak = 1
            streak_start_date = today
    else:
        # First time tracking this user
        new_streak = 1
        longest_streak = 0
        streak_start_date = today
    
    # Update longest streak if needed
    if new_streak > (longest_streak or 0):
        longest_streak = new_streak
    
    # Insert or update
    await db.execute("""
        INSERT INTO streaks (user_name, current_streak, longest_streak, last_watch_date, streak_start_date, updated_at)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_name) DO UPDATE SET
            current_streak = ?, longest_streak = ?, last_watch_date = ?, streak_start_date = ?, updated_at = CURRENT_TIMESTAMP
    """, (user_name, new_streak, longest_streak, today, streak_start_date,
          new_streak, longest_streak, today, streak_start_date))
    
    # Check for streak milestones (7, 30, 100 days)
    streak_milestones = [7, 30, 100]
    for milestone in streak_milestones:
        if new_streak == milestone:
            # Award achievement
            ach_name = f"{milestone} Day Streak"
            async with db.execute("""
                SELECT id FROM achievements
                WHERE user_name = ? AND achievement_type = 'streak_milestone' AND value = ?
            """, (user_name, milestone)) as check:
                if not await check.fetchone():
                    await db.execute("""
                        INSERT INTO achievements (user_name, achievement_type, achievement_name, value, earned_at, reported)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (user_name, 'streak_milestone', ach_name, milestone, datetime.now().isoformat(), 0))

async def check_achievements(user_name: str, series_name: str, item_name: str, item_type: str, item_id: str, db):
    """Check if user has earned any achievements"""
    
    # Check if user finished a series (all episodes watched)
    if item_type == 'Episode' and series_name:
        # Count watched distinct episodes by series_name
        async with db.execute("""
            SELECT COUNT(DISTINCT item_id) as watched_episodes
            FROM watch_history
            WHERE user_name = ? AND series_name = ? AND item_type = 'Episode'
        """, (user_name, series_name)) as cursor:
            result = await cursor.fetchone()
            watched_count = result[0] if result else 0

        # Try to get accurate total episodes from Jellyfin using series id
        total_episodes = None
        try:
            # Fetch item details to get series id
            item = await jf.get_item(item_id)
            series_id = item.get('SeriesId') or item.get('SeriesId') or item.get('ParentId')
            if series_id:
                total_episodes = await jf.get_series_episode_count(series_id)
        except Exception:
            total_episodes = None

        # Determine completion: prefer accurate count if available
        completed = False
        if total_episodes and total_episodes > 0:
            if watched_count >= total_episodes:
                completed = True
        else:
            # Fallback heuristic: at least 10 distinct episodes
            if watched_count >= 10:
                completed = True

        if completed:
            async with db.execute("""
                SELECT id FROM achievements
                WHERE user_name = ? AND achievement_type = 'series_completion' AND item_name = ?
            """, (user_name, series_name)) as check:
                if not await check.fetchone():
                    await db.execute("""
                        INSERT INTO achievements (user_name, achievement_type, achievement_name, item_name, value)
                        VALUES (?, ?, ?, ?, ?)
                    """, (user_name, 'series_completion', f'Finished "{series_name}"', series_name, watched_count))
    
    # Check for watch time milestones
    async with db.execute("""
        SELECT SUM(duration_seconds) as total_seconds
        FROM watch_history
        WHERE user_name = ? AND duration_seconds IS NOT NULL
    """, (user_name,)) as cursor:
        result = await cursor.fetchone()
        if result and result[0]:
            total_hours = result[0] / 3600
            
            for milestone in ACHIEVEMENT_MILESTONES:
                if total_hours >= milestone:
                    async with db.execute("""
                        SELECT id FROM achievements
                        WHERE user_name = ? AND achievement_type = 'watch_milestone' AND value = ?
                    """, (user_name, milestone)) as check:
                        if not await check.fetchone():
                            await db.execute("""
                                INSERT INTO achievements (user_name, achievement_type, achievement_name, value)
                                VALUES (?, ?, ?, ?)
                            """, (user_name, 'watch_milestone', f'Watched {milestone} Hours', milestone))

async def generate_weekly_digest():
    """Generate and send weekly digest to activity channel"""
    if not ACTIVITY_CHANNEL_ID:
        return

    try:
        activity_channel = bot.get_channel(ACTIVITY_CHANNEL_ID)
        if not activity_channel:
            print("ERROR: Could not find activity channel for weekly digest")
            return

        async with aiosqlite.connect(db_path) as db:
            # Get week boundaries (last 7 days)
            week_start = datetime.now() - timedelta(days=7)
            week_start_str = week_start.isoformat()
            now_str = datetime.now().isoformat()

            # Get total watch time this week
            async with db.execute("""
                SELECT SUM(duration_seconds) as total_seconds, COUNT(DISTINCT user_name) as unique_users
                FROM watch_history
                WHERE start_time >= ? AND duration_seconds IS NOT NULL
            """, (week_start_str,)) as cursor:
                stats = await cursor.fetchone()
                total_seconds = stats[0] or 0
                unique_users = stats[1] or 0

            total_hours = total_seconds / 3600

            # Get last week total for comparison
            two_weeks_ago = datetime.now() - timedelta(days=14)
            one_week_ago = datetime.now() - timedelta(days=7)
            async with db.execute("""
                SELECT SUM(duration_seconds) as total_seconds
                FROM watch_history
                WHERE start_time >= ? AND start_time < ? AND duration_seconds IS NOT NULL
            """, (two_weeks_ago.isoformat(), one_week_ago.isoformat())) as cursor:
                last_week = await cursor.fetchone()
                last_week_hours = (last_week[0] or 0) / 3600

            # Calculate percentage change
            if last_week_hours > 0:
                change_percent = ((total_hours - last_week_hours) / last_week_hours) * 100
                change_icon = "↑" if change_percent > 0 else "↓"
            else:
                change_percent = 0
                change_icon = "→"

            # Get top content this week
            async with db.execute("""
                SELECT 
                    CASE 
                        WHEN item_type = 'Episode' THEN series_name
                        ELSE item_name
                    END as display_name,
                    SUM(total_watch_time) as total_time
                FROM popular_content
                WHERE last_watched >= ?
                GROUP BY 
                    CASE 
                        WHEN item_type = 'Episode' THEN series_name
                        ELSE item_name
                    END
                ORDER BY total_time DESC
                LIMIT 5
            """, (week_start_str,)) as cursor:
                top_content = await cursor.fetchall()

            # Get top watchers this week
            async with db.execute("""
                SELECT user_name, SUM(duration_seconds) as total_seconds, COUNT(DISTINCT item_id) as items_watched
                FROM watch_history
                WHERE start_time >= ? AND duration_seconds IS NOT NULL
                GROUP BY user_name
                ORDER BY total_seconds DESC
                LIMIT 3
            """, (week_start_str,)) as cursor:
                top_watchers = await cursor.fetchall()

            # Get unreported achievements this week
            async with db.execute("""
                SELECT user_name, achievement_type, achievement_name, item_name, value, earned_at
                FROM achievements
                WHERE earned_at >= ? AND reported = 0
                ORDER BY earned_at DESC
            """, (week_start_str,)) as cursor:
                new_achievements = await cursor.fetchall()

            # Mark achievements as reported
            for achievement in new_achievements:
                await db.execute("""
                    UPDATE achievements SET reported = 1
                    WHERE user_name = ? AND achievement_name = ? AND earned_at = ?
                """, (achievement[0], achievement[2], achievement[5]))

            # Get peak viewing day
            async with db.execute("""
                SELECT DATE(start_time) as date, SUM(duration_seconds) as total_seconds
                FROM watch_history
                WHERE start_time >= ? AND duration_seconds IS NOT NULL
                GROUP BY DATE(start_time)
                ORDER BY total_seconds DESC
                LIMIT 1
            """, (week_start_str,)) as cursor:
                peak_day = await cursor.fetchone()

            await db.commit()

        # Build embed
        embed = discord.Embed(
            title="📺 Weekly Digest",
            description=f"Week of {week_start.strftime('%b %d')} - {datetime.now().strftime('%b %d')}",
            color=discord.Color.purple(),
            timestamp=datetime.now()
        )

        # Summary stats
        summary = f"**Total Watched:** {total_hours:.1f}h {change_icon} {abs(change_percent):.0f}%\n"
        summary += f"**Active Users:** {unique_users}\n"
        if peak_day:
            peak_hours = peak_day[1] / 3600
            summary += f"**Peak Day:** {peak_day[0]} ({peak_hours:.1f}h)"

        embed.add_field(name="📊 Summary", value=summary, inline=False)

        # Top content
        if top_content:
            content_text = ""
            for i, (name, watch_time) in enumerate(top_content, 1):
                hours = watch_time / 3600
                content_text += f"{i}. **{name}** - {hours:.1f}h\n"
            embed.add_field(name="🏆 Top Content This Week", value=content_text, inline=False)

        # Top watchers
        if top_watchers:
            watchers_text = ""
            for i, (user, seconds, items) in enumerate(top_watchers, 1):
                hours = seconds / 3600
                watchers_text += f"{i}. **{user}** - {hours:.1f}h ({items} items)\n"
            embed.add_field(name="👥 Top Watchers", value=watchers_text, inline=False)

        # Achievements
        if new_achievements:
            achievements_text = ""
            for user, ach_type, ach_name, item_name, value, earned_at in new_achievements:
                if ach_type == 'series_completion':
                    achievements_text += f"🎉 **{user}** finished \"{item_name}\"!\n"
                elif ach_type == 'watch_milestone':
                    achievements_text += f"🏅 **{user}** reached **{value} hours** watched!\n"
            if achievements_text:
                embed.add_field(name="🎯 Milestones Achieved", value=achievements_text, inline=False)

        embed.set_footer(text="See you next week!")
        await activity_channel.send(embed=embed)

        # Send individual achievement badges/messages for visibility
        for user, ach_type, ach_name, item_name, value, earned_at in new_achievements:
            try:
                badge_buf = generate_badge(ach_name, f"{user}")
                badge_buf.seek(0)
                file = discord.File(badge_buf, filename="achievement.png")
                ach_embed = discord.Embed(title=ach_name, description=f"{user}", color=discord.Color.green(), timestamp=datetime.fromisoformat(earned_at) if earned_at else datetime.now())
                ach_embed.set_image(url="attachment://achievement.png")
                await activity_channel.send(embed=ach_embed, file=file)
            except Exception as e:
                print(f"Error sending achievement badge for {user}: {e}")

        print("Weekly digest sent successfully")

    except Exception as e:
        print(f"Error generating weekly digest: {e}")

# Fire at an exact wall-clock time rather than polling hourly. The previous
# version looped every hour and required now.minute < 5, but tasks.loop fires
# at whatever minute the bot started, so the digest never sent unless the bot
# happened to start in the first 5 minutes of an hour.
DIGEST_TIME = time(hour=18, minute=0, tzinfo=ZoneInfo(os.getenv("TZ", "America/New_York")))


@tasks.loop(time=DIGEST_TIME)
async def send_weekly_digest():
    """Send the digest every Sunday at 6 PM local time."""
    if datetime.now().weekday() == 6:  # Sunday
        await generate_weekly_digest()

@bot.event
async def on_ready():
    global server_start_time
    server_start_time = datetime.now()
    print(f'Bot connected as {bot.user}')
    print(f'Dashboard channel: {DASHBOARD_CHANNEL_ID}')
    print(f'Activity channel: {ACTIVITY_CHANNEL_ID}')

    # Initialize database
    await init_database()

    # Sync slash commands
    try:
        synced = await bot.tree.sync()
        print(f'Synced {len(synced)} slash command(s)')
    except Exception as e:
        print(f'Failed to sync commands: {e}')

    # Movie night roles, channels and the #roles picker: created if missing
    try:
        await movie_night.ensure_structure(bot)
    except Exception as e:
        print(f'Movie night setup failed: {e}')

    # Wait 10 seconds before starting activity tracking (avoid startup spam)
    await asyncio.sleep(10)

    # Start background tasks. on_ready fires again after every reconnect, and
    # calling .start() on an already-running task raises RuntimeError, so each
    # one is guarded.
    if not heartbeat.is_running():
        heartbeat.start()
    if jf and DASHBOARD_CHANNEL_ID and not update_dashboard.is_running():
        update_dashboard.start()
    if jf and ACTIVITY_CHANNEL_ID:
        if not check_sessions.is_running():
            check_sessions.start()
        if not send_weekly_digest.is_running():
            send_weekly_digest.start()

@tasks.loop(seconds=UPDATE_INTERVAL)
async def update_dashboard():
    """Update the dashboard embed every UPDATE_INTERVAL seconds"""
    global dashboard_message

    try:
        server_info = await jf.get_server_info()
        sessions = await jf.get_sessions()
        library_stats = await jf.get_library_stats()

        uptime = datetime.now() - server_start_time if server_start_time else timedelta(0)
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

                embed.add_field(
                    name=f"{status_icon} {user} on {device}",
                    value=f"**{title}**\n{subtitle}\n`{'█' * (progress // 5)}{'░' * (20 - progress // 5)}` {progress}%",
                    inline=False
                )
        else:
            embed.add_field(
                name="💤 No active streams",
                value="All quiet on the media front",
                inline=False
            )

        embed.set_footer(text=f"Last updated")

        channel = bot.get_channel(DASHBOARD_CHANNEL_ID)
        if not channel:
            print(f"ERROR: Could not find dashboard channel {DASHBOARD_CHANNEL_ID}")
            return

        if dashboard_message is None:
            async for msg in channel.history(limit=10):
                if msg.author == bot.user and msg.embeds:
                    if "Jellyfin Server Dashboard" in msg.embeds[0].title:
                        dashboard_message = msg
                        break

        if dashboard_message:
            await dashboard_message.edit(embed=embed)
        else:
            dashboard_message = await channel.send(embed=embed)

    except Exception as e:
        print(f"Error updating dashboard: {e}")

HEARTBEAT_FILE = "/tmp/bot_heartbeat"
# How long authenticated Jellyfin calls may keep failing before the heartbeat
# stops. Long enough to ride out a Jellyfin restart for an update.
JELLYFIN_STALE_SECONDS = 300
jellyfin_stale_reported = False
# Uptime Kuma has no Docker socket, so it cannot see the container's health
# status. Each good heartbeat also hits a Kuma Push monitor, which alerts when
# the pushes stop. Optional: empty means file only.
UPTIME_KUMA_PUSH_URL = os.getenv("UPTIME_KUMA_PUSH_URL", "")
# Its own session: the Jellyfin client's carries the API key in its headers.
push_session: aiohttp.ClientSession | None = None


async def push_uptime_kuma():
    global push_session
    if not UPTIME_KUMA_PUSH_URL:
        return
    if push_session is None or push_session.closed:
        push_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
    try:
        async with push_session.get(UPTIME_KUMA_PUSH_URL) as resp:
            if resp.status != 200:
                print(f"Uptime Kuma push failed: {resp.status}")
    except Exception as e:
        print(f"Uptime Kuma push failed: {e}")


@tasks.loop(seconds=30)
async def heartbeat():
    """Touch a file so the container healthcheck can detect a wedged bot.

    Restart-on-exit only catches a crashed process; this also catches the
    case where the process is alive but no longer connected to Discord, or
    connected but unable to reach Jellyfin. The second case ran unnoticed for
    six days in October 2026: every call got a 401 while the container stayed
    healthy.
    """
    global jellyfin_stale_reported

    if bot.is_closed():
        return

    # Only the polling loops call Jellyfin on a schedule, so without them
    # there is nothing to judge freshness by.
    polling = check_sessions.is_running() or update_dashboard.is_running()
    if jf and polling:
        stale = jf.seconds_since_auth_success()
        if stale > JELLYFIN_STALE_SECONDS:
            if not jellyfin_stale_reported:
                print(f"Heartbeat withheld: no successful Jellyfin call for {int(stale)}s")
                jellyfin_stale_reported = True
            return
        if jellyfin_stale_reported:
            print("Heartbeat resumed: Jellyfin calls succeeding again")
            jellyfin_stale_reported = False

    try:
        with open(HEARTBEAT_FILE, "w") as fh:
            fh.write(str(datetime.now()))
    except OSError as e:
        print(f"Could not write heartbeat: {e}")
    await push_uptime_kuma()


@tasks.loop(seconds=30)
async def check_sessions():
    """Check for session changes every 30 seconds"""
    global active_sessions, sessions_seeded

    try:
        sessions = await jf.get_sessions()
        current_session_ids = {s['Id'] for s in sessions}

        activity_channel = bot.get_channel(ACTIVITY_CHANNEL_ID)
        if not activity_channel:
            return

        # active_sessions lives in memory, so after a restart every session
        # Jellyfin still lists (idle ones linger for hours) looked new and was
        # announced as "connected" or "started watching". The first poll only
        # takes note of them; watch history is still recorded below.
        announce = sessions_seeded

        for session in sessions:
            sess_id = session['Id']
            username = session.get('UserName', 'Unknown')
            device = session.get('DeviceName', 'Unknown')

            if sess_id not in active_sessions and announce:
                embed = discord.Embed(
                    title="👤 User Connected",
                    description=f"**{username}** connected from **{device}**",
                    color=discord.Color.green(),
                    timestamp=datetime.now()
                )
                await activity_channel.send(embed=embed)

            now_playing = session.get('NowPlayingItem')
            prev_playing = active_sessions.get(sess_id, {}).get('NowPlayingItem')

            # Detect if content changed (either started fresh or switched to different show/movie)
            now_item_id = now_playing.get('Id') if now_playing else None
            prev_item_id = prev_playing.get('Id') if prev_playing else None

            # If bot just started and we find an active playback, check database for existing unclosed session
            if now_playing and sess_id not in active_sessions:
                # Bot may have restarted - check if there's an unclosed session in DB
                async with aiosqlite.connect(db_path) as db:
                    async with db.execute("""
                        SELECT id FROM watch_history
                        WHERE session_id = ? AND item_id = ? AND end_time IS NULL
                        ORDER BY start_time DESC LIMIT 1
                    """, (sess_id, now_item_id)) as cursor:
                        existing = await cursor.fetchone()

                # If no existing session, record the start
                if not existing:
                    await record_watch_start(sess_id, session)

            # Record watch history when content changes
            elif now_playing and now_item_id != prev_item_id:
                # If switching from one item to another, record end of previous
                if prev_item_id:
                    await record_watch_end(sess_id, prev_item_id)
                # Record start of new item
                await record_watch_start(sess_id, session)

            # Only send notification if the item ID actually changed (switched content)
            if announce and now_playing and now_item_id != prev_item_id:
                item_name = now_playing.get('Name', 'Unknown')
                item_type = now_playing.get('Type', 'Media')

                # Determine if this is a fresh start or a switch
                if prev_playing:
                    title_text = "🔄 Switched Content"
                    desc_text = f"**{username}** switched to"
                else:
                    title_text = "▶️ Playback Started"
                    desc_text = f"**{username}** started watching"

                embed = discord.Embed(
                    title=title_text,
                    description=desc_text,
                    color=discord.Color.blue(),
                    timestamp=datetime.now()
                )

                if item_type == 'Episode':
                    series = now_playing.get('SeriesName', 'Unknown')
                    season = now_playing.get('ParentIndexNumber', '?')
                    episode = now_playing.get('IndexNumber', '?')
                    embed.add_field(name="Show", value=series, inline=True)
                    embed.add_field(name="Episode", value=f"S{season}E{episode}", inline=True)
                    embed.add_field(name="Title", value=item_name, inline=False)
                else:
                    embed.add_field(name="Title", value=item_name, inline=True)
                    embed.add_field(name="Type", value=item_type, inline=True)

                embed.add_field(name="Device", value=device, inline=True)

                # Skip thumbnail for now - internal Docker URLs won't work for Discord
                # TODO: Use public Tailscale URL if you want thumbnails

                await activity_channel.send(embed=embed)

            elif prev_playing and not now_playing:
                prev_name = prev_playing.get('Name', 'Unknown')

                # Record end of watch session
                if prev_item_id:
                    await record_watch_end(sess_id, prev_item_id)

                embed = discord.Embed(
                    title="⏹️ Playback Stopped",
                    description=f"**{username}** stopped watching **{prev_name}**",
                    color=discord.Color.greyple(),
                    timestamp=datetime.now()
                )
                await activity_channel.send(embed=embed)

            active_sessions[sess_id] = session

        offline_sessions = set(active_sessions.keys()) - current_session_ids
        for sess_id in offline_sessions:
            username = active_sessions[sess_id].get('UserName', 'Unknown')

            # Record end of any active watch session when user disconnects
            prev_session = active_sessions[sess_id]
            prev_item = prev_session.get('NowPlayingItem')
            if prev_item:
                await record_watch_end(sess_id, prev_item.get('Id'))

            embed = discord.Embed(
                title="👋 User Disconnected",
                description=f"**{username}** disconnected",
                color=discord.Color.red(),
                timestamp=datetime.now()
            )
            await activity_channel.send(embed=embed)
            del active_sessions[sess_id]

        sessions_seeded = True

        # Monitor transcodes - warn if too many
        transcoding_sessions = [s for s in sessions if s.get('PlayState', {}).get('PlayMethod') == 'Transcode']
        if len(transcoding_sessions) >= 3:
            transcode_users = [s.get('UserName', 'Unknown') for s in transcoding_sessions]
            embed = discord.Embed(
                title="⚠️ High Transcode Load",
                description=f"**{len(transcoding_sessions)} active transcodes** detected",
                color=discord.Color.orange(),
                timestamp=datetime.now()
            )
            embed.add_field(name="Users Transcoding", value="\n".join([f"• {u}" for u in transcode_users]), inline=False)
            embed.add_field(name="💡 Tip", value="Consider using compatible formats or adjusting quality settings to reduce server load.", inline=False)
            await activity_channel.send(embed=embed)

    except Exception as e:
        print(f"Error checking sessions: {e}")


# --- Owner-only commands ----------------------------------------------------
# For the server owner (or the bot application's owner) only. default_permissions
# hides them from everyone without Administrator in the command list; the check
# refuses anyone else who calls them anyway.
async def _is_owner(interaction: discord.Interaction) -> bool:
    if interaction.guild and interaction.user.id == interaction.guild.owner_id:
        return True
    return await bot.is_owner(interaction.user)


def owner_only():
    def decorate(func):
        func = app_commands.check(_is_owner)(func)
        return app_commands.default_permissions(administrator=True)(func)
    return decorate


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        message = "That command is for the server owner only."
    else:
        print(f"Command error in /{interaction.command.name if interaction.command else '?'}: {error}")
        message = "Something went wrong running that command."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)

@bot.tree.command(name="jf-health", description="Show server health and resource usage")
async def health_command(interaction: discord.Interaction):
    """Slash command to check server health"""
    await interaction.response.defer()

    try:
        # Get system stats
        cpu_percent = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        # /app/data is bind-mounted from the media drive, so this reports the
        # disk that actually matters. "/" would report the container's overlay
        # filesystem, where a low-space warning could never fire.
        disk = shutil.disk_usage(db_path.rsplit("/", 1)[0])

        # Get Jellyfin system info if available
        system_info = await jf.get_system_info()

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
        sessions = await jf.get_sessions()
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

@bot.tree.command(name="jf-stats", description="Show Jellyfin server statistics")
async def stats_command(interaction: discord.Interaction):
    """Slash command to get server stats"""
    await interaction.response.defer()

    try:
        server_info = await jf.get_server_info()
        sessions = await jf.get_sessions()
        library_stats = await jf.get_library_stats()

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

@bot.tree.command(name="jf-status", description="Show detailed Jellyfin server status with active streams")
async def status_command(interaction: discord.Interaction):
    """Slash command to get comprehensive server status"""
    await interaction.response.defer()

    try:
        server_info = await jf.get_server_info()
        sessions = await jf.get_sessions()
        library_stats = await jf.get_library_stats()

        # Calculate bot uptime
        uptime = datetime.now() - server_start_time if server_start_time else timedelta(0)
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

@bot.tree.command(name="jf-recent", description="Show recently added content")
async def recent_command(interaction: discord.Interaction):
    """Slash command to show recently added items"""
    await interaction.response.defer()

    try:
        items = await jf.get_recently_added(limit=10)

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

@bot.tree.command(name="jf-stats-graph", description="Show watch time statistics with charts")
async def stats_graph_command(interaction: discord.Interaction, days: int = 7):
    """Generate watch time charts"""
    await interaction.response.defer()

    try:
        async with aiosqlite.connect(db_path) as db:
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

@bot.tree.command(name="jf-popular", description="Show most popular content based on watch time")
async def popular_command(interaction: discord.Interaction, limit: int = 10):
    """Show most watched content - aggregates TV shows by series"""
    await interaction.response.defer()

    try:
        async with aiosqlite.connect(db_path) as db:
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

@bot.tree.command(name="jf-subscribe", description="Subscribe to notifications for a specific show")
async def subscribe_command(interaction: discord.Interaction, show_name: str):
    """Subscribe to show notifications"""
    await interaction.response.defer()

    try:
        discord_user_id = str(interaction.user.id)
        discord_username = interaction.user.name

        async with aiosqlite.connect(db_path) as db:
            try:
                await db.execute("""
                    INSERT INTO subscriptions (discord_user_id, discord_username, show_name)
                    VALUES (?, ?, ?)
                """, (discord_user_id, discord_username, show_name))
                await db.commit()

                embed = discord.Embed(
                    title="✅ Subscribed Successfully",
                    description=f"You'll be notified when new episodes of **{show_name}** are added!",
                    color=discord.Color.green(),
                    timestamp=datetime.now()
                )
                await interaction.followup.send(embed=embed)

            except aiosqlite.IntegrityError:
                embed = discord.Embed(
                    title="ℹ️ Already Subscribed",
                    description=f"You're already subscribed to **{show_name}**",
                    color=discord.Color.blue()
                )
                await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@bot.tree.command(name="jf-unsubscribe", description="Unsubscribe from show notifications")
async def unsubscribe_command(interaction: discord.Interaction, show_name: str):
    """Unsubscribe from show notifications"""
    await interaction.response.defer()

    try:
        discord_user_id = str(interaction.user.id)

        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute("""
                DELETE FROM subscriptions
                WHERE discord_user_id = ? AND show_name = ?
            """, (discord_user_id, show_name))
            await db.commit()

            if cursor.rowcount > 0:
                embed = discord.Embed(
                    title="✅ Unsubscribed Successfully",
                    description=f"You'll no longer receive notifications for **{show_name}**",
                    color=discord.Color.green(),
                    timestamp=datetime.now()
                )
            else:
                embed = discord.Embed(
                    title="ℹ️ Not Subscribed",
                    description=f"You weren't subscribed to **{show_name}**",
                    color=discord.Color.blue()
                )

            await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@bot.tree.command(name="jf-subscriptions", description="List your current show subscriptions")
async def subscriptions_command(interaction: discord.Interaction):
    """List user's subscriptions"""
    await interaction.response.defer()

    try:
        discord_user_id = str(interaction.user.id)

        async with aiosqlite.connect(db_path) as db:
            async with db.execute("""
                SELECT show_name, subscribed_at
                FROM subscriptions
                WHERE discord_user_id = ?
                ORDER BY subscribed_at DESC
            """, (discord_user_id,)) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            await interaction.followup.send("You don't have any show subscriptions yet.\n\nUse `/jf-subscribe <show-name>` to subscribe!")
            return

        embed = discord.Embed(
            title="📺 Your Show Subscriptions",
            description=f"You're subscribed to {len(rows)} show(s)",
            color=discord.Color.purple(),
            timestamp=datetime.now()
        )

        for show_name, subscribed_at in rows:
            try:
                dt = datetime.fromisoformat(subscribed_at)
                date_str = dt.strftime('%Y-%m-%d')
            except (ValueError, TypeError):
                date_str = "Unknown"

            embed.add_field(
                name=f"📺 {show_name}",
                value=f"Subscribed: {date_str}",
                inline=True
            )

        embed.set_footer(text="Use /jf-unsubscribe to remove a subscription")
        await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@bot.tree.command(name="jf-weekly-digest", description="(Owner) Post the weekly digest now")
@owner_only()
async def weekly_digest_command(interaction: discord.Interaction):
    """Manually trigger the weekly digest"""
    await interaction.response.defer()

    try:
        await generate_weekly_digest()
        await interaction.followup.send("✅ Weekly digest sent to the activity channel!")
    except Exception as e:
        await interaction.followup.send(f"❌ Error generating digest: {e}")


@bot.tree.command(name="jf-achievements", description="List achievements for a user (default: your Jellyfin username)")
async def achievements_command(interaction: discord.Interaction, user_name: str = None, limit: int = 20):
    """Show a user's achievements"""
    await interaction.response.defer()

    try:
        if not user_name:
            user_name = interaction.user.name

        async with aiosqlite.connect(db_path) as db:
            async with db.execute("""
                SELECT achievement_type, achievement_name, item_name, value, earned_at
                FROM achievements
                WHERE user_name = ?
                ORDER BY earned_at DESC
                LIMIT ?
            """, (user_name, limit)) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            await interaction.followup.send(f"No achievements found for {user_name}.")
            return

        embed = discord.Embed(title=f"🏅 Achievements - {user_name}", color=discord.Color.blue(), timestamp=datetime.now())
        for ach_type, ach_name, item_name, value, earned_at in rows:
            try:
                dt = datetime.fromisoformat(earned_at)
                date_str = dt.strftime('%Y-%m-%d')
            except (ValueError, TypeError):
                date_str = "Unknown"

            name = ach_name
            desc = f"{item_name or ''} — {value or ''}\n{date_str}"
            embed.add_field(name=name, value=desc, inline=False)

        await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")


@bot.tree.command(name="jf-achievements-all", description="(Owner) List recent achievements across users")
@owner_only()
async def achievements_all_command(interaction: discord.Interaction, limit: int = 50):
    await interaction.response.defer()

    try:
        async with aiosqlite.connect(db_path) as db:
            async with db.execute("""
                SELECT user_name, achievement_type, achievement_name, item_name, value, earned_at
                FROM achievements
                ORDER BY earned_at DESC
                LIMIT ?
            """, (limit,)) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            await interaction.followup.send("No achievements found.")
            return

        embed = discord.Embed(title=f"🏆 Recent Achievements", color=discord.Color.gold(), timestamp=datetime.now())
        for user, ach_type, ach_name, item_name, value, earned_at in rows:
            try:
                dt = datetime.fromisoformat(earned_at)
                date_str = dt.strftime('%Y-%m-%d')
            except (ValueError, TypeError):
                date_str = "Unknown"
            embed.add_field(name=f"{ach_name} — {user}", value=f"{item_name or ''} • {value or ''}\n{date_str}", inline=False)

        await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@bot.tree.command(name="jf-streaks", description="Show your current watch streak")
async def streaks_command(interaction: discord.Interaction, user_name: str = None):
    """Show user's watch streak"""
    await interaction.response.defer()

    try:
        if not user_name:
            user_name = interaction.user.name

        async with aiosqlite.connect(db_path) as db:
            async with db.execute("""
                SELECT current_streak, longest_streak, last_watch_date, streak_start_date
                FROM streaks WHERE user_name = ?
            """, (user_name,)) as cursor:
                row = await cursor.fetchone()

        if not row:
            await interaction.followup.send(f"No streak data found for {user_name}.")
            return

        current_streak, longest_streak, last_watch_date, streak_start_date = row

        embed = discord.Embed(
            title=f"🔥 Watch Streak - {user_name}",
            color=discord.Color.orange(),
            timestamp=datetime.now()
        )

        embed.add_field(name="Current Streak", value=f"**{current_streak}** days 🔥", inline=True)
        embed.add_field(name="Longest Streak", value=f"**{longest_streak}** days", inline=True)
        embed.add_field(name="Last Watched", value=last_watch_date or "Never", inline=True)
        embed.add_field(name="Streak Started", value=streak_start_date or "N/A", inline=False)

        # Progress to next milestone
        next_milestone = None
        for m in [7, 30, 100]:
            if current_streak < m:
                next_milestone = m
                break
        
        if next_milestone:
            days_left = next_milestone - current_streak
            embed.add_field(name=f"Next Milestone", value=f"**{next_milestone}** days ({days_left} more)", inline=False)

        await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")


@bot.tree.command(name="jf-streaks-leaderboard", description="Show watch streak leaderboard")
async def streaks_leaderboard_command(interaction: discord.Interaction, limit: int = 10):
    """Show streak leaderboard"""
    await interaction.response.defer()

    try:
        async with aiosqlite.connect(db_path) as db:
            async with db.execute("""
                SELECT user_name, current_streak, longest_streak, last_watch_date
                FROM streaks
                ORDER BY current_streak DESC
                LIMIT ?
            """, (limit,)) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            await interaction.followup.send("No streaks found yet.")
            return

        embed = discord.Embed(
            title="🔥 Watch Streak Leaderboard",
            color=discord.Color.orange(),
            timestamp=datetime.now()
        )

        for i, (user, current, longest, last) in enumerate(rows, 1):
            embed.add_field(
                name=f"{i}. {user}",
                value=f"Current: **{current}** days | Longest: **{longest}** days\nLast: {last}",
                inline=False
            )

        await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")


@bot.tree.command(name="jf-debug-sessions", description="(Owner) Show unclosed watch sessions in database")
@owner_only()
async def debug_sessions_command(interaction: discord.Interaction, limit: int = 20):
    """Debug command to show sessions without end_time"""
    await interaction.response.defer()

    try:
        async with aiosqlite.connect(db_path) as db:
            async with db.execute("""
                SELECT id, user_name, item_name, item_type, start_time,
                       CAST((julianday('now') - julianday(start_time)) * 24 AS INTEGER) as hours_ago
                FROM watch_history
                WHERE end_time IS NULL
                ORDER BY start_time DESC
                LIMIT ?
            """, (limit,)) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            await interaction.followup.send("✅ No unclosed sessions found!")
            return

        embed = discord.Embed(
            title="🔍 Unclosed Watch Sessions",
            description=f"Found {len(rows)} sessions without end_time",
            color=discord.Color.orange(),
            timestamp=datetime.now()
        )

        for row_id, user, item, item_type, start_time, hours_ago in rows:
            embed.add_field(
                name=f"ID {row_id}: {user} - {item}",
                value=f"Type: {item_type}\nStarted: {start_time}\n⏰ {hours_ago}h ago",
                inline=False
            )

        embed.set_footer(text="These sessions are excluded from statistics")
        await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@bot.tree.command(name="jf-help", description="Show all available Jellyfin bot commands")
async def help_command(interaction: discord.Interaction):
    """Display help information with all available commands"""
    embed = discord.Embed(
        title="🤖 Jellyfin Discord Bot - Commands",
        description="Here are all available commands for the Jellyfin bot:",
        color=discord.Color.purple(),
        timestamp=datetime.now()
    )

    # Server Status Commands
    embed.add_field(
        name="📊 Server Status",
        value=(
            "`/jf-status` - Show detailed server status with active streams\n"
            "`/jf-stats` - Show server statistics and library info\n"
            "`/jf-health` - Check server health and resource usage"
        ),
        inline=False
    )

    # Content Commands
    embed.add_field(
        name="🎬 Content Discovery",
        value=(
            "`/jf-recent` - Show recently added content\n"
            "`/jf-popular [limit]` - Show most popular content by watch time"
        ),
        inline=False
    )

    # Analytics Commands
    embed.add_field(
        name="📈 Analytics",
        value=(
            "`/jf-stats-graph [days]` - Show watch time charts (default: 7 days)\n"
            "`/jf-weekly-digest` - (Owner) Post the weekly digest now"
        ),
        inline=False
    )

    # Subscription Commands
    embed.add_field(
        name="🔔 Subscriptions",
        value=(
            "`/jf-subscribe <show-name>` - Subscribe to notifications for a show\n"
            "`/jf-unsubscribe <show-name>` - Unsubscribe from a show\n"
            "`/jf-subscriptions` - List your current subscriptions"
        ),
        inline=False
    )

    # Achievements & Streaks
    embed.add_field(
        name="🏆 Achievements & Streaks",
        value=(
            "`/jf-achievements [user]` - List achievements for a user\n"
            "`/jf-achievements-all [limit]` - (Owner) List recent achievements\n"
            "`/jf-streaks [user]` - Show your watch streak\n"
            "`/jf-streaks-leaderboard [limit]` - View streak leaderboard"
        ),
        inline=False
    )

    # Debug Commands
    embed.add_field(
        name="🔧 Owner only",
        value=(
            "`/jf-debug-sessions [limit]` - Show unclosed watch sessions"
        ),
        inline=False
    )

    # Bot Info
    embed.add_field(
        name="ℹ️ About",
        value=(
            f"**Dashboard Channel:** <#{DASHBOARD_CHANNEL_ID}>\n"
            f"**Activity Channel:** <#{ACTIVITY_CHANNEL_ID}>\n"
            f"**Weekly Digest:** Sundays at 6 PM\n"
            f"**Update Interval:** {UPDATE_INTERVAL} seconds"
        ),
        inline=False
    )

    embed.set_footer(text="Jellyfin MediaServer Bot")
    await interaction.response.send_message(embed=embed)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    print(f"Command error: {error}")

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("ERROR: DISCORD_TOKEN not set!")
        exit(1)
    if not JELLYFIN_API_KEY:
        print("ERROR: JELLYFIN_API_KEY not set!")
        exit(1)

    bot.run(DISCORD_TOKEN)
