"""The activity feed: connections, playback and transcode load, polled every 30 seconds.

Also where watch history gets recorded, since that follows the same sessions.
"""
import io
from datetime import datetime

import aiosqlite
import discord
from discord.ext import commands, tasks

import config
import parties
import watch_data


class Activity(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.active_sessions = {}
        self.sessions_seeded = False   # set after the first session poll; see check_sessions
        self.parties = {}              # item id -> the watch party on it; see update_parties
        self.party_sessions = set()    # sessions that were in a party at the last poll

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
        now = datetime.now()
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
            print(f"Could not update the watch party post: {e}")

    @commands.Cog.listener()
    async def on_services_ready(self):
        if self.bot.jf and config.ACTIVITY_CHANNEL_ID and not self.check_sessions.is_running():
            self.check_sessions.start()

    @tasks.loop(seconds=30)
    async def check_sessions(self):
        """Check for session changes every 30 seconds"""
        try:
            sessions = await self.bot.jf.get_sessions()
            current_session_ids = {s['Id'] for s in sessions}

            activity_channel = self.bot.get_channel(config.ACTIVITY_CHANNEL_ID)
            if not activity_channel:
                return

            # Open sessions live in memory only, so after a restart every session
            # Jellyfin still lists (idle ones linger for hours) looked new and was
            # announced as "connected" or "started watching". The first poll only
            # takes note of them; watch history is still recorded below.
            announce = self.sessions_seeded

            # People watching the same thing together get one post between them,
            # so their own start and stop posts are skipped.
            parties_now = parties.find_parties(sessions)
            in_party = {sid for party in parties_now.values() for sid in party['session_ids']}

            for session in sessions:
                sess_id = session['Id']
                username = session.get('UserName', 'Unknown')
                device = session.get('DeviceName', 'Unknown')

                if sess_id not in self.active_sessions and announce:
                    embed = discord.Embed(
                        title="👤 User Connected",
                        description=f"**{username}** connected from **{device}**",
                        color=discord.Color.green(),
                        timestamp=datetime.now()
                    )
                    await activity_channel.send(embed=embed)

                now_playing = session.get('NowPlayingItem')
                prev_playing = self.active_sessions.get(sess_id, {}).get('NowPlayingItem')

                # Detect if content changed (either started fresh or switched to different show/movie)
                now_item_id = now_playing.get('Id') if now_playing else None
                prev_item_id = prev_playing.get('Id') if prev_playing else None

                # If bot just started and we find an active playback, check database for existing unclosed session
                if now_playing and sess_id not in self.active_sessions:
                    # Bot may have restarted - check if there's an unclosed session in DB
                    async with aiosqlite.connect(config.DB_PATH) as db:
                        async with db.execute("""
                            SELECT id FROM watch_history
                            WHERE session_id = ? AND item_id = ? AND end_time IS NULL
                            ORDER BY start_time DESC LIMIT 1
                        """, (sess_id, now_item_id)) as cursor:
                            existing = await cursor.fetchone()

                    # If no existing session, record the start
                    if not existing:
                        await watch_data.record_watch_start(sess_id, session)

                # Record watch history when content changes
                elif now_playing and now_item_id != prev_item_id:
                    # If switching from one item to another, record end of previous
                    if prev_item_id:
                        await watch_data.record_watch_end(self.bot.jf, sess_id, prev_item_id)
                    # Record start of new item
                    await watch_data.record_watch_start(sess_id, session)

                # Only send notification if the item ID actually changed (switched content)
                if announce and now_playing and now_item_id != prev_item_id and sess_id not in in_party:
                    item_name = now_playing.get('Name', 'Unknown')
                    item_type = now_playing.get('Type', 'Media')

                    # Determine if this is a fresh start or a switch
                    if prev_playing:
                        title_text = "🔄 Switched Content"
                        desc_text = f"**{username}** switched to"
                    else:
                        title_text = "▶️ Playback Started"
                        desc_text = f"**{username}** started watching"

                    embed = discord.Embed(
                        title=title_text,
                        description=desc_text,
                        color=discord.Color.blue(),
                        timestamp=datetime.now()
                    )

                    if item_type == 'Episode':
                        series = now_playing.get('SeriesName', 'Unknown')
                        season = now_playing.get('ParentIndexNumber', '?')
                        episode = now_playing.get('IndexNumber', '?')
                        embed.add_field(name="Show", value=series, inline=True)
                        embed.add_field(name="Episode", value=f"S{season}E{episode}", inline=True)
                        embed.add_field(name="Title", value=item_name, inline=False)
                    else:
                        embed.add_field(name="Title", value=item_name, inline=True)
                        embed.add_field(name="Type", value=item_type, inline=True)

                    embed.add_field(name="Device", value=device, inline=True)

                    files = []
                    poster = await self.poster_for(now_playing)
                    if poster:
                        files.append(discord.File(io.BytesIO(poster), filename="poster.jpg"))
                        embed.set_thumbnail(url="attachment://poster.jpg")

                    await activity_channel.send(embed=embed, files=files)

                elif prev_playing and not now_playing:
                    prev_name = prev_playing.get('Name', 'Unknown')

                    # Record end of watch session
                    if prev_item_id:
                        await watch_data.record_watch_end(self.bot.jf, sess_id, prev_item_id)

                    if sess_id not in self.party_sessions:
                        embed = discord.Embed(
                            title="⏹️ Playback Stopped",
                            description=f"**{username}** stopped watching **{prev_name}**",
                            color=discord.Color.greyple(),
                            timestamp=datetime.now()
                        )
                        await activity_channel.send(embed=embed)

                self.active_sessions[sess_id] = session

            offline_sessions = set(self.active_sessions.keys()) - current_session_ids
            for sess_id in offline_sessions:
                username = self.active_sessions[sess_id].get('UserName', 'Unknown')

                # Record end of any active watch session when user disconnects
                prev_session = self.active_sessions[sess_id]
                prev_item = prev_session.get('NowPlayingItem')
                if prev_item:
                    await watch_data.record_watch_end(self.bot.jf, sess_id, prev_item.get('Id'))

                embed = discord.Embed(
                    title="👋 User Disconnected",
                    description=f"**{username}** disconnected",
                    color=discord.Color.red(),
                    timestamp=datetime.now()
                )
                await activity_channel.send(embed=embed)
                del self.active_sessions[sess_id]

            await self.update_parties(parties_now, activity_channel, announce)
            self.party_sessions = in_party
            self.sessions_seeded = True

            # Monitor transcodes - warn if too many
            transcoding_sessions = [s for s in sessions if s.get('PlayState', {}).get('PlayMethod') == 'Transcode']
            if len(transcoding_sessions) >= 3:
                transcode_users = [s.get('UserName', 'Unknown') for s in transcoding_sessions]
                embed = discord.Embed(
                    title="⚠️ High Transcode Load",
                    description=f"**{len(transcoding_sessions)} active transcodes** detected",
                    color=discord.Color.orange(),
                    timestamp=datetime.now()
                )
                embed.add_field(name="Users Transcoding", value="\n".join([f"• {u}" for u in transcode_users]), inline=False)
                embed.add_field(name="💡 Tip", value="Consider using compatible formats or adjusting quality settings to reduce server load.", inline=False)
                await activity_channel.send(embed=embed)

        except Exception as e:
            print(f"Error checking sessions: {e}")


async def setup(bot):
    await bot.add_cog(Activity(bot))
