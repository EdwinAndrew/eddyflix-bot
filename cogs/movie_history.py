"""/movienight-history: past movie nights, newest first, with how many came and
how the film was rated, and the club's top-rated films. The rating buttons
themselves are in ratings.py."""
import discord
from discord import app_commands
from discord.ext import commands

import ratings
import screenings
import watch_data
from cogs.movie_night import details_link


class MovieHistory(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="movienight-history", description="Past movie nights: who hosted, how many came, how it was rated")
    @app_commands.guild_only()
    @app_commands.describe(limit="How many nights to show (default 10)")
    async def history(self, interaction: discord.Interaction, limit: app_commands.Range[int, 1, 25] = 10):
        nights = await watch_data.finished_nights(interaction.guild.id, limit)
        if not nights:
            await interaction.response.send_message(
                "No movie nights have finished yet. Schedule one with `/movienight`.", ephemeral=True)
            return
        lines = []
        for night in nights:
            text = ratings.history_line(night, details_link(night["item_id"]) if night["item_id"] else None)
            if len("\n".join(lines + [text])) > 4000:
                break
            lines.append(text)
        embed = discord.Embed(title="🎬 Movie night history", description="\n".join(lines),
                              colour=discord.Colour(screenings.COLOUR))
        top = await watch_data.top_rated_films(interaction.guild.id)
        if len(top) >= 2:   # one rated film is already in the list above
            embed.add_field(name="🏆 Top rated", value=ratings.top_lines(top), inline=False)
        embed.set_footer(text=f"Came: {screenings.ATTEND_MINUTES}+ minutes in the Theatre. "
                              "Rate a film with the buttons on its recap, for a day after")
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(MovieHistory(bot))
