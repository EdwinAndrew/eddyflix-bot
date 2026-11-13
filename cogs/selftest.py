"""/jf-selftest: the owner's one-command check that the bot works, Discord included.

It runs the live Jellyfin and database checks from selftest.py, then the parts
only Discord can answer: the bot can DM (it sends the owner samples of the
new-episode DM, the movie night reminder and the recap), it can post and ping
Ticket Holders in #movie-night, it can create, start, end and delete an event in
the Theatre, and it can see who is in the Theatre. It pings nobody, and the test
event is deleted at once.
"""
import io
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

import config
import screenings
import selftest
import watch_data
from checks import owner_only
from cogs.movie_night import (ANNOUNCE_CHANNEL, TICKET_ROLE, VOICE_CHANNEL, role, syncplay_steps,
                              theatre_audience)
from cogs.subscriptions import notification_embed

TEST_EVENT = "🧪 Bot self-test (ignore)"
SAMPLE_FILM = "One Hundred and One Dalmatians (1961)"


async def event_round_trip(guild, theatre, now) -> selftest.Check:
    """Create a private event in the Theatre, start it, end it, delete it: the calls a
    movie night makes. The event is deleted even when a step fails."""
    name = "Movie night event calls"
    if theatre is None:
        return selftest.Check(name, False, f"no {VOICE_CHANNEL} channel")
    steps, error, event = [], None, None
    try:
        event = await guild.create_scheduled_event(
            name=TEST_EVENT, start_time=now + timedelta(minutes=10), entity_type=discord.EntityType.voice,
            channel=theatre, privacy_level=discord.PrivacyLevel.guild_only,
            description="Made by /jf-selftest and deleted straight away.", reason="/jf-selftest")
        steps.append("created")
        # start() and end() return the updated event and leave the old object as it
        # was: ending the old one raises ValueError "This scheduled event is not active"
        event = await event.start(reason="/jf-selftest")
        steps.append("started")
        event = await event.end(reason="/jf-selftest")
        steps.append("ended")
    except Exception as e:   # anything: the event must still be deleted below
        error = e
    if event is not None:
        try:
            await event.delete(reason="/jf-selftest")
            steps.append("deleted")
        except discord.NotFound:
            steps.append("deleted")
        except discord.HTTPException as e:
            error = error or e
            steps.append(f"NOT deleted, remove “{TEST_EVENT}” by hand")
    detail = " → ".join(steps)
    if error is not None:
        detail = f"{detail + ' → ' if steps else ''}failed: {getattr(error, 'text', None) or error}"
    return selftest.Check(name, error is None, detail)


def posting_check(name: str, channel, me, ping_role=None) -> selftest.Check:
    """The bot can post embeds and files here, and ping ping_role if one is given."""
    if channel is None:
        return selftest.Check(name, False, "channel missing")
    perms = channel.permissions_for(me)
    missing = [p for p in ("view_channel", "send_messages", "embed_links", "attach_files") if not getattr(perms, p)]
    if ping_role is not None and not (ping_role.mentionable or perms.mention_everyone):
        missing.append(f"pinging {ping_role.name}")
    return selftest.Check(name, not missing, f"can't: {', '.join(missing)}" if missing else f"#{channel.name}")


def audience_check(guild, theatre, caller_id: int, bot_id: int) -> selftest.Check:
    name = "Sees who is in the Theatre"
    if theatre is None:
        return selftest.Check(name, False, f"no {VOICE_CHANNEL} channel")
    present = theatre_audience(guild, theatre, bot_id)
    if caller_id in present:
        return selftest.Check(name, True, f"you and {len(present) - 1} other(s)" if len(present) > 1 else "you")
    return selftest.Check(name, None, f"join {VOICE_CHANNEL} and run it again to check this")


def loop_check(bot, cog_name: str, loop_name: str, label: str) -> selftest.Check:
    cog = bot.get_cog(cog_name)
    loop = getattr(cog, loop_name, None)
    running = bool(loop and loop.is_running())
    return selftest.Check(label, running, "running" if running else "not running")


def report(checks: list[selftest.Check]) -> discord.Embed:
    failed = sum(c.ok is False for c in checks)
    embed = discord.Embed(
        title="🧪 Self-test: all good" if not failed else f"🧪 Self-test: {failed} failed",
        description="\n".join(c.line for c in checks)[:4096],
        colour=discord.Colour.green() if not failed else discord.Colour.red())
    embed.set_footer(text="➖ = skipped. Samples of the DMs and movie night posts are in your DMs.")
    return embed


