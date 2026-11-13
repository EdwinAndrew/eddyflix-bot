"""/jf-help."""
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

import config


class Help(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="jf-help", description="Show all available Jellyfin bot commands")
    async def help_command(self, interaction: discord.Interaction):
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
                "`/watch <title>` - Find a film or show, get a link and where you left off\n"
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
                "`/jf-subscribe <show>` - Get a DM when new episodes of a show are added\n"
                "`/jf-unsubscribe <show>` - Stop those DMs for a show\n"
                "`/jf-subscriptions` - List the shows you're subscribed to"
            ),
            inline=False
        )

        # Achievements & Streaks
        embed.add_field(
            name="🏆 Achievements & Streaks",
            value=(
                "`/jf-achievements [user]` - Watch time, streak, movie nights and finished series\n"
                "`/jf-link <jellyfin user>` - Connect your Discord to your Jellyfin user (or the 👤 button in #roles)\n"
                "`/jf-achievements-all [limit]` - (Owner) List recent achievements\n"
                "`/jf-streaks [user]` - Show your watch streak\n"
                "`/jf-streaks-leaderboard [limit]` - View streak leaderboard"
            ),
            inline=False
        )

        # Movie night
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

        # Debug Commands
        embed.add_field(
            name="🔧 Owner only",
            value=(
                "`/jf-debug-sessions [limit]` - Show unclosed watch sessions\n"
                "`/jf-selftest` - Check the bot end to end, Discord included"
            ),
            inline=False
        )

        # Bot Info
        embed.add_field(
            name="ℹ️ About",
            value=(
                f"**Dashboard Channel:** <#{config.DASHBOARD_CHANNEL_ID}>\n"
                f"**Activity Channel:** <#{config.ACTIVITY_CHANNEL_ID}>\n"
                f"**Weekly Digest:** Sundays at 6 PM\n"
                f"**Update Interval:** {config.UPDATE_INTERVAL} seconds"
            ),
            inline=False
        )

        embed.set_footer(text="Jellyfin MediaServer Bot")
        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(Help(bot))
