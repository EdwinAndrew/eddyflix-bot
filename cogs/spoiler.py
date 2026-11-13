"""/spoiler and /unspoiler: staff hand out the 🙊 Spoiler Alert flair for a while (#15).

The expiry is kept in the database and a loop takes the role off once it has run
out, so a restart does not lose it. While it runs, the #start-here picker keeps it on
(see set_flairs in movie_night.py).
"""
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import watch_data
from cogs.movie_night import CHAT_CHANNEL, DIRECTOR_ROLE, SPOILER_FLAIR, USHER_ROLE, can_host, role, staff_only

DEFAULT_HOURS = 24
MAX_HOURS = 168   # a week


def expiry(now: int, hours: int) -> int:
    """Unix time hours from now. Giving it again starts the clock over from now,
    so it can lengthen or shorten a running one."""
    return now + hours * 3600


def hours_text(hours: int) -> str:
    return "1 hour" if hours == 1 else f"{hours} hours"


def announcement(member: str, staff: str, hours: int, until: int, previous: int | None) -> str:
    """The line in #watch-chat. previous is the expiry it replaced, if one was running."""
    flair = f"**{SPOILER_FLAIR.role_name}**"
    if previous:
        return (f"{SPOILER_FLAIR.emoji} {member}'s {flair} is reset by {staff}: {hours_text(hours)} from now, "
                f"until <t:{until}:f> (was <t:{previous}:R>).")
    return (f"{SPOILER_FLAIR.emoji} {member} is wearing {flair} for {hours_text(hours)}, until <t:{until}:f>. "
            f"Handed out by {staff}. No spoilers outside #{CHAT_CHANNEL}!")


class Spoiler(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def cog_unload(self):
        self.expire.cancel()

    @commands.Cog.listener()
    async def on_services_ready(self):
        if not self.expire.is_running():
            self.expire.start()

    async def take_off(self, guild: discord.Guild, user_id: int, had_role: bool, reason: str):
        """Remove the flair, unless they wore it before /spoiler, and forget the expiry.
        Without the members intent the member is fetched rather than looked up."""
        flair = role(guild, SPOILER_FLAIR.role_name)
        if flair and not had_role:
            try:
                member = await guild.fetch_member(user_id)
                await member.remove_roles(flair, reason=reason)
            except discord.NotFound:
                pass   # left the server
            except discord.Forbidden as e:
                print(f"Spoiler Alert: can't remove it from {user_id} ({e})")   # not retried every minute
        await watch_data.clear_spoiler(guild.id, user_id)

    @tasks.loop(minutes=1)
    async def expire(self):
        try:
            due = await watch_data.due_spoilers(int(time.time()))
        except Exception as e:
            print(f"Spoiler Alert: {e}")
            return
        for row in due:
            guild = self.bot.get_guild(row["guild_id"])
            if guild is None:
                continue   # not available right now: tried again next minute
            try:
                await self.take_off(guild, row["discord_user_id"], row["had_role"], "Spoiler Alert time is up")
            except Exception as e:   # e.g. Discord down: the row stays, tried again next minute
                print(f"Spoiler Alert for {row['discord_user_id']}: {e}")

    async def post(self, guild: discord.Guild, text: str, member: discord.Member):
        """A line in #watch-chat, pinging only the member. False if it could not be posted."""
        chat = discord.utils.get(guild.text_channels, name=CHAT_CHANNEL)
        if chat is None:
            return False
        try:
            await chat.send(text, allowed_mentions=discord.AllowedMentions(users=[member], roles=False, everyone=False))
        except discord.HTTPException as e:
            print(f"Spoiler Alert: could not post in #{CHAT_CHANNEL} ({e})")
            return False
        return True

    @app_commands.command(name="spoiler", description="Give someone the Spoiler Alert flair for a while")
    @app_commands.guild_only()
    @staff_only()
    @app_commands.describe(member="Who spoiled it",
                           hours=f"How long (default {DEFAULT_HOURS}). Again on someone who has it: starts over from now")
    async def spoiler(self, interaction: discord.Interaction, member: discord.Member,
                      hours: app_commands.Range[int, 1, MAX_HOURS] = DEFAULT_HOURS):
        guild = interaction.guild
        if not can_host(interaction.user):
            await interaction.response.send_message(f"Only a {DIRECTOR_ROLE} or {USHER_ROLE} can hand out spoiler time.", ephemeral=True)
            return
        if member.bot:
            await interaction.response.send_message("Bots don't spoil things.", ephemeral=True)
            return
        flair = role(guild, SPOILER_FLAIR.role_name)
        if flair is None:
            await interaction.response.send_message("The Spoiler Alert role is missing. Restart the bot to recreate it.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        now = int(time.time())
        until = expiry(now, hours)
        current = await watch_data.get_spoiler(guild.id, member.id)
        # Already wearing it by choice: it stays on when the time is up
        had_role = current["had_role"] if current else flair in member.roles
        if flair not in member.roles:
            try:
                await member.add_roles(flair, reason=f"/spoiler by {interaction.user} for {hours}h")
            except discord.Forbidden:
                await interaction.followup.send("I can't hand out that role: mine has to sit above it.", ephemeral=True)
                return
        previous = await watch_data.set_spoiler(guild.id, member.id, until, interaction.user.id, had_role)
        line = announcement(member.mention, interaction.user.mention, hours, until,
                            previous if previous and previous > now else None)
        posted = await self.post(guild, line, member)
        await interaction.followup.send(line if posted else f"{line}\n(Couldn't post it in #{CHAT_CHANNEL}.)",
                                        ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="unspoiler", description="Take Spoiler Alert off someone before their time is up")
    @app_commands.guild_only()
    @staff_only()
    @app_commands.describe(member="Who to let off")
    async def unspoiler(self, interaction: discord.Interaction, member: discord.Member):
        guild = interaction.guild
        if not can_host(interaction.user):
            await interaction.response.send_message(f"Only a {DIRECTOR_ROLE} or {USHER_ROLE} can lift spoiler time.", ephemeral=True)
            return
        current = await watch_data.get_spoiler(guild.id, member.id)
        if current is None:
            await interaction.response.send_message(f"{member.mention} isn't serving any spoiler time.", ephemeral=True,
                                                    allowed_mentions=discord.AllowedMentions.none())
            return
        await interaction.response.defer(ephemeral=True)
        await self.take_off(guild, member.id, current["had_role"], f"/unspoiler by {interaction.user}")
        line = f"{SPOILER_FLAIR.emoji} {member.mention} is forgiven: {interaction.user.mention} lifted their spoiler time early."
        posted = await self.post(guild, line, member)
        kept = "\nThey picked the flair themselves before, so it stays on until they drop it in #start-here." if current["had_role"] else ""
        await interaction.followup.send((line if posted else f"{line}\n(Couldn't post it in #{CHAT_CHANNEL}.)") + kept,
                                        ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(Spoiler(bot))
