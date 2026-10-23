"""Achievements and streaks: /jf-achievements, /jf-streaks, the leaderboard, and the
owner's /jf-achievements-all and /jf-debug-sessions. They are earned in watch_data.py."""
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

import config
import tiers
import watch_data
from checks import is_owner, owner_only

LOCAL = ZoneInfo(os.getenv("TZ", "America/Toronto"))
STREAK_BADGES = {7: "7-day", 30: "30-day", 100: "100-day"}


def earned_unix(earned_at) -> int | None:
    """Achievement times come in two shapes: SQLite's CURRENT_TIMESTAMP, which is
    UTC ("2026-10-22 18:00:00"), and the bot's own isoformat(), which is local
    ("2026-10-22T14:00:00.123456")."""
    try:
        dt = datetime.fromisoformat(str(earned_at))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL if "T" in str(earned_at) else timezone.utc)
    return int(dt.timestamp())


def watch_time_field(hours: float) -> str:
    """The tier, hours, a bar to the next tier, and the tiers collected so far."""
    tier, nxt = tiers.current_tier(hours), tiers.next_tier(hours)
    lines = [f"{tier.emoji} **{tier.name}**" if tier else "No tier yet",
             f"**{hours:,.0f} h** watched"]
    if nxt:
        lines.append(f"`{tiers.progress_bar(hours)}`")
        lines.append(f"Next: {nxt.emoji} {nxt.name} in {nxt.hours - hours:,.0f} h")
    else:
        lines.append("Every tier collected")
    collected = [t.emoji for t in tiers.TIERS if hours >= t.hours]
    if len(collected) > 1:
        lines.append(" ".join(collected))
    return "\n".join(lines)


def achievement_card(user_name: str, summary: dict, linked: bool,
                     limit: int = 20) -> tuple[discord.Embed, discord.File | None]:
    """One compact embed: watch time, streak, finished series. The current
    tier's badge comes with it as a file, shown in the corner."""
    tier = tiers.current_tier(summary["hours"])
    embed = discord.Embed(title=f"🏅 {user_name}",
                          colour=discord.Colour(tier.colour) if tier else discord.Colour.dark_grey())
    badge = None
    if tier:
        badge = tiers.badge_file(tier, 256)
        embed.set_thumbnail(url=f"attachment://{badge.filename}")

    embed.add_field(name="⏱️ Watch time", value=watch_time_field(summary["hours"]), inline=True)

    if summary["streak"]:
        current, best = summary["streak"]
        streak_line = f"Current **{current}** · Best **{best}** days"
        badges = [STREAK_BADGES.get(v, f"{v}-day") for v in summary["streak_milestones"]]
        if badges:
            streak_line += "\n🏅 " + " · ".join(badges)
    else:
        streak_line = "No streak yet"
    embed.add_field(name="🔥 Streak", value=streak_line, inline=True)

    series = summary["series"]
    lines = []
    for name, watched, total, earned_at in series[:limit]:
        episodes = f"{watched}/{total} episodes" if total else f"{watched} episodes"
        ts = earned_unix(earned_at)
        lines.append(f"**{name}** · {episodes}" + (f" · <t:{ts}:D>" if ts else ""))
    def render(shown):
        hidden = len(series) - len(shown)
        return "\n".join(shown + ([f"… and {hidden} more"] if hidden else [])) or "None yet"

    text = render(lines)
    while len(text) > 1000 and lines:           # embed fields hold 1024 characters
        lines.pop()
        text = render(lines)
    embed.add_field(name=f"📺 Finished series ({len(series)})", value=text, inline=False)

    if not linked:
        embed.set_footer(text="Shown for your Discord name. Use /jf-link to connect your Jellyfin user.")
    return embed, badge


