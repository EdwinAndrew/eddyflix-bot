"""Movie night: themed roles, a role picker and a getting-started guide in #roles,
/movienight and /movievote,
and following each night through: a reminder, the start, and a recap.

The bot creates the roles and channels below at start-up if they are missing,
and never deletes anything. The names here are the source of truth: rename a
role or channel in Discord and the bot creates the old name again on its next
start, so rename it here instead.
"""
import io
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import ratings
import screenings
import watch_data

TZ = ZoneInfo("America/Toronto")

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
GUIDE_TITLE = "👋 New here? Start watching"
LINK_LABEL = "Which Jellyfin user are you?"


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
SPOILER_FLAIR = FLAIR_BY_LABEL["Spoiler Alert"]   # what /spoiler hands out

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
JELLYFIN_ID = re.compile(r"^[0-9a-f]{32}$")


# --- helpers ---------------------------------------------------------------

def role(guild: discord.Guild, name: str) -> discord.Role | None:
    return discord.utils.get(guild.roles, name=name)


def can_host(member: discord.Member) -> bool:
    return member.guild_permissions.manage_events or any(r.name in (DIRECTOR_ROLE, USHER_ROLE) for r in member.roles)


def staff_only():
    """Hides a staff command (/movienight, /movievote, /spoiler, /unspoiler) from members:
    Discord lists it only for people with Timeout Members, which both staff roles have and
    members don't. Visibility only: each command still checks can_host."""
    return app_commands.default_permissions(moderate_members=True)


def details_link(item_id: str) -> str | None:
    """The item's page in Jellyfin's web app, or None without JELLYFIN_PUBLIC_URL."""
    return f"{config.JELLYFIN_PUBLIC_URL}/web/#/details?id={item_id}" if config.JELLYFIN_PUBLIC_URL else None


def theatre_audience(guild: discord.Guild, theatre, bot_id: int) -> list[int]:
    """Discord ids of the people in the Theatre voice channel, bots left out.
    Read from voice states, which need no members intent."""
    return [uid for uid in (theatre.voice_states if theatre else {})
            if uid != bot_id and not getattr(guild.get_member(uid), "bot", False)]


def film_ref(name: str, link: str | None) -> str:
    return f"[{name}]({link})" if link else f"**{name}**"


def syncplay_steps(film: str, host: str | None, theatre: str) -> str:
    """How to watch together, for the announcement and the reminder. host is the
    display name of whoever scheduled it, if known."""
    # Tested 2026-10-22: SyncPlay does not open the film for the others.
    # Everyone has to be on the same film before playback is synced.
    group = f"**{host}**'s group" if host else "the group"
    return (f"1. **Everyone** opens {film} in Jellyfin. SyncPlay won't open it for you\n"
            f"2. Tap **SyncPlay** (the group icon at the top) and join {group}, or start one if you're first\n"
            f"3. Once everyone's in, press play: pausing and seeking stay in sync\n"
            f"4. Hop into {theatre} to talk along")


def film_label(item: dict) -> str:
    name, year = item.get("Name", "Unknown"), item.get("ProductionYear")
    # Some names carry the year already: "Yellowstone (2018)"
    return f"{name} ({year})" if year and not name.endswith(f"({year})") else name


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


def parse_when(day: str, at: str, now: datetime | None = None) -> datetime:
    now = now or datetime.now(TZ)
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
            f"**👤 {LINK_LABEL}** tells the bot who you are on Jellyfin, for `/watch`, achievements, "
            "new-episode DMs and movie night attendance.\n\n"
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
        await set_flairs(interaction, chosen, await spoiler_lock(interaction))

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
        await set_flairs(interaction, [], await spoiler_lock(interaction))

    @discord.ui.button(label=LINK_LABEL, emoji="👤", style=discord.ButtonStyle.success,
                       custom_id="movienight:link", row=2)
    async def link(self, interaction: discord.Interaction, button: discord.ui.Button):
        await offer_link(interaction)


