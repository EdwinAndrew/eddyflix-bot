"""/jf-subscribe, /jf-unsubscribe, /jf-subscriptions, and the DMs they lead to.

Every five minutes the bot looks for episodes added to Jellyfin since its last
look (see new_episodes.py) and DMs each subscriber once per show: "6 new
episodes of Fallout: S02E01–E06". Anyone whose DMs are closed is mentioned in
the activity channel instead.

It also offers the subscription with a 🔔 button to someone who has just caught
up on a show, once per member per show (catch_up.py). The activity cog calls
offer_if_caught_up when a play ends.
"""
import io
from datetime import datetime, timezone

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

import catch_up
import config
import new_episodes
from cogs.movie_night import JELLYFIN_ID, details_link, film_label

COLOUR = discord.Colour(0x00A4DC)      # Jellyfin blue
PAGE = 100                             # episodes per request while polling
SEED_PAGE = 500                        # ...and while noting the whole library once
CATCH_UP_PAGE = 10                     # a series' newest episodes asked for at a time


def best_series(typed: str, found: list[dict]) -> dict | None:
    """The show with this name, ignoring case and punctuation, or Jellyfin's only hit.
    None when it is not in the library or the name is ambiguous."""
    exact = [i for i in found if new_episodes.normalize(i.get("Name")) == new_episodes.normalize(typed)]
    if exact:
        return exact[0]
    return found[0] if len(found) == 1 else None


def notification_embed(episodes: list[dict]) -> discord.Embed:
    """One show's new episodes: a headline, the range, and the first few by name."""
    first = episodes[0]
    series_name = first.get("SeriesName") or "Unknown show"
    lines = [f"**{new_episodes.episode_range(episodes)}** just landed on Jellyfin.", ""]
    for ep in episodes[:new_episodes.MAX_LISTED]:
        label = f"{new_episodes.episode_code(ep)} · {ep.get('Name') or 'Untitled'}"
        link = details_link(ep["Id"]) if ep.get("Id") else None
        lines.append(f"[{label}]({link})" if link else label)
    if len(episodes) > new_episodes.MAX_LISTED:
        lines.append(f"…and {len(episodes) - new_episodes.MAX_LISTED} more")
    embed = discord.Embed(
        title=f"📺 {new_episodes.headline(series_name, len(episodes))}"[:256],
        url=details_link(first["SeriesId"]) if first.get("SeriesId") else None,
        description="\n".join(lines)[:4096],
        colour=COLOUR,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="You subscribed with /jf-subscribe · /jf-unsubscribe to stop")
    return embed


