"""Movie night: themed roles, a role picker in #roles, /movienight and /movievote.

The bot creates the roles and channels below at start-up if they are missing,
and never deletes anything. The names here are the source of truth: rename a
role or channel in Discord and the bot creates the old name again on its next
start, so rename it here instead.
"""
import io
import os
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

TZ = ZoneInfo("America/Toronto")
# Where people open Jellyfin from, e.g. https://jellyfin.example.com. Used for
# links only, never for images. Empty means announcements carry no link.
PUBLIC_URL = os.getenv("JELLYFIN_PUBLIC_URL", "").rstrip("/")

ROLES_CHANNEL = "roles"
CATEGORY = "🎬 Movie Night"
ANNOUNCE_CHANNEL = "movie-night"
CHAT_CHANNEL = "watch-chat"
VOICE_CHANNEL = "🎥 Theatre"

TICKET_ROLE = "🎟️ Ticket Holder"   # pinged for movie nights; opt-in from #roles

# Staff, handed out by the owner, never picked. Both may schedule movie nights.
# The bot can only grant permissions it holds itself, so no kick, ban or Administrator.
DIRECTOR_ROLE = "🎬 Director"      # admins
USHER_ROLE = "🔦 Usher"            # mods: keep order in chat and voice
STAFF = [
    (DIRECTOR_ROLE, 0xF1C40F, discord.Permissions(
        manage_channels=True, manage_roles=True, manage_messages=True,
        manage_events=True, create_events=True, moderate_members=True)),
    (USHER_ROLE, 0xC0392B, discord.Permissions(
        manage_messages=True, moderate_members=True, create_events=True)),
]

PICKER_TITLE = "🎭 Pick your flair"


@dataclass(frozen=True)
class Flair:
    emoji: str
    label: str
    colour: int
    blurb: str

    @property
    def role_name(self) -> str:
        return f"{self.emoji} {self.label}"


# Cosmetic only: the friend everyone has on movie night. Up to 25 fit in one menu.
# Spoiler Alert doubles as a punishment role: an Usher can hand it out for a while.
FLAIRS = [
    Flair("🗣️", "Talks During the Movie", 0xE67E22, "The friend who narrates everything"),
    Flair("🍿", "Popcorn Hog", 0xF4C430, "Always has snacks, or spams the chat"),
    Flair("😴", "Fell Asleep in the First Act", 0x95A5A6, "Always dozes off on movie night"),
    Flair("⏰", "Late Arrival", 0x3498DB, "Shows up 30 minutes in, every time"),
    Flair("🙊", "Spoiler Alert", 0x2ECC71, "Ruins endings. Also a punishment role"),
    Flair("🚽", "Bathroom Break", 0x8D6E63, "Always away at the worst moment"),
    Flair("🗑️", "Picks the Worst Movies", 0x7F8C8D, "Self-explanatory"),
    Flair("😭", "Cries at Everything", 0x5DADE2, "The soft one of the group"),
    Flair("⏳", "Credits Watcher", 0x1ABC9C, "Sticks around for the post-credits scene"),
    Flair("📱", "Phone Screen Glow", 0x9B59B6, "On their phone the whole movie"),
    Flair("🔍", "Where Do I Know Them From", 0xE91E63, "Pauses to look up the actor"),
    Flair("⏪", "Wait, Rewind That", 0xD35400, "Missed it. Needs it again"),
    Flair("❓", "Explain the Ending", 0xFF8FB1, "Still confused after the credits"),
]
FLAIR_BY_LABEL = {f.label: f for f in FLAIRS}

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
JELLYFIN_ID = re.compile(r"^[0-9a-f]{32}$")


# --- helpers ---------------------------------------------------------------

def role(guild: discord.Guild, name: str) -> discord.Role | None:
    return discord.utils.get(guild.roles, name=name)


def can_host(member: discord.Member) -> bool:
    return member.guild_permissions.manage_events or any(r.name in (DIRECTOR_ROLE, USHER_ROLE) for r in member.roles)


def film_label(item: dict) -> str:
    year = item.get("ProductionYear")
    return f"{item.get('Name', 'Unknown')} ({year})" if year else item.get("Name", "Unknown")