class SelfTest(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def dm_samples(self, user, findings: selftest.Findings, guild, theatre) -> selftest.Check:
        """Send the caller what subscribers and movie-goers get. A closed DM is a failure:
        subscribers in the same state get the activity-channel fallback instead."""
        embeds, files = [], []
        if findings.sample_episodes:
            embeds.append(notification_embed(findings.sample_episodes[:8]))
            if findings.poster:
                files.append(discord.File(io.BytesIO(findings.poster), filename="poster.jpg"))
                embeds[0].set_thumbnail(url="attachment://poster.jpg")
        steps = syncplay_steps(f"**{SAMPLE_FILM}**", user.display_name,
                               theatre.mention if theatre else VOICE_CHANNEL)
        embeds.append(screenings.reminder_embed(SAMPLE_FILM, None, steps))
        attended = await watch_data.movie_nights_attended([user.id])
        embeds.append(screenings.recap_embed(SAMPLE_FILM, [(user.id, attended + 1)], []))
        try:
            await user.send(content="🧪 **Self-test samples**: a new-episode DM, a movie night reminder and a "
                                    "recap, as the bot sends them. Nobody else got these.",
                            embeds=embeds, files=files)
            return selftest.Check("DMs, with a poster attached", True, f"{len(embeds)} samples sent to you")
        except discord.HTTPException as e:
            return selftest.Check("DMs, with a poster attached", False,
                                  f"{e.text or e} (subscribers with closed DMs get the activity-channel fallback)")

    @app_commands.command(name="jf-selftest", description="(Owner) Check the bot end to end, Discord included")
    @app_commands.guild_only()
    @owner_only()
    async def selftest_command(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild, me = interaction.guild, interaction.guild.me
        theatre = discord.utils.get(guild.voice_channels, name=VOICE_CHANNEL)
        announce = discord.utils.get(guild.text_channels, name=ANNOUNCE_CHANNEL)

        findings = selftest.Findings()
        if self.bot.jf is None:
            findings.add("Jellyfin answers with the API key", False, "JELLYFIN_API_KEY is not set")
        else:
            await selftest.jellyfin_checks(self.bot.jf, findings)
        selftest.database_checks(findings=findings)

        checks = findings.checks + [
            loop_check(self.bot, "MovieNight", "follow_nights", "Movie night loop"),
            loop_check(self.bot, "Subscriptions", "check_new_episodes", "New-episode loop"),
            posting_check("Posts and pings in #movie-night", announce, me, role(guild, TICKET_ROLE)),
        ]
        activity = self.bot.get_channel(config.ACTIVITY_CHANNEL_ID) if config.ACTIVITY_CHANNEL_ID else None
        checks.append(posting_check("Posts in the activity channel", activity, me) if config.ACTIVITY_CHANNEL_ID
                      else selftest.Check("Posts in the activity channel", None, "ACTIVITY_CHANNEL_ID is 0"))
        admin = self.bot.get_channel(config.ADMIN_CHANNEL_ID) if config.ADMIN_CHANNEL_ID else None
        checks.append(posting_check("Posts in the admin channel", admin, me) if config.ADMIN_CHANNEL_ID
                      else selftest.Check("Posts in the admin channel", None, "ADMIN_CHANNEL_ID is 0"))
        checks.append(loop_check(self.bot, "Admin", "weekly_summary", "Admin summary loop") if config.ADMIN_CHANNEL_ID
                      else selftest.Check("Admin summary loop", None, "ADMIN_CHANNEL_ID is 0"))
        checks.append(audience_check(guild, theatre, interaction.user.id, self.bot.user.id))
        checks.append(await event_round_trip(guild, theatre, discord.utils.utcnow()))
        checks.append(await self.dm_samples(interaction.user, findings, guild, theatre))

        failed = [c for c in checks if c.ok is False]
        print(f"/jf-selftest by {interaction.user}: {len(failed)} failed"
              + "".join(f"\n  {c.line}" for c in failed))
        await interaction.followup.send(embed=report(checks), ephemeral=True)


async def setup(bot):
    await bot.add_cog(SelfTest(bot))
