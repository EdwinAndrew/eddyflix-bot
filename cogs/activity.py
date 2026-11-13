"""The activity feed, polled every 30 seconds: one post per viewing (viewings.py),
one per watch party (parties.py), and a warning when too much is transcoding.

Also where watch history gets recorded, since that follows the same sessions,
and where the end of a play is noticed for the catch-up subscription offer
(catch_up.py).
"""
import io
from datetime import datetime

import aiosqlite
import discord
from discord.ext import commands, tasks

import admin_summary
import config
import parties
import viewings
import watch_data

TRANSCODE_WARNING = 3     # this many transcodes at once gets a warning


class Activity(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.active_sessions = {}
        self.sessions_seeded = False   # set after the first session poll; see check_sessions
        self.parties = {}              # item id -> the watch party on it; see update_parties
        self.feed = viewings.Feed()     # one post per viewing; see viewings.py
        self.reasons_recorded = {}  # (session, item) -> the (reasons, kind) last saved for it
        self.transcode_warned = False
        self.now = datetime.now         # replaced in the tests

    async def poster_for(self, item: dict) -> bytes | None:
        """A small poster for a playback post: the show's for an episode, the film's
        otherwise. Uploaded rather than linked, because Jellyfin's public address
        answers Canadian addresses only and Discord fetches linked images from
        elsewhere. None if there is no image or Jellyfin does not answer, and the
        post goes out without one."""
        image_id = item.get('SeriesId') if item.get('Type') == 'Episode' and item.get('SeriesId') else item.get('Id')
        if not image_id or self.bot.jf is None:
            return None
        try:
            return await self.bot.jf.get_image(image_id, "Primary", max_width=300)
        except Exception as e:
            print(f"No poster for {item.get('Name')}: {e}")
            return None

    async def update_parties(self, parties_now: dict, channel, announce: bool):
        """One post per watch party: sent when it forms, edited as people join or
        leave, and turned into a "watched together" line when it ends. After a
        restart, parties already running are tracked without a post."""
        now = self.now()
        for item_id, party in parties_now.items():
            known = self.parties.get(item_id)
            if known is None:
                message = None
                if announce:
                    embed = parties.live_embed(party["item"], party["users"], now)
                    files = []
                    poster = await self.poster_for(party["item"])
                    if poster:
                        files.append(discord.File(io.BytesIO(poster), filename="poster.jpg"))
                        embed.set_thumbnail(url="attachment://poster.jpg")
                    message = await channel.send(embed=embed, files=files)
                self.parties[item_id] = {"item": party["item"], "users": party["users"],
                                         "everyone": set(party["users"]), "started": now,
                                         "message": message}
            elif party["users"] != known["users"]:
                known["users"] = party["users"]
                known["everyone"] |= set(party["users"])
                await self._edit(known, parties.live_embed(known["item"], known["users"], known["started"]))

        for item_id in [i for i in self.parties if i not in parties_now]:
            known = self.parties.pop(item_id)
            everyone = sorted(known["everyone"], key=str.lower)
            await self._edit(known, parties.ended_embed(known["item"], everyone, known["started"], now))

    async def _edit(self, known: dict, embed: discord.Embed):
        if known["message"] is None:
            return
        if known["message"].attachments:
            embed.set_thumbnail(url="attachment://poster.jpg")
        try:
            await known["message"].edit(embed=embed)
        except discord.HTTPException as e:
            print(f"Could not update a feed post: {e}")

    @commands.Cog.listener()
    async def on_services_ready(self):
        if self.bot.jf and config.ACTIVITY_CHANNEL_ID and not self.check_sessions.is_running():
            self.check_sessions.start()

    @tasks.loop(seconds=30)
    async def check_sessions(self):
        """Poll Jellyfin's sessions: record plays, update the feed, warn about transcodes."""
        try:
            sessions = await self.bot.jf.get_sessions()
            current_session_ids = {s['Id'] for s in sessions}

            activity_channel = self.bot.get_channel(config.ACTIVITY_CHANNEL_ID)
            if not activity_channel:
                return

            # Open sessions live in memory only, so after a restart everything
            # Jellyfin still lists would look new. The first poll only takes note
            # of what is playing; watch history is still recorded below.
            announce = self.sessions_seeded

            # People watching the same thing together get one post between them
            parties_now = parties.find_parties(sessions)
            in_party = {sid for party in parties_now.values() for sid in party['session_ids']}

            for session in sessions:
                await self.record(session)

            if not self.sessions_seeded:
                # Plays left open while the bot was down: close them once, now
                # that this poll has picked up the ones still playing
                live = {(s['Id'], s['NowPlayingItem']['Id']) for s in sessions if s.get('NowPlayingItem')}
                closed, deleted = await watch_data.close_orphans(self.bot.jf, live, self.now())
                if closed or deleted:
                    print(f"Closed {closed} plays left open, deleted {deleted} duplicates")

            for sess_id in set(self.active_sessions) - current_session_ids:
                # Record the end of anything still playing when the session went away
                prev_session = self.active_sessions.pop(sess_id)
                prev_item = prev_session.get('NowPlayingItem')
                if prev_item:
                    await watch_data.record_watch_end(self.bot.jf, sess_id, prev_item.get('Id'))
                    await self.play_ended(prev_session)
            self.reasons_recorded = {k: v for k, v in self.reasons_recorded.items() if k[0] in current_session_ids}

            # Connections are not posted: Jellyfin opens a session whenever
            # someone opens an app, and keeps idle ones for hours (#13).
            now = self.now()
            for viewing in self.feed.update(sessions, now, in_party, announce):
                await self.post_viewing(viewing, activity_channel, now)

            await self.update_parties(parties_now, activity_channel, announce)
            self.sessions_seeded = True

            await self.check_transcodes(sessions, activity_channel)

        except Exception as e:
            print(f"Error checking sessions: {e}")

    async def record(self, session: dict):
        """Watch history for one session: a start when something new plays, an end
        when it changes or stops, and why it is transcoded once Jellyfin says."""
        sess_id = session['Id']
        now_playing = session.get('NowPlayingItem')
        prev_playing = self.active_sessions.get(sess_id, {}).get('NowPlayingItem')
        now_item_id = now_playing.get('Id') if now_playing else None
        prev_item_id = prev_playing.get('Id') if prev_playing else None

        if now_playing and sess_id not in self.active_sessions:
            # The bot may have restarted mid-play: carry on an unclosed row if there is one
            async with aiosqlite.connect(config.DB_PATH) as db:
                async with db.execute("""
                    SELECT id FROM watch_history
                    WHERE session_id = ? AND item_id = ? AND end_time IS NULL
                    ORDER BY start_time DESC LIMIT 1
                """, (sess_id, now_item_id)) as cursor:
                    existing = await cursor.fetchone()
            if not existing:
                await watch_data.record_watch_start(sess_id, session)
        elif now_playing and now_item_id != prev_item_id:
            if prev_item_id:
                await watch_data.record_watch_end(self.bot.jf, sess_id, prev_item_id)
                await self.play_ended(self.active_sessions[sess_id])
            await watch_data.record_watch_start(sess_id, session)
        elif prev_playing and not now_playing:
            await watch_data.record_watch_end(self.bot.jf, sess_id, prev_item_id)
            await self.play_ended(self.active_sessions[sess_id])

        reasons = admin_summary.transcode_reasons(session)
        kind = admin_summary.transcode_kind(session)
        if not now_playing or now_item_id != prev_item_id:
            self.reasons_recorded = {k: v for k, v in self.reasons_recorded.items()
                                     if k[0] != sess_id or k[1] == now_item_id}
        # Saved again when it changes: the kind can show up a poll after the
        # reasons, and a player can fall back from audio-only to full video
        seen = (tuple(reasons), kind)
        if now_playing and (reasons or kind) and self.reasons_recorded.get((sess_id, now_item_id)) != seen:
            play_method = (session.get('PlayState') or {}).get('PlayMethod')
            await watch_data.record_transcode(sess_id, now_item_id, play_method, reasons, kind)
            self.reasons_recorded[(sess_id, now_item_id)] = seen

        self.active_sessions[sess_id] = session

    async def play_ended(self, last_seen: dict):
        """A play ended; last_seen is its session at the last poll, with how far
        in it was. Someone who just caught up on a show may get offered
        new-episode DMs (cogs/subscriptions.py). Never stops the poll."""
        get_cog = getattr(self.bot, "get_cog", None)
        subscriptions = get_cog("Subscriptions") if get_cog else None
        if subscriptions is None:
            return
        try:
            await subscriptions.offer_if_caught_up(last_seen)
        except Exception as e:
            print(f"Could not check for a catch-up offer: {e}")

    async def post_viewing(self, viewing, channel, now: datetime):
        """Send a viewing's post the first time, edit it after that."""
        embed = viewings.embed(viewing, now)
        if viewing.message is not None:
            await self._edit({"message": viewing.message}, embed)
            return
        files = []
        shown = viewing.shown(now)
        poster = await self.poster_for((shown[0] if shown else viewing.current).item)
        if poster:
            files.append(discord.File(io.BytesIO(poster), filename="poster.jpg"))
            embed.set_thumbnail(url="attachment://poster.jpg")
        try:
            viewing.message = await channel.send(embed=embed, files=files)
        except discord.HTTPException as e:
            print(f"Could not post a viewing: {e}")

    async def check_transcodes(self, sessions, activity_channel):
        """Warn once when TRANSCODE_WARNING streams are re-encoding video at the
        same time, and again only after it has dropped below. Audio-only and
        repackaged streams copy the picture and cost little, so they don't count.
        In the admin channel when there is one (#51)."""
        transcoding = [s for s in sessions if admin_summary.heavy(s)]
        if len(transcoding) < TRANSCODE_WARNING:
            self.transcode_warned = False
            return
        if self.transcode_warned:
            return
        self.transcode_warned = True
        channel = (self.bot.get_channel(config.ADMIN_CHANNEL_ID) if config.ADMIN_CHANNEL_ID else None) \
            or activity_channel
        embed = discord.Embed(
            title="⚠️ High Transcode Load",
            description=f"**{len(transcoding)} streams** are re-encoding video at once",
            color=discord.Color.orange(),
            timestamp=datetime.now()
        )
        embed.add_field(name="Who, and why", value=admin_summary.clip(
            "\n".join(admin_summary.transcode_alert_lines(transcoding))), inline=False)
        await channel.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Activity(bot))