class Subscriptions(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def cog_unload(self):
        self.check_new_episodes.cancel()

    @commands.Cog.listener()
    async def on_services_ready(self):
        if self.bot.jf and not self.check_new_episodes.is_running():
            self.check_new_episodes.start()

    # --- the poll ------------------------------------------------------------

    @tasks.loop(minutes=5)
    async def check_new_episodes(self):
        try:
            await self.poll()
        except Exception as e:
            print(f"Error checking for new episodes: {e}")

    async def episodes_since(self, cutoff: datetime | None, page: int | None = None) -> list[dict]:
        """Episodes added after cutoff (all of them when None), newest first."""
        page = page or PAGE
        found, start = [], 0
        while True:
            batch = await self.bot.jf.episodes_by_date_added(start, page)
            found += batch
            if len(batch) < page:
                break
            oldest = new_episodes.parse_date(batch[-1].get("DateCreated"))
            if cutoff and oldest and oldest <= cutoff:
                break
            start += page
        if cutoff is None:
            return found
        return [e for e in found if (d := new_episodes.parse_date(e.get("DateCreated"))) and d > cutoff]

    async def poll(self, now: datetime | None = None):
        now = now or datetime.now(timezone.utc)
        async with aiosqlite.connect(config.DB_PATH) as db:
            mark = await new_episodes.load_mark(db)
            if mark is None:
                # First run: what is already there is not news.
                episodes = await self.episodes_since(None, SEED_PAGE)
                await new_episodes.add_known(db, episodes)
                await new_episodes.save_mark(db, new_episodes.newest_date(episodes, None) or now)
                await db.commit()
                print(f"Subscriptions: {len(episodes)} episodes already in the library; watching for new ones")
                return

            episodes = await self.episodes_since(mark - new_episodes.LOOKBACK)
            ready = new_episodes.pick_new(episodes, await new_episodes.known_keys(db), now)
            if ready:
                async with db.execute("SELECT id, discord_user_id, show_name, show_id FROM subscriptions") as cursor:
                    rows = await cursor.fetchall()
                for group in ready:
                    await self.notify(db, group, rows)
                    # Marked per show, so a crash part-way repeats at most one show.
                    await new_episodes.add_known(db, group)
                    await db.commit()

            new_mark = new_episodes.newest_date(episodes, mark)
            if new_mark and new_mark != mark:
                await new_episodes.save_mark(db, new_mark)
                await db.commit()

    async def notify(self, db, episodes: list[dict], rows):
        first = episodes[0]
        series_id, series_name = first.get("SeriesId"), first.get("SeriesName")
        subs = new_episodes.subscribers(rows, series_id, series_name)
        if not subs:
            return

        embed = notification_embed(episodes)
        poster = None
        if series_id:
            try:
                poster = await self.bot.jf.get_image(series_id, "Primary", max_width=300)
            except Exception as e:
                print(f"No poster for {series_name}: {e}")
        if poster:
            embed.set_thumbnail(url="attachment://poster.jpg")

        def files():
            # A discord.File is used up by one send, so each message gets its own.
            return [discord.File(io.BytesIO(poster), filename="poster.jpg")] if poster else []

        closed = []
        for user_id in dict.fromkeys(r[1] for r in subs):   # once per person, in order
            try:
                user = self.bot.get_user(int(user_id)) or await self.bot.fetch_user(int(user_id))
                await user.send(embed=embed, files=files())
            except (discord.HTTPException, ValueError) as e:
                print(f"Could not DM {user_id} about {series_name}: {e}")
                closed.append(user_id)

        channel = self.bot.get_channel(config.ACTIVITY_CHANNEL_ID) if config.ACTIVITY_CHANNEL_ID else None
        if closed and channel:
            try:
                await channel.send(
                    content=" ".join(f"<@{u}>" for u in closed) + " new episodes are in (your DMs are closed)",
                    embed=embed, files=files(),
                    allowed_mentions=discord.AllowedMentions(users=[discord.Object(int(u)) for u in closed],
                                                             roles=False, everyone=False))
            except (discord.HTTPException, ValueError) as e:
                print(f"Could not post the new-episode fallback for {series_name}: {e}")

        # Rows from before the autocomplete matched by name; give them the id.
        for row in subs:
            if series_id and row[3] != series_id:
                await db.execute("UPDATE subscriptions SET show_id = ? WHERE id = ?", (series_id, row[0]))

    # --- the catch-up offer ---------------------------------------------------

    async def latest_episode(self, series_id: str) -> dict | None:
        """The newest episode the library has of a series. One short page when
        Jellyfin sorts as asked, the whole series when it does not."""
        page = await self.bot.jf.newest_episodes(series_id, limit=CATCH_UP_PAGE)
        if len(page) < CATCH_UP_PAGE or (catch_up.newest_first(page) and catch_up.latest(page)):
            return catch_up.latest(page)
        return catch_up.latest(await self.bot.jf.newest_episodes(series_id, limit=None))

    async def offer_if_caught_up(self, session: dict):
        """Called with a session as last seen playing, when that play ends. Offers
        the 🔔 subscription to each member linked to its Jellyfin user who has
        just finished the newest episode of a show still running, unless they
        are subscribed or were offered it before."""
        item = session.get("NowPlayingItem") or {}
        series_id, user_name = item.get("SeriesId"), session.get("UserName")
        if item.get("Type") != "Episode" or not series_id or not user_name or self.bot.jf is None:
            return
        series_name = item.get("SeriesName")
        # The database first: most plays end here, without asking Jellyfin anything.
        async with aiosqlite.connect(config.DB_PATH) as db:
            members = [m for m in await catch_up.linked_members(db, user_name)
                       if not await catch_up.offered(db, m, series_id, series_name)
                       and not await new_episodes.subscribed(db, m, series_id, series_name)]
        if not members:
            return

        jf = self.bot.jf
        if not catch_up.caught_up(item, await self.latest_episode(series_id)):
            return
        if not catch_up.reached_end(session):
            # Stopped in the credits between two polls: Jellyfin knows better.
            user_id = session.get("UserId") or await jf.get_user_id(user_name)
            played = await jf.get_item(item["Id"], user_id) if user_id and item.get("Id") else {}
            if not (played.get("UserData") or {}).get("Played"):
                return
        series = await jf.get_item(series_id)
        if series.get("Status") == "Ended":
            return
        name = series.get("Name") or series_name or "this show"

        for member in members:
            async with aiosqlite.connect(config.DB_PATH) as db:
                fresh = await catch_up.mark_offered(db, member, series_id, name)
                await db.commit()
            if fresh:
                await self.send_offer(member, series_id, name)

    async def send_offer(self, member_id: str, series_id: str, name: str):
        """The offer as a DM, or a mention in the activity channel when DMs are closed."""
        embed = catch_up.offer_embed(name)
        poster = None
        try:
            poster = await self.bot.jf.get_image(series_id, "Primary", max_width=300)
        except Exception as e:
            print(f"No poster for {name}: {e}")
        if poster:
            embed.set_thumbnail(url="attachment://poster.jpg")

        def extra():
            # A discord.File is used up by one send, so each message gets its own.
            return {"files": [discord.File(io.BytesIO(poster), filename="poster.jpg")] if poster else [],
                    "view": catch_up.offer_view(series_id, member_id)}

        try:
            user = self.bot.get_user(int(member_id)) or await self.bot.fetch_user(int(member_id))
            await user.send(embed=embed, **extra())
            print(f"Offered {member_id} new-episode DMs for {name}")
            return
        except (discord.HTTPException, ValueError) as e:
            print(f"Could not DM {member_id} the offer for {name}: {e}")

        channel = self.bot.get_channel(config.ACTIVITY_CHANNEL_ID) if config.ACTIVITY_CHANNEL_ID else None
        if channel:
            try:
                await channel.send(content=catch_up.fallback_text(member_id, name), embed=embed, **extra(),
                                   allowed_mentions=discord.AllowedMentions(users=[discord.Object(int(member_id))],
                                                                            roles=False, everyone=False))
            except (discord.HTTPException, ValueError) as e:
                print(f"Could not post the offer fallback for {name}: {e}")

    # --- the commands --------------------------------------------------------

    async def resolve_series(self, value: str) -> dict | None:
        """The autocomplete gives a series id; typed text without picking is searched instead."""
        if JELLYFIN_ID.match(value):
            item = await self.bot.jf.get_item(value)
            if item.get("Type") == "Series":
                return item
        return best_series(value, await self.bot.jf.search_series(value, limit=10))

    async def user_rows(self, db, user_id: str):
        async with db.execute("""
            SELECT id, discord_user_id, show_name, show_id, subscribed_at
            FROM subscriptions WHERE discord_user_id = ? ORDER BY subscribed_at DESC
        """, (user_id,)) as cursor:
            return await cursor.fetchall()

    @app_commands.command(name="jf-subscribe", description="Get a DM when new episodes of a show are added")
    @app_commands.describe(show="A show from the Jellyfin library")
    async def subscribe_command(self, interaction: discord.Interaction, show: str):
        await interaction.response.defer(ephemeral=True)
        if self.bot.jf is None:
            await interaction.followup.send("Jellyfin isn't set up.", ephemeral=True)
            return
        try:
            series = await self.resolve_series(show)
        except Exception as e:
            await interaction.followup.send(f"❌ Couldn't reach Jellyfin: {e}", ephemeral=True)
            return
        if not series:
            await interaction.followup.send(f"There's no show called **{show}** in Jellyfin. Pick one from the list.",
                                            ephemeral=True)
            return

        name, series_id = series.get("Name"), series.get("Id")
        try:
            async with aiosqlite.connect(config.DB_PATH) as db:
                already = not await new_episodes.subscribe(db, str(interaction.user.id), interaction.user.name,
                                                           name, series_id)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)
            return

        if already:
            embed = discord.Embed(title="ℹ️ Already Subscribed",
                                  description=f"You're already subscribed to **{name}**.",
                                  color=discord.Color.blue())
        else:
            embed = discord.Embed(
                title="✅ Subscribed",
                description=(f"I'll DM you when new episodes of **{name}** are added. "
                             "If your DMs are closed, I'll mention you in the activity channel instead."),
                color=discord.Color.green(), timestamp=datetime.now())
        await interaction.followup.send(embed=embed, ephemeral=True)

    @subscribe_command.autocomplete("show")
    async def subscribe_show_choices(self, interaction: discord.Interaction, current: str):
        if self.bot.jf is None:
            return []
        try:
            items = await self.bot.jf.search_series(current, limit=25)
        except Exception:
            return []
        return [app_commands.Choice(name=film_label(i)[:100], value=i["Id"]) for i in items]

    @app_commands.command(name="jf-unsubscribe", description="Stop new-episode DMs for a show")
    @app_commands.describe(show="One of your subscriptions")
    async def unsubscribe_command(self, interaction: discord.Interaction, show: str):
        await interaction.response.defer(ephemeral=True)
        user_id = str(interaction.user.id)
        try:
            async with aiosqlite.connect(config.DB_PATH) as db:
                rows = await self.user_rows(db, user_id)
                # The autocomplete gives the stored name; typed text matches loosely.
                matched = [r for r in rows if r[2] == show] or new_episodes.subscribers(rows, None, show)
                for row in matched:
                    await db.execute("DELETE FROM subscriptions WHERE id = ?", (row[0],))
                await db.commit()
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)
            return

        if matched:
            embed = discord.Embed(title="✅ Unsubscribed",
                                  description=f"No more DMs about **{matched[0][2]}**.",
                                  color=discord.Color.green(), timestamp=datetime.now())
        else:
            embed = discord.Embed(title="ℹ️ Not Subscribed",
                                  description=f"You weren't subscribed to **{show}**.",
                                  color=discord.Color.blue())
        await interaction.followup.send(embed=embed, ephemeral=True)

    @unsubscribe_command.autocomplete("show")
    async def unsubscribe_show_choices(self, interaction: discord.Interaction, current: str):
        try:
            async with aiosqlite.connect(config.DB_PATH) as db:
                rows = await self.user_rows(db, str(interaction.user.id))
        except Exception:
            return []
        return [app_commands.Choice(name=r[2][:100], value=r[2][:100])
                for r in rows if current.lower() in r[2].lower()][:25]

    @app_commands.command(name="jf-subscriptions", description="List the shows you get new-episode DMs for")
    async def subscriptions_command(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            async with aiosqlite.connect(config.DB_PATH) as db:
                rows = await self.user_rows(db, str(interaction.user.id))
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)
            return

        if not rows:
            await interaction.followup.send("You don't have any show subscriptions yet.\n\n"
                                            "Use `/jf-subscribe` to get a DM when a show gets new episodes.",
                                            ephemeral=True)
            return

        embed = discord.Embed(
            title="📺 Your Show Subscriptions",
            description=f"You get a DM when these {len(rows)} show(s) get new episodes",
            color=discord.Color.purple(),
            timestamp=datetime.now()
        )
        for _, _, show_name, _, subscribed_at in rows[:25]:
            try:
                date_str = datetime.fromisoformat(subscribed_at).strftime('%Y-%m-%d')
            except (ValueError, TypeError):
                date_str = "Unknown"
            embed.add_field(name=f"📺 {show_name}", value=f"Subscribed: {date_str}", inline=True)
        embed.set_footer(text="Use /jf-unsubscribe to remove a subscription")
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Subscriptions(bot))
