"""Achievements and streaks: /jf-achievements, /jf-streaks, the leaderboard, and the
owner's /jf-achievements-all and /jf-debug-sessions. They are earned in watch_data.py."""
from datetime import datetime

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

import config
from checks import owner_only


class Achievements(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="jf-achievements", description="List achievements for a user (default: your Jellyfin username)")
    async def achievements_command(self, interaction: discord.Interaction, user_name: str = None, limit: int = 20):
        """Show a user's achievements"""
        await interaction.response.defer()

        try:
            if not user_name:
                user_name = interaction.user.name

            async with aiosqlite.connect(config.DB_PATH) as db:
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

    @app_commands.command(name="jf-achievements-all", description="(Owner) List recent achievements across users")
    @owner_only()
    async def achievements_all_command(self, interaction: discord.Interaction, limit: int = 50):
        await interaction.response.defer()

        try:
            async with aiosqlite.connect(config.DB_PATH) as db:
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

    @app_commands.command(name="jf-streaks", description="Show your current watch streak")
    async def streaks_command(self, interaction: discord.Interaction, user_name: str = None):
        """Show user's watch streak"""
        await interaction.response.defer()

        try:
            if not user_name:
                user_name = interaction.user.name

            async with aiosqlite.connect(config.DB_PATH) as db:
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

    @app_commands.command(name="jf-streaks-leaderboard", description="Show watch streak leaderboard")
    async def streaks_leaderboard_command(self, interaction: discord.Interaction, limit: int = 10):
        """Show streak leaderboard"""
        await interaction.response.defer()

        try:
            async with aiosqlite.connect(config.DB_PATH) as db:
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

    @app_commands.command(name="jf-debug-sessions", description="(Owner) Show unclosed watch sessions in database")
    @owner_only()
    async def debug_sessions_command(self, interaction: discord.Interaction, limit: int = 20):
        """Debug command to show sessions without end_time"""
        await interaction.response.defer()

        try:
            async with aiosqlite.connect(config.DB_PATH) as db:
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


async def setup(bot):
    await bot.add_cog(Achievements(bot))
