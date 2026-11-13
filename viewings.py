"""The activity feed's viewing posts: one post per person per sitting (#13).

A viewing is someone watching, from pressing play until they stop for longer
than RESUME_GAP. It gets one post, edited in place as they move on to the next
episode and when they stop, the way the watch party post works (parties.py).

Over a quarter of plays last under ten minutes: someone browsing or sampling.
So a viewing is posted only once one play has lasted SHORT_PLAY, and short
plays are left out of the list.

Pure apart from the embeds, for the tests. The activity cog does the sending.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import discord

from parties import length_text, title_of

SHORT_PLAY = timedelta(minutes=5)     # plays shorter than this are not posted
RESUME_GAP = timedelta(minutes=10)    # stop and play again within this: the same post
MAX_LISTED = 10                       # earlier titles listed by name in one post


@dataclass
class Play:
    item: dict
    started: datetime
    ended: datetime | None = None
    together: bool = False            # in a watch party, which has its own post

    def length(self, now: datetime) -> timedelta:
        return (self.ended or now) - self.started


@dataclass
class Viewing:
    user: str
    device: str
    plays: list[Play] = field(default_factory=list)
    quiet: bool = False               # already playing when the bot started: not posted
    stopped: datetime | None = None
    posted: bool = False
    message: object = None            # the Discord message, set by the cog

    @property
    def current(self) -> Play:
        return self.plays[-1]

    def shown(self, now: datetime) -> list[Play]:
        """The plays the post lists: none from a watch party, and no short ones
        except the one playing now."""
        playing = self.current if self.stopped is None else None
        return [p for p in self.plays
                if not p.together and (p is playing or p.length(now) >= SHORT_PLAY)]

    def watched_for(self, now: datetime) -> timedelta:
        return sum((p.length(now) for p in self.shown(now)), timedelta())


class Feed:
    """Viewings by Jellyfin session id, updated once per session poll."""

    def __init__(self):
        self.viewings: dict[str, Viewing] = {}

    def update(self, sessions, now: datetime, in_party=frozenset(), announce=True) -> list[Viewing]:
        """Take one poll of Jellyfin's sessions. Returns the viewings whose post
        needs sending (no message yet) or editing.

        announce is False on the first poll after a restart: what is already
        playing is followed without a post, as before (1.0.0)."""
        changed = []
        playing = {s['Id']: s for s in sessions
                   if (s.get('NowPlayingItem') or {}).get('Id') and s.get('UserName')}

        for sid, session in playing.items():
            item = session['NowPlayingItem']
            viewing = self.viewings.get(sid) or self._resume(sid, session['UserName'], now)
            if viewing is None:
                viewing = Viewing(session['UserName'], session.get('DeviceName', 'Unknown'),
                                  [Play(item, now)], quiet=not announce)
                self.viewings[sid] = viewing
                moved = False
            else:
                moved = self._carry_on(viewing, item, session, now)

            if sid in in_party and not viewing.posted:
                viewing.current.together = True

            if not viewing.posted and not viewing.quiet and not viewing.current.together \
                    and viewing.current.length(now) >= SHORT_PLAY:
                viewing.posted = True
                changed.append(viewing)
            elif viewing.posted and moved:
                changed.append(viewing)

        for sid, viewing in list(self.viewings.items()):
            if sid in playing:
                continue
            if viewing.stopped is None:
                viewing.current.ended = viewing.stopped = now
                if viewing.posted:
                    changed.append(viewing)
                elif not viewing.quiet and viewing.shown(now):
                    # Passed SHORT_PLAY between two polls, then stopped
                    viewing.posted = True
                    changed.append(viewing)
            elif now - viewing.stopped > RESUME_GAP:
                del self.viewings[sid]
        return changed

    def _resume(self, sid: str, user: str, now: datetime) -> Viewing | None:
        """The same person's viewing that stopped less than RESUME_GAP ago, on any
        device, moved to this session. Picking the next episode by hand, or
        moving from the laptop to the TV, carries on the same post."""
        stopped = [(v.stopped, key) for key, v in self.viewings.items()
                   if v.stopped and v.user.lower() == user.lower() and now - v.stopped <= RESUME_GAP]
        if not stopped:
            return None
        key = max(stopped)[1]
        self.viewings[sid] = self.viewings.pop(key)
        return self.viewings[sid]

    @staticmethod
    def _carry_on(viewing: Viewing, item: dict, session: dict, now: datetime) -> bool:
        """Update a viewing with what its session plays now. True if the post changes."""
        resumed = viewing.stopped is not None
        if resumed:
            viewing.stopped = None
            viewing.device = session.get('DeviceName', viewing.device)
        if viewing.current.item.get('Id') == item.get('Id'):
            viewing.current.ended = None
            return resumed
        viewing.current.ended = viewing.current.ended or now
        if viewing.quiet:
            # Started before the bot did, so its start is unknown: the next title
            # is where its post begins.
            viewing.plays, viewing.quiet = [], False
        viewing.plays.append(Play(item, now))
        return True


# --- the post ------------------------------------------------------------------

LIVE_COLOUR = discord.Color.blue()
DONE_COLOUR = discord.Color.greyple()


def describe(item: dict) -> str:
    """**Show S1E3** · Episode name, or **Film (2019)**."""
    text = f"**{title_of(item)}**"
    if item.get('Type') == 'Episode' and item.get('Name'):
        text += f" · {item['Name']}"
    return text


def listing(plays: list[Play]) -> str:
    lines = [f"• {describe(p.item)}" for p in plays[-MAX_LISTED:]]
    if len(plays) > MAX_LISTED:
        lines.insert(0, f"…and {len(plays) - MAX_LISTED} earlier")
    return "\n".join(lines)


def embed(viewing: Viewing, now: datetime) -> discord.Embed:
    shown = viewing.shown(now)
    started = shown[0].started if shown else viewing.plays[0].started
    if viewing.stopped is None:
        post = discord.Embed(
            title="▶️ Now watching",
            description=f"**{viewing.user}** is watching {describe(viewing.current.item)}",
            colour=LIVE_COLOUR)
        earlier = [p for p in shown if p is not viewing.current]
        if earlier:
            post.add_field(name="Earlier", value=listing(earlier), inline=False)
        post.add_field(name="Started", value=f"<t:{int(started.timestamp())}:R>", inline=True)
    else:
        length = length_text(viewing.watched_for(now))
        if len(shown) == 1:
            description = f"**{viewing.user}** watched {describe(shown[0].item)}, for {length}"
        else:
            description = f"**{viewing.user}** watched {len(shown)} titles, for {length}"
        post = discord.Embed(title="⏹️ Watched", description=description, colour=DONE_COLOUR)
        if len(shown) > 1:
            post.add_field(name="Watched", value=listing(shown), inline=False)
        post.add_field(name="Started", value=f"<t:{int(started.timestamp())}:t>", inline=True)
    post.add_field(name="Device", value=viewing.device, inline=True)
    return post