async def spoiler_lock(interaction: discord.Interaction) -> int | None:
    """When the member's Spoiler Alert from /spoiler runs out, if it is running."""
    try:
        return await watch_data.spoiler_until(interaction.guild.id, interaction.user.id, int(time.time()))
    except Exception as e:
        print(f"Role picker: could not check /spoiler ({e})")
        return None


async def set_flairs(interaction: discord.Interaction, chosen: list[Flair], locked_until: int | None = None):
    """Give the member exactly these flairs. locked_until: Spoiler Alert from /spoiler
    is running until then (Unix time), so it stays on whatever they pick."""
    kept = locked_until is not None and SPOILER_FLAIR not in chosen
    others = list(chosen)
    if kept:
        chosen = [*chosen, SPOILER_FLAIR]
    guild, member = interaction.guild, interaction.user
    wanted = {r for f in chosen if (r := role(guild, f.role_name))}
    every = {r for f in FLAIRS if (r := role(guild, f.role_name))}
    add = [r for r in wanted if r not in member.roles]
    drop = [r for r in every - wanted if r in member.roles]
    if add:
        await member.add_roles(*add, reason="Role picker")
    if drop:
        await member.remove_roles(*drop, reason="Role picker")
    if kept:
        first = f"Your flair: {', '.join(f.role_name for f in others)}" if others else "Flairs cleared"
        await interaction.response.send_message(
            f"{first}, but **{SPOILER_FLAIR.role_name}** stays on until <t:{locked_until}:f> "
            f"(<t:{locked_until}:R>). Staff gave it to you with `/spoiler`, so it can't come off before then.",
            ephemeral=True)
    elif wanted:
        names = ", ".join(f.role_name for f in chosen)
        await interaction.response.send_message(f"Your flair: {names}", ephemeral=True)
    else:
        await interaction.response.send_message("Flairs cleared.", ephemeral=True)


# --- linking a Jellyfin user from #roles -------------------------------------
# The same as /link, for people who don't know the command. The button is on
# the picker, so it survives restarts with it; the menu it answers with is made
# fresh each time, so it always lists who is still free.

LINK_MENU_LIMIT = 25   # options in one select menu


def link_choices(names: list[str], linked: set[str]) -> tuple[list[str], bool]:
    """Jellyfin users nobody is linked to yet (linked is lower case), the first 25
    by name, and whether some did not fit."""
    free = sorted((n for n in names if n.lower() not in linked and len(n) <= 100), key=str.lower)
    return free[:LINK_MENU_LIMIT], len(free) > LINK_MENU_LIMIT


def link_prompt(mine: str | None, free: list[str], more: bool) -> str:
    """What the link button answers. mine is the member's current link, if any."""
    if mine:
        text = f"You're **{mine}** on Jellyfin."
        text += " Not right? Pick yourself below." if free else " Not right? Ask the server owner to fix it."
    elif free:
        text = "Which Jellyfin user are you? Only the ones nobody has claimed yet are listed."
    else:
        text = ("Every Jellyfin user is linked to someone already. If yours is taken, ask the server owner. "
                f"No account yet? Ask the server owner or a **{DIRECTOR_ROLE}**.")
    if more:
        text += "\nNot in the list? Use `/link` with your Jellyfin user name."
    return text


def linked_message(name: str) -> str:
    return (f"You're now **{name}** on Jellyfin. `/watch` shows where you left off, `/achievements` "
            "shows your card, and movie nights count towards it.")


class LinkMenu(discord.ui.View):
    """The menu the link button answers with, seen only by whoever pressed it."""

    def __init__(self, names: list[str]):
        super().__init__(timeout=300)
        menu = discord.ui.Select(placeholder="I'm…",
                                 options=[discord.SelectOption(label=n, value=n) for n in names])
        menu.callback = self.on_pick
        self.add_item(menu)

    async def on_pick(self, interaction: discord.Interaction):
        name = interaction.data["values"][0]
        mine = await watch_data.get_link(interaction.user.id)
        # Someone may have picked the same user since the menu was made
        if name.lower() in await watch_data.linked_jellyfin_users() and (mine or "").lower() != name.lower():
            await interaction.response.edit_message(
                content=f"Someone has just claimed **{name}**. If that's you, ask the server owner.", view=None)
            return
        await watch_data.set_link(interaction.user.id, name)
        await interaction.response.edit_message(content=linked_message(name), view=None)