def parse_day(text: str, now: datetime):
    s = text.strip().lower()
    if s in ("today", "tonight"):
        return now.date(), False
    if s == "tomorrow":
        return (now + timedelta(days=1)).date(), False
    if len(s) >= 3:
        for i, name in enumerate(WEEKDAYS):
            if name.startswith(s):
                return (now + timedelta(days=(i - now.weekday()) % 7)).date(), True
    return datetime.strptime(s, "%Y-%m-%d").date(), False


def parse_time(text: str):
    """'8pm', '8:30pm', '20:00'. A bare '8' or '8:30' means the evening."""
    m = re.fullmatch(r"\s*(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?\s*", text.lower())
    if not m:
        raise ValueError(text)
    hour, minute, ampm = int(m[1]), int(m[2] or 0), m[3]
    if ampm:
        if not 1 <= hour <= 12:
            raise ValueError(text)
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    elif 1 <= hour <= 11:
        hour += 12
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(text)
    return hour, minute


def parse_when(day: str, at: str) -> datetime:
    now = datetime.now(TZ)
    date, is_weekday = parse_day(day, now)
    hour, minute = parse_time(at)
    when = datetime(date.year, date.month, date.day, hour, minute, tzinfo=TZ)
    if when <= now and is_weekday:
        when += timedelta(days=7)   # "friday" said on a Friday evening means next week
    if when <= now:
        raise ValueError("past")
    return when


# --- the role picker in #roles ---------------------------------------------

def picker_embed() -> discord.Embed:
    lines = "\n".join(f"{f.emoji} **{f.label}**: {f.blurb}" for f in FLAIRS)
    return discord.Embed(
        title=PICKER_TITLE,
        description=(
            "Which movie night friend are you? These don't unlock anything, they just go on your "
            "profile. Pick as many as you like from the menu; picking again replaces your set.\n\n"
            f"{lines}\n\n"
            f"**{TICKET_ROLE}** is the one that does something: you get pinged when a movie night "
            "is scheduled. Use the button to get it or drop it.\n\n"
            f"Staff: **{DIRECTOR_ROLE}** runs the place, **{USHER_ROLE}** keeps order."
        ),
        colour=discord.Colour(0xF4C430),
    )


class RolePicker(discord.ui.View):
    """Persistent: the custom_ids are fixed, so the buttons keep working after a restart."""

    def __init__(self):
        super().__init__(timeout=None)
        menu = discord.ui.Select(
            custom_id="movienight:flairs",
            placeholder="Choose your flairs…",
            min_values=0,
            max_values=len(FLAIRS),
            options=[discord.SelectOption(label=f.label, value=f.label, emoji=f.emoji, description=f.blurb)
                     for f in FLAIRS],
            row=0,
        )
        menu.callback = self.on_flairs
        self.add_item(menu)

    async def on_flairs(self, interaction: discord.Interaction):
        chosen = [FLAIR_BY_LABEL[v] for v in interaction.data.get("values", []) if v in FLAIR_BY_LABEL]
        await set_flairs(interaction, chosen)

    @discord.ui.button(label="Ping me for movie nights", emoji="🎟️", style=discord.ButtonStyle.primary,
                       custom_id="movienight:ticket", row=1)
    async def ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        member, ticket = interaction.user, role(interaction.guild, TICKET_ROLE)
        if ticket is None:
            await interaction.response.send_message("The Ticket Holder role is missing. Ask an admin.", ephemeral=True)
            return
        if ticket in member.roles:
            await member.remove_roles(ticket, reason="Role picker")
            await interaction.response.send_message("No more movie night pings. 🎟️ ticket returned.", ephemeral=True)
        else:
            await member.add_roles(ticket, reason="Role picker")
            await interaction.response.send_message("🎟️ You're on the list: you'll be pinged for movie nights.", ephemeral=True)

    @discord.ui.button(label="Remove my flairs", style=discord.ButtonStyle.secondary,
                       custom_id="movienight:clear", row=1)
    async def clear(self, interaction: discord.Interaction, button: discord.ui.Button):
        await set_flairs(interaction, [])


