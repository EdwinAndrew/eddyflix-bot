"""/jf-subscribe, /jf-unsubscribe, /jf-subscriptions.

Subscriptions are stored but nothing notifies on them yet: see issue #10.
"""
from datetime import datetime

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

import config


class Subscriptions(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="jf-subscribe", description="Subscribe to notifications for a specific show")
    async def subscribe_command(self, interaction: discord.Interaction, show_name: str):
        """Subscribe to show notifications"""
        await interaction.response.defer()

        try:
            discord_user_id = str(interaction.user.id)
            discord_username = interaction.user.name

            async with aiosqlite.connect(config.DB_PATH) as db:
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

    @app_commands.command(name="jf-unsubscribe", description="Unsubscribe from show notifications")
    async def unsubscribe_command(self, interaction: discord.Interaction, show_name: str):
        """Unsubscribe from show notifications"""
        await interaction.response.defer()

        try:
            discord_user_id = str(interaction.user.id)

            async with aiosqlite.connect(config.DB_PATH) as db:
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

    @app_commands.command(name="jf-subscriptions", description="List your current show subscriptions")
    async def subscriptions_command(self, interaction: discord.Interaction):
        """List user's subscriptions"""
        await interaction.response.defer()

        try:
            discord_user_id = str(interaction.user.id)

            async with aiosqlite.connect(config.DB_PATH) as db:
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


async def setup(bot):
    await bot.add_cog(Subscriptions(bot))