async def offer_link(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    jf = getattr(interaction.client, "jf", None)
    try:
        names = await jf.get_user_names() if jf else []
    except Exception:
        names = []
    if not names:
        await interaction.followup.send("I can't reach Jellyfin right now. Try again later, or use `/link`.",
                                        ephemeral=True)
        return
    mine = await watch_data.get_link(interaction.user.id)
    free, more = link_choices(names, await watch_data.linked_jellyfin_users())
    await interaction.followup.send(link_prompt(mine, free, more), ephemeral=True,
                                    **({"view": LinkMenu(free)} if free else {}))


# --- the getting-started guide in #roles -------------------------------------

def guide_embed(owner_id: int | None = None) -> discord.Embed:
    """How to get an account and start watching, then what to do here. Posted in
    #roles next to the picker and edited in place the same way, so a change to the
    text or the addresses shows up after a restart."""
    owner = f"<@{owner_id}>" if owner_id else "the server owner"
    url = config.JELLYFIN_PUBLIC_URL
    if url:
        port = " Type it exactly, port and all." if urlsplit(url).port else ""
        address = f"Server address: **{url}**.{port}"
    else:
        address = "Ask for the server address when you get your account."
    embed = discord.Embed(
        title=GUIDE_TITLE,
        description="EddyFlix is a private Jellyfin server: films and shows, streamed to whatever you watch on.",
        colour=discord.Colour(0x00A4DC),   # Jellyfin blue
    )
    embed.add_field(name="1. Get an account", inline=False, value=(
        f"Ask {owner} or a **{DIRECTOR_ROLE}** for a Jellyfin login. There's no sign-up page."))
    embed.add_field(name="2. Get the app", inline=False, value=(
        "📺 **LG TV**: the *Jellyfin* app from the LG Content Store (webOS)\n"
        "📱 **iPhone**: the *Jellyfin* app from the App Store\n"
        "💻 **Browser**: no app needed, open the address below"))
    embed.add_field(name="3. Connect", inline=False, value=f"{address}\nThen sign in with your login.")
    if config.SEERR_PUBLIC_URL:
        embed.add_field(name="Something missing?", inline=False, value=(
            f"Request it on Seerr: **{config.SEERR_PUBLIC_URL}**, with the same login."))
    who, it = ("Jellyfin and Seerr only answer", "they") if config.SEERR_PUBLIC_URL else ("Jellyfin only answers", "it")
    embed.add_field(name="🇨🇦 Canada only", inline=False, value=(
        f"{who} from Canada. Abroad, or on a VPN that comes out somewhere else, {it} won't load."))
    embed.add_field(name="Then, here on Discord", inline=False, value=(
        f"👤 **{LINK_LABEL}** on the flair picker in this channel tells the bot who you are on "
        "Jellyfin (or use `/link`). `/watch` progress, achievements, new-episode DMs and "
        "movie night attendance all need it\n"
        f"🎟️ **Ping me for movie nights** gets you a ticket, so you hear when one is on\n"
        "Worth knowing: `/watch` finds something and where you left off · `/subscribe` DMs you "
        "new episodes of a show · `/achievements` shows your watch time and badges · "
        "`/help` lists the rest"))
    return embed


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
    # The guide and the picker: one message each, found by title and edited in
    # place, so a change here shows up after a restart. A new #roles gets the
    # guide first, so it reads top down; an older one gets it under the picker.
    found = {}
    async for message in roles_channel.history(limit=50):
        if message.author == bot.user and message.embeds:
            found.setdefault(message.embeds[0].title, message)
    posts = [(GUIDE_TITLE, "the getting-started guide", {"embed": guide_embed(guild.owner_id)}),
             (PICKER_TITLE, "the role picker", {"embed": picker_embed(), "view": RolePicker()})]
    for title, what, content in posts:
        if title in found:
            await found[title].edit(**content)
        else:
            await roles_channel.send(**content)
            print(f"Posted {what} in #{ROLES_CHANNEL}")


# --- slash commands --------------------------------------------------------

class MovieNight(commands.Cog):
    """/movienight and /movievote, and the loop that follows each night. The #roles
    picker is re-attached in the bot's setup_hook, and the roles and channels are
    created from on_ready."""

    def __init__(self, bot):
        self.bot = bot
        self.empty_since = {}   # event id -> when the Theatre emptied during a live night

    def cog_unload(self):
        self.follow_nights.cancel()

    @commands.Cog.listener()
    async def on_services_ready(self):
        if not self.follow_nights.is_running():
            self.follow_nights.start()

    # --- following a night: reminder, start, recap ---------------------------
    # Every minute. The state is in the database, so a restart neither repeats
    # the reminder or the recap nor loses the minutes counted so far.
    #
    # The night runs on the bot's own clock, from its start to its end time,
    # whatever the Discord event says. Discord ends an active voice event a few
    # minutes after its channel empties, so the event is started only once
    # someone is in the Theatre, and an event Discord has ended (or someone
    # ended by hand) does not end the night: a break would cut the recap short.

    @tasks.loop(minutes=1)
    async def follow_nights(self):
        try:
            await self.adopt_events()
            nights = await watch_data.open_movie_nights()
        except Exception as e:
            print(f"Movie nights: {e}")
            return
        for night in nights:
            try:
                await self.follow(night)
            except Exception as e:
                print(f"Movie night {night['title']}: {e}")

    async def adopt_events(self):
        """Follow the bot's own events that the database does not know: ones made
        before this existed. They carry no host or film id."""
        for guild in self.bot.guilds:
            for event in guild.scheduled_events:
                if (event.creator_id == self.bot.user.id and event.name.startswith("🎬 ")
                        and event.status in (discord.EventStatus.scheduled, discord.EventStatus.active)):
                    title = event.name.removeprefix("🎬 ")
                    end = event.end_time or event.start_time + timedelta(hours=3)
                    await watch_data.add_movie_night(event.id, guild.id, title, title, None, None,
                                                     event.start_time, end, discord.utils.snowflake_time(event.id))

    async def follow(self, night: dict):
        guild = self.bot.get_guild(int(night["guild_id"]))
        if guild is None:
            return
        event_id = int(night["event_id"])
        event = guild.get_scheduled_event(event_id)
        if event is None:   # not cached: finished while the bot was away, or deleted
            try:
                event = await guild.fetch_scheduled_event(event_id, with_counts=False)
            except discord.NotFound:
                await self.close_night(night, "gone")
                return
        if event.status is discord.EventStatus.cancelled:
            await self.close_night(night, "cancelled")
            return
        live = night["status"] == "live" or event.status is discord.EventStatus.active

        # Someone may have moved the event in Discord
        start, end = event.start_time, event.end_time or night["end_time"]
        if (start, end) != (night["start_time"], night["end_time"]):
            await watch_data.update_movie_night(event_id, start_time=start, end_time=end)
            night.update(start_time=start, end_time=end)
        now = discord.utils.utcnow()
        theatre = event.channel if isinstance(event.channel, discord.VoiceChannel) else self.channels(guild)[1]

        if not live:
            step = screenings.before_start(now, start, end, night["created_at"], night["reminded"])
            if step == "remind":
                await watch_data.update_movie_night(event_id, reminded=1)
                await self.remind(night, event, guild, theatre)
            elif step == "missed":
                await self.close_night(night, "missed")
            elif step == "start":
                live = True
        if not live:
            return
        if night["status"] != "live":
            await watch_data.update_movie_night(event_id, status="live")
            night["status"] = "live"

        present = theatre_audience(guild, theatre, self.bot.user.id)
        if present and event.status is discord.EventStatus.scheduled:
            try:   # shows the event as live
                await event.start(reason="Movie night time")
            except (discord.HTTPException, ValueError) as e:
                print(f"Could not start the event for {night['title']}: {e}")
        if present:
            self.empty_since.pop(event_id, None)
        elif await watch_data.theatre_minutes(event_id):
            self.empty_since.setdefault(event_id, now)
        if screenings.time_to_end(now, start, end, self.empty_since.get(event_id)):
            await self.wrap_up(night, event, guild)
            return
        await watch_data.add_theatre_minutes(event_id, present)

    async def close_night(self, night: dict, status: str):
        await watch_data.update_movie_night(night["event_id"], status=status)
        self.empty_since.pop(int(night["event_id"]), None)
        print(f"Movie night {night['title']}: {status}")

    async def remind(self, night: dict, event: discord.ScheduledEvent, guild: discord.Guild, theatre):
        announce = self.channels(guild)[0]
        if announce is None:
            return
        link = details_link(night["item_id"]) if night["item_id"] else None
        steps = syncplay_steps(film_ref(night["film_name"], link), night["host"],
                               theatre.mention if theatre else VOICE_CHANNEL)
        ticket = role(guild, TICKET_ROLE)
        unix = int(night["start_time"].timestamp())
        await announce.send(
            content=f"{ticket.mention if ticket else ''} **{night['title']}** starts <t:{unix}:R>. {event.url}".strip(),
            embed=screenings.reminder_embed(night["title"], link, steps),
            allowed_mentions=discord.AllowedMentions(roles=[ticket] if ticket else [], everyone=False, users=False),
        )

    async def wrap_up(self, night: dict, event: discord.ScheduledEvent, guild: discord.Guild):
        """End the event if it is still live and post who came. Marked done first,
        so a restart halfway through cannot post the recap twice."""
        await self.close_night(night, "done")
        if event.status is discord.EventStatus.active:
            try:
                await event.end(reason="Movie night over")
            except (discord.HTTPException, ValueError) as e:
                print(f"Could not end the event for {night['title']}: {e}")
        attended, dropped = screenings.split_attendance(await watch_data.theatre_minutes(night["event_id"]))
        announce = self.channels(guild)[0]
        if announce is None or not (attended or dropped):
            return   # nobody was in the Theatre: nothing to recap
        counts = [(uid, await watch_data.movie_nights_attended([uid])) for uid in attended]
        embed = screenings.recap_embed(night["title"], counts, dropped)
        # 1-5 🍿 for a day (#50). The buttons outlive restarts, see ratings.py
        embed.add_field(name=ratings.FIELD, value=ratings.field_text([], discord.utils.utcnow() + ratings.WINDOW),
                        inline=False)
        await announce.send(embed=embed, view=ratings.rating_view(night["event_id"]),
                            allowed_mentions=discord.AllowedMentions.none())


    async def film_choices(self, term: str):
        if self.bot.jf is None:
            return []
        try:
            items = await self.bot.jf.search_movies(term, limit=25)
        except Exception:
            return []
        return [app_commands.Choice(name=film_label(i)[:100], value=i["Id"]) for i in items]

    async def resolve_film(self, value: str) -> dict | None:
        """The autocomplete gives an item id; typed text without picking is searched instead."""
        if JELLYFIN_ID.match(value):
            item = await self.bot.jf.get_item(value)
            if item:
                return item
        found = await self.bot.jf.search_movies(value, limit=1)
        return found[0] if found else None

    def channels(self, guild):
        return (discord.utils.get(guild.text_channels, name=ANNOUNCE_CHANNEL),
                discord.utils.get(guild.voice_channels, name=VOICE_CHANNEL))

    @app_commands.command(name="movienight", description="Schedule a movie night: makes an event and pings Ticket Holders")
    @app_commands.guild_only()
    @staff_only()
    @app_commands.describe(film="A film from the Jellyfin library",
                           day="Tonight, tomorrow, a weekday, or a date like 2026-10-31",
                           time="Start time, e.g. 8pm or 20:00 (a bare 8 means 8pm)")
    async def movienight(self, interaction: discord.Interaction, film: str, day: str, time: str):
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

        item = await self.resolve_film(film)
        if not item:
            await interaction.followup.send(f"No film called “{film}” in Jellyfin.", ephemeral=True)
            return
        guild = interaction.guild
        announce, theatre = self.channels(guild)
        if announce is None or theatre is None:
            await interaction.followup.send("The Movie Night channels are missing. Restart the bot to recreate them.", ephemeral=True)
            return

        title = film_label(item)
        minutes = int((item.get("RunTimeTicks") or 0) / 600_000_000) or 120
        overview = (item.get("Overview") or "").strip()
        genres = ", ".join(item.get("Genres") or []) or "—"
        link = details_link(item["Id"])
        backdrop = await self.bot.jf.get_image(item["Id"], "Backdrop") or await self.bot.jf.get_image(item["Id"], "Primary")
        poster = await self.bot.jf.get_image(item["Id"], "Primary", max_width=600)

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
        await watch_data.add_movie_night(event.id, guild.id, title, item.get("Name"), item["Id"],
                                         interaction.user.display_name, event.start_time, event.end_time,
                                         discord.utils.snowflake_time(event.id))

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
            value=syncplay_steps(film_ref(item.get("Name"), link), interaction.user.display_name, theatre.mention)
            + f"\n{TICKET_ROLE}s get a ping 15 minutes before; press **Interested** on the event "
            "for Discord's own reminder.",
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
    async def movienight_film(self, interaction: discord.Interaction, current: str):
        return await self.film_choices(current)

    @movienight.autocomplete("day")
    async def movienight_day(self, interaction: discord.Interaction, current: str):
        today = datetime.now(TZ).date()
        options = []
        for n in range(14):
            d = today + timedelta(days=n)
            label = "Tonight" if n == 0 else "Tomorrow" if n == 1 else d.strftime("%A")
            options.append(app_commands.Choice(name=f"{label} ({d.strftime('%b')} {d.day})", value=d.isoformat()))
        return [o for o in options if current.lower() in o.name.lower()][:25]

    @app_commands.command(name="movievote", description="Post a poll of films for the next movie night")
    @app_commands.guild_only()
    @staff_only()
    @app_commands.describe(film1="A film (leave the films empty for a random pick)",
                           film2="Another film", film3="Another film", film4="Another film",
                           hours="How long the vote stays open (default 24)")
    async def movievote(self, interaction: discord.Interaction, film1: str | None = None, film2: str | None = None,
                        film3: str | None = None, film4: str | None = None,
                        hours: app_commands.Range[int, 1, 168] = 24):
        if not can_host(interaction.user):
            await interaction.response.send_message(f"Only a {DIRECTOR_ROLE} or {USHER_ROLE} can start a movie vote.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        announce, _ = self.channels(interaction.guild)
        if announce is None:
            await interaction.followup.send("The Movie Night channels are missing. Restart the bot to recreate them.", ephemeral=True)
            return

        picks, seen = [], set()
        for value in (film1, film2, film3, film4):
            if value and (item := await self.resolve_film(value)) and item["Id"] not in seen:
                picks.append(item)
                seen.add(item["Id"])
        if len(picks) < 2:
            for item in await self.bot.jf.random_movies(10):
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

    @movievote.autocomplete("film1")
    @movievote.autocomplete("film2")
    @movievote.autocomplete("film3")
    @movievote.autocomplete("film4")
    async def movievote_film(self, interaction: discord.Interaction, current: str):
        return await self.film_choices(current)


async def setup(bot):
    await bot.add_cog(MovieNight(bot))