async def set_flairs(interaction: discord.Interaction, chosen: list[Flair]):
    guild, member = interaction.guild, interaction.user
    wanted = {r for f in chosen if (r := role(guild, f.role_name))}
    every = {r for f in FLAIRS if (r := role(guild, f.role_name))}
    add = [r for r in wanted if r not in member.roles]
    drop = [r for r in every - wanted if r in member.roles]
    if add:
        await member.add_roles(*add, reason="Role picker")
    if drop:
        await member.remove_roles(*drop, reason="Role picker")
    if wanted:
        names = ", ".join(f.role_name for f in chosen)
        await interaction.response.send_message(f"Your flair: {names}", ephemeral=True)
    else:
        await interaction.response.send_message("Flairs cleared.", ephemeral=True)


# --- creating what is missing ----------------------------------------------

async def ensure_structure(bot: discord.Client):
    for guild in bot.guilds:
        try:
            await _ensure_guild(bot, guild)
        except discord.Forbidden as e:
            print(f"Movie night setup in {guild.name}: missing permission ({e})")


async def _ensure_guild(bot: discord.Client, guild: discord.Guild):
    why = "Movie night setup"
    wanted = [(TICKET_ROLE, 0, True)] + [(f.role_name, f.colour, False) for f in FLAIRS]
    for name, colour, mentionable in wanted:
        if role(guild, name) is None:
            await guild.create_role(name=name, colour=discord.Colour(colour), mentionable=mentionable, reason=why)
            print(f"Created role {name}")
    # Staff are hoisted (listed apart in the member list) and sit right under the
    # bot, so their colour wins over any flair. Only set when created: changes
    # made in Discord afterwards are left alone.
    new_staff = False
    for name, colour, perms in STAFF:
        if role(guild, name) is None:
            await guild.create_role(name=name, colour=discord.Colour(colour), permissions=perms,
                                    hoist=True, reason=why)
            print(f"Created role {name}")
            new_staff = True
    if new_staff:
        top = guild.me.top_role.position
        await guild.edit_role_positions(
            {role(guild, name): top - 1 - i for i, (name, _, _) in enumerate(STAFF)}, reason=why)

    everyone, me = guild.default_role, guild.me
    director, usher = role(guild, DIRECTOR_ROLE), role(guild, USHER_ROLE)
    read_only = {
        everyone: discord.PermissionOverwrite(send_messages=False, create_public_threads=False,
                                              create_private_threads=False),
        me: discord.PermissionOverwrite(send_messages=True, embed_links=True, attach_files=True,
                                        mention_everyone=True),
    }

    category = discord.utils.get(guild.categories, name=CATEGORY)
    if category is None:
        category = await guild.create_category(CATEGORY, reason=why)
        print(f"Created category {CATEGORY}")
    announce = discord.utils.get(guild.text_channels, name=ANNOUNCE_CHANNEL)
    if announce is None:
        announce = await guild.create_text_channel(
            ANNOUNCE_CHANNEL, category=category, reason=why, overwrites=read_only,
            topic="Movie nights: what's on and when. Schedule one with /movienight, or let people vote with /movievote.")
        print(f"Created #{ANNOUNCE_CHANNEL}")
    for staff in (director, usher):   # staff may post announcements too
        if staff and announce.overwrites_for(staff).send_messages is not True:
            await announce.set_permissions(staff, send_messages=True, reason=why)
    if discord.utils.get(guild.text_channels, name=CHAT_CHANNEL) is None:
        await guild.create_text_channel(CHAT_CHANNEL, category=category, reason=why,
                                        topic="Talk during the film. Spoilers allowed in here.")
        print(f"Created #{CHAT_CHANNEL}")
    if discord.utils.get(guild.voice_channels, name=VOICE_CHANNEL) is None:
        await guild.create_voice_channel(VOICE_CHANNEL, category=category, reason=why)
        print(f"Created {VOICE_CHANNEL}")

    roles_channel = discord.utils.get(guild.text_channels, name=ROLES_CHANNEL)
    if roles_channel is None:
        return
    if roles_channel.overwrites_for(everyone).send_messages is not False:
        await roles_channel.edit(overwrites={**roles_channel.overwrites, **read_only}, reason=why)
    # One picker message, edited in place so a change to FLAIRS shows up after a restart.
    async for message in roles_channel.history(limit=50):
        if message.author == bot.user and message.embeds and message.embeds[0].title == PICKER_TITLE:
            await message.edit(embed=picker_embed(), view=RolePicker())
            return
    await roles_channel.send(embed=picker_embed(), view=RolePicker())
    print(f"Posted the role picker in #{ROLES_CHANNEL}")


