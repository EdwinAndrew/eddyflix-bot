"""/help."""
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

import config


class Help(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="help", description="What the EddyFlix bot can do")
    async def help_command(self, interaction: discord.Interaction):
        """Display help information with all available commands"""
        embed = discord.Embed(
            title="🤖 EddyFlix Bot - Commands",
            description="Here's what the bot can do. Staff and owner commands only show up for them.",
            color=discord.Color.purple(),
            timestamp=datetime.now()
        )

        embed.add_field(
            name="🎬 Find something to watch",
            value=(
                "`/watch <title>` - Find a film or show, get a link and where you left off\n"
                "`/pick [type] [genre] [max_length] [with]` - Something to watch at random, with a reroll; "
                "`with` a member for what neither of you has seen\n"
                "`/continue` - What you were in the middle of\n"
                "`/request <title>` - Ask for a film or show to be added; DMs you when it's ready"
            ),
            inline=False
        )

        embed.add_field(
            name="🔔 New-episode DMs",
            value=(
                "`/subscribe <show>` - Get a DM when new episodes of a show are added\n"
                "`/unsubscribe <show>` - Stop those DMs for a show\n"
                "`/subscriptions` - List the shows you're subscribed to"
            ),
            inline=False
        )

        embed.add_field(
            name="🏆 You and the server",
            value=(
                "`/link <jellyfin user>` - Connect your Discord to your Jellyfin user (or the 👤 button in #roles)\n"
                "`/achievements [user]` - Watch time, streak, movie nights and finished series\n"
                "`/streaks [user]` - Your watch streak\n"
                "`/stats [view]` - Server status, recently added, most watched, a watch time chart, "
                "or the streak leaderboard\n"
                "`/movienight-history` - Past movie nights, who came and how they were rated"
            ),
            inline=False
        )

        embed.add_field(
            name="🍿 Movie Night (Directors and Ushers)",
            value=(
                "`/movienight <film> <day> <time>` - Schedule one: an event and a ping for Ticket Holders\n"
                "`/movievote [films] [hours]` - Post a poll of films\n"
                "`/spoiler <member> [hours]` - Spoiler Alert for a while (default 24 hours)\n"
                "`/unspoiler <member>` - Lift it early"
            ),
            inline=False
        )

        embed.add_field(
            name="🔧 Owner only",
            value="`/admin` - Post the weekly digest, New on EddyFlix or admin summary now; self-test, health and diagnostics",
            inline=False
        )

        # Bot Info
        embed.add_field(
            name="ℹ️ About",
            value=(
                f"**Dashboard Channel:** <#{config.DASHBOARD_CHANNEL_ID}>\n"
                f"**Activity Channel:** <#{config.ACTIVITY_CHANNEL_ID}>\n"
                f"**Weekly Digest:** Sundays at 6 PM, then New on EddyFlix\n"
                f"**Update Interval:** {config.UPDATE_INTERVAL} seconds"
            ),
            inline=False
        )

        embed.set_footer(text="Jellyfin MediaServer Bot")
        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(Help(bot))
