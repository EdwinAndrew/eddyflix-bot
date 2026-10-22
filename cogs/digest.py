"""The weekly digest: Sundays at 6 PM, or now with /jf-weekly-digest (owner only)."""
import io
import os
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import aiosqlite
import discord
import matplotlib
from discord import app_commands
from discord.ext import commands, tasks
from PIL import Image, ImageDraw, ImageFont

import config
from checks import owner_only


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

# Fire at an exact wall-clock time rather than polling hourly. The previous
# version looped every hour and required now.minute < 5, but tasks.loop fires
# at whatever minute the bot started, so the digest never sent unless the bot
# happened to start in the first 5 minutes of an hour.
DIGEST_TIME = time(hour=18, minute=0, tzinfo=ZoneInfo(os.getenv("TZ", "America/New_York")))


class Digest(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_services_ready(self):
        if self.bot.jf and config.ACTIVITY_CHANNEL_ID and not self.send_weekly_digest.is_running():
            self.send_weekly_digest.start()

    async def generate_weekly_digest(self):
        """Generate and send weekly digest to activity channel"""
        if not config.ACTIVITY_CHANNEL_ID:
            return

        try:
            activity_channel = self.bot.get_channel(config.ACTIVITY_CHANNEL_ID)
            if not activity_channel:
                print("ERROR: Could not find activity channel for weekly digest")
                return

            async with aiosqlite.connect(config.DB_PATH) as db:
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

    @tasks.loop(time=DIGEST_TIME)
    async def send_weekly_digest(self):
        """Send the digest every Sunday at 6 PM local time."""
        if datetime.now().weekday() == 6:  # Sunday
            await self.generate_weekly_digest()

    @app_commands.command(name="jf-weekly-digest", description="(Owner) Post the weekly digest now")
    @owner_only()
    async def weekly_digest_command(self, interaction: discord.Interaction):
        """Manually trigger the weekly digest"""
        await interaction.response.defer()

        try:
            await self.generate_weekly_digest()
            await interaction.followup.send("✅ Weekly digest sent to the activity channel!")
        except Exception as e:
            await interaction.followup.send(f"❌ Error generating digest: {e}")


async def setup(bot):
    await bot.add_cog(Digest(bot))