# --- slash commands --------------------------------------------------------

def register(bot: discord.Client, jf):
    """Add the commands to bot.tree. Call before the bot starts."""

    async def film_choices(term: str):
        if jf is None:
            return []
        try:
            items = await jf.search_movies(term, limit=25)
        except Exception:
            return []
        return [app_commands.Choice(name=film_label(i)[:100], value=i["Id"]) for i in items]

    async def resolve_film(value: str) -> dict | None:
        """The autocomplete gives an item id; typed text without picking is searched instead."""
        if JELLYFIN_ID.match(value):
            item = await jf.get_item(value)
            if item:
                return item
        found = await jf.search_movies(value, limit=1)
        return found[0] if found else None

    def channels(guild):
        return (discord.utils.get(guild.text_channels, name=ANNOUNCE_CHANNEL),
                discord.utils.get(guild.voice_channels, name=VOICE_CHANNEL))

    @bot.tree.command(name="movienight", description="Schedule a movie night: makes an event and pings Ticket Holders")
    @app_commands.guild_only()
    @app_commands.describe(film="A film from the Jellyfin library",
                           day="Tonight, tomorrow, a weekday, or a date like 2026-10-31",
                           time="Start time, e.g. 8pm or 20:00 (a bare 8 means 8pm)")
    async def movienight(interaction: discord.Interaction, film: str, day: str, time: str):
        if not can_host(interaction.user):
            await interaction.response.send_message(f"Only a {DIRECTOR_ROLE} or {USHER_ROLE} can schedule movie nights.", ephemeral=True)
            return
        try:
            start = parse_when(day, time)
        except ValueError as e:
            msg = ("That time has already passed." if str(e) == "past"
                   else "I couldn't read that. Try a day like `friday` or `2026-10-31` and a time like `8pm`.")
            await interaction.response.send_message(msg, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        item = await resolve_film(film)
        if not item:
            await interaction.followup.send(f"No film called “{film}” in Jellyfin.", ephemeral=True)
            return
        guild = interaction.guild
        announce, theatre = channels(guild)
        if announce is None or theatre is None:
            await interaction.followup.send("The Movie Night channels are missing. Restart the bot to recreate them.", ephemeral=True)
            return

        title = film_label(item)
        minutes = int((item.get("RunTimeTicks") or 0) / 600_000_000) or 120
        overview = (item.get("Overview") or "").strip()
        genres = ", ".join(item.get("Genres") or []) or "—"
        link = f"{PUBLIC_URL}/web/#/details?id={item['Id']}" if PUBLIC_URL else None
        film_ref = f"[{item.get('Name')}]({link})" if link else f"**{item.get('Name')}**"
        backdrop = await jf.get_image(item["Id"], "Backdrop") or await jf.get_image(item["Id"], "Primary")
        poster = await jf.get_image(item["Id"], "Primary", max_width=600)

        event = await guild.create_scheduled_event(
            name=f"🎬 {title}"[:100],
            start_time=start,
            end_time=start + timedelta(minutes=minutes + 15),
            entity_type=discord.EntityType.voice,
            channel=theatre,
            privacy_level=discord.PrivacyLevel.guild_only,
            description=(f"{overview[:600]}\n\n" if overview else "")
            + "Open this film in Jellyfin yourself (SyncPlay won't open it for you), join the "
            f"SyncPlay group, then press play. Talk along in {VOICE_CHANNEL}.",
            reason=f"/movienight by {interaction.user}",
            **({"image": backdrop} if backdrop else {}),
        )

        unix = int(start.timestamp())
        embed = discord.Embed(
            title=f"🎬 Movie Night: {title}",
            url=link,
            description=(overview[:350] + ("…" if len(overview) > 350 else "")) or None,
            colour=discord.Colour(0xF4C430),
        )
        embed.add_field(name="When", value=f"<t:{unix}:F>\n<t:{unix}:R>", inline=True)
        embed.add_field(name="Runtime", value=f"{minutes // 60}h {minutes % 60:02d}m", inline=True)
        embed.add_field(name="Genre", value=genres[:100], inline=True)
        embed.add_field(
            name="How to watch together",
            # Tested 2026-10-22: SyncPlay does not open the film for the others.
            # Everyone has to be on the same film before playback is synced.
            value=(f"1. **Everyone** opens {film_ref} in Jellyfin. SyncPlay won't open it for you\n"
                   f"2. Tap **SyncPlay** (the group icon at the top) and join "
                   f"**{interaction.user.display_name}**'s group, or start one if you're first\n"
                   f"3. Once everyone's in, press play: pausing and seeking stay in sync\n"
                   f"4. Hop into {theatre.mention} to talk along\n"
                   f"Press **Interested** on the event to get a reminder."),
            inline=False,
        )
        embed.set_footer(text=f"Hosted by {interaction.user.display_name}")
        files = []
        if poster:
            files.append(discord.File(io.BytesIO(poster), filename="poster.jpg"))
            embed.set_thumbnail(url="attachment://poster.jpg")

        ticket = role(guild, TICKET_ROLE)
        await announce.send(
            content=f"{ticket.mention if ticket else ''} movie night is on! {event.url}".strip(),
            embed=embed, files=files,
            allowed_mentions=discord.AllowedMentions(roles=[ticket] if ticket else [], everyone=False, users=False),
        )
        await interaction.followup.send(f"Scheduled **{title}** for <t:{unix}:F>, announced in {announce.mention}.", ephemeral=True)

    @movienight.autocomplete("film")
    async def movienight_film(interaction: discord.Interaction, current: str):
        return await film_choices(current)

    @movienight.autocomplete("day")
    async def movienight_day(interaction: discord.Interaction, current: str):
        today = datetime.now(TZ).date()
        options = []
        for n in range(14):
            d = today + timedelta(days=n)
            label = "Tonight" if n == 0 else "Tomorrow" if n == 1 else d.strftime("%A")
            options.append(app_commands.Choice(name=f"{label} ({d.strftime('%b')} {d.day})", value=d.isoformat()))
        return [o for o in options if current.lower() in o.name.lower()][:25]

    @bot.tree.command(name="movievote", description="Post a poll of films for the next movie night")
    @app_commands.guild_only()
    @app_commands.describe(film1="A film (leave the films empty for a random pick)",
                           film2="Another film", film3="Another film", film4="Another film",
                           hours="How long the vote stays open (default 24)")
    async def movievote(interaction: discord.Interaction, film1: str | None = None, film2: str | None = None,
                        film3: str | None = None, film4: str | None = None,
                        hours: app_commands.Range[int, 1, 168] = 24):
        if not can_host(interaction.user):
            await interaction.response.send_message(f"Only a {DIRECTOR_ROLE} or {USHER_ROLE} can start a movie vote.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        announce, _ = channels(interaction.guild)
        if announce is None:
            await interaction.followup.send("The Movie Night channels are missing. Restart the bot to recreate them.", ephemeral=True)
            return

        picks, seen = [], set()
        for value in (film1, film2, film3, film4):
            if value and (item := await resolve_film(value)) and item["Id"] not in seen:
                picks.append(item)
                seen.add(item["Id"])
        if len(picks) < 2:
            for item in await jf.random_movies(10):
                if len(picks) >= 4:
                    break
                if item["Id"] not in seen:
                    picks.append(item)
                    seen.add(item["Id"])
        if len(picks) < 2:
            await interaction.followup.send("Not enough films in the library to vote on.", ephemeral=True)
            return

        poll = discord.Poll(question="What are we watching next? 🍿", duration=timedelta(hours=hours))
        for item in picks:
            poll.add_answer(text=film_label(item)[:55], emoji="🎬")
        ticket = role(interaction.guild, TICKET_ROLE)
        await announce.send(
            content=f"{ticket.mention if ticket else ''} vote for the next movie night!".strip(),
            poll=poll,
            allowed_mentions=discord.AllowedMentions(roles=[ticket] if ticket else [], everyone=False, users=False),
        )
        await interaction.followup.send(f"Vote posted in {announce.mention}, open for {hours}h.", ephemeral=True)

    async def movievote_film(interaction: discord.Interaction, current: str):
        return await film_choices(current)

    for name in ("film1", "film2", "film3", "film4"):
        movievote.autocomplete(name)(movievote_film)