class Achievements(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def resolve_user(self, interaction: discord.Interaction, user_name: str | None) -> tuple[str, bool]:
        """Who to show: the name given, else the caller's linked Jellyfin user, else
        their Discord name. The flag says whether it came from a link or by hand."""
        if user_name:
            return user_name, True
        linked = await watch_data.get_link(interaction.user.id)
        return (linked, True) if linked else (interaction.user.name, False)

    @app_commands.command(name="jf-achievements", description="List achievements for a user (default: your Jellyfin username)")
    async def achievements_command(self, interaction: discord.Interaction, user_name: str = None, limit: int = 20):
        """Show a user's achievements"""
        await interaction.response.defer()

        try:
            user_name, linked = await self.resolve_user(interaction, user_name)
            summary = await watch_data.achievement_summary(user_name)
            if not (summary["hours"] or summary["series"] or summary["streak"]):
                hint = "" if linked else "\nIs your Jellyfin name different? Link it with `/jf-link`."
                await interaction.followup.send(f"No watch history for **{user_name}** yet.{hint}")
                return
            embed, badge = achievement_card(user_name, summary, linked, limit)
            await interaction.followup.send(embed=embed, **({"file": badge} if badge else {}))

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}")

    @app_commands.command(name="jf-link", description="Connect your Discord account to your Jellyfin user")
    @app_commands.describe(jellyfin_user="Your Jellyfin user name",
                           member="(Owner) Link someone else instead")
    async def link_command(self, interaction: discord.Interaction, jellyfin_user: str,
                           member: discord.Member | None = None):
        target = member or interaction.user
        if target.id != interaction.user.id and not await is_owner(interaction):
            await interaction.response.send_message("Only the server owner can link someone else.", ephemeral=True)
            return
        try:
            names = await self.bot.jf.get_user_names() if self.bot.jf else []
        except Exception:
            names = []
        match = next((n for n in names if n.lower() == jellyfin_user.strip().lower()), None)
        if names and not match:
            await interaction.response.send_message(f"There is no Jellyfin user called **{jellyfin_user}**.", ephemeral=True)
            return
        await watch_data.set_link(target.id, match or jellyfin_user.strip())
        who = "You are" if target.id == interaction.user.id else f"{target.mention} is"
        await interaction.response.send_message(
            f"{who} now **{match or jellyfin_user.strip()}** on Jellyfin. `/jf-achievements` and `/jf-streaks` "
            "use it by default.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @link_command.autocomplete("jellyfin_user")
    async def link_user_choices(self, interaction: discord.Interaction, current: str):
        try:
            names = await self.bot.jf.get_user_names() if self.bot.jf else []
        except Exception:
            return []
        return [app_commands.Choice(name=n, value=n) for n in names if current.lower() in n.lower()][:25]

    @app_commands.command(name="jf-achievements-all", description="(Owner) List recent achievements across users")
    @owner_only()
    async def achievements_all_command(self, interaction: discord.Interaction, limit: int = 50):
        await interaction.response.defer()

        try:
            async with aiosqlite.connect(config.DB_PATH) as db:
                async with db.execute("""
                    SELECT user_name FROM achievements UNION SELECT user_name FROM streaks
                """) as cursor:
                    users = sorted((r[0] for r in await cursor.fetchall()), key=str.lower)

            if not users:
                await interaction.followup.send("No achievements found.")
                return

            lines = []
            for user in users[:limit]:
                summary = await watch_data.achievement_summary(user)
                best = summary["streak"][1] if summary["streak"] else 0
                tier = tiers.current_tier(summary["hours"])
                lines.append(f"{tier.emoji if tier else '▫️'} **{user}** · {summary['hours']:,.0f} h · best streak {best} d · "
                             f"{len(summary['series'])} series finished")
            embed = discord.Embed(title="🏆 Achievements, everyone", description="\n".join(lines)[:4000],
                                  colour=discord.Colour.gold())
            await interaction.followup.send(embed=embed)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}")

    @app_commands.command(name="jf-streaks", description="Show your current watch streak")
    async def streaks_command(self, interaction: discord.Interaction, user_name: str = None):
        """Show user's watch streak"""
        await interaction.response.defer()

        try:
            user_name, _ = await self.resolve_user(interaction, user_name)

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
