# EddyFlix Bot

A Discord bot for a small Jellyfin server and the friends who use it. It shows what is
playing, keeps watch stats and streaks, and runs movie nights: a scheduled Discord event, a
ping for the people who want one, and everyone watching in sync through Jellyfin's SyncPlay.

Feature work and bugs are tracked in the
[Eddyflix-Bot project](https://github.com/users/EdwinAndrew/projects/3) and this repo's issues.

## What it does

| | |
|---|---|
| **Dashboard** | One message in a channel, edited every minute: server status and active streams |
| **Activity feed** | Connections, playback starts, switches and stops, and a warning at three transcodes |
| **Weekly digest** | Sundays at 6 PM: watch time, top shows, who watched most |
| **Achievements and streaks** | Hours-watched milestones, series completions, daily watch streaks |
| **Movie night** | Themed roles with a picker in `#roles`, `/movienight` and `/movievote`. See below |
| **Health** | A heartbeat file for Docker's health check, written only while Discord *and* Jellyfin answer, and an optional Uptime Kuma push |

### Commands

`/jf-health` · `/jf-stats` · `/jf-status` · `/jf-recent` · `/jf-stats-graph` · `/jf-popular` ·
`/jf-subscribe` · `/jf-unsubscribe` · `/jf-subscriptions` · `/jf-weekly-digest` ·
`/jf-achievements` · `/jf-achievements-all` · `/jf-streaks` · `/jf-streaks-leaderboard` ·
`/jf-debug-sessions` · `/jf-help` · `/movienight` · `/movievote`

### Movie night

On start the bot creates whatever is missing, and never deletes anything:

- **Roles.** 🎬 Director (admins) and 🔦 Usher (mods), hoisted, with moderation permissions.
  🎟️ Ticket Holder, opt-in, pinged for movie nights. Thirteen cosmetic flairs, "the friend
  everyone has on movie night" (*Talks During the Movie*, *Late Arrival*, *Spoiler Alert*…)
- **`#roles`** gets a picker message: a menu for flairs and a button for the ticket. It survives
  restarts and is edited in place when the list changes
- **A 🎬 Movie Night category**: `#movie-night` (announcements, staff only), `#watch-chat`, and
  a 🎥 Theatre voice channel
- **`/movienight film day time`**: the film from the library, a day (`tonight`, `friday`,
  `2026-10-31`) and a time (`8pm`, `20:00`). Creates a Discord event with the film's backdrop
  and posts an announcement with the poster and SyncPlay steps. **SyncPlay does not open the film
  for the others**: everyone opens the same film, joins the group, then playback stays in sync
- **`/movievote`**: a Discord poll of up to four films, random ones if none are given

Names in [movie_night.py](movie_night.py) are the source of truth. Rename a role there, not in
Discord, or the bot recreates the old name.

## Running it

It is built for Docker Compose next to Jellyfin. Images are published to
`ghcr.io/edwinandrew/eddyflix-bot`, tagged by version.

```yaml
services:
  eddyflix-bot:
    image: ghcr.io/edwinandrew/eddyflix-bot:1.0.0
    environment:
      - TZ=America/Toronto
      - JELLYFIN_URL=http://jellyfin:8096
      - JELLYFIN_API_KEY=${JELLYFIN_API_KEY:?}
      - DISCORD_TOKEN=${DISCORD_BOT_TOKEN:?}
      - DASHBOARD_CHANNEL_ID=${DASHBOARD_CHANNEL_ID:-0}
      - ACTIVITY_CHANNEL_ID=${ACTIVITY_CHANNEL_ID:-0}
    volumes:
      - ./data:/app/data        # SQLite: watch history, streaks, subscriptions. Owned by uid 1000
    restart: unless-stopped
```

All settings are in [.env.example](.env.example).

- **Jellyfin:** an API key from *Dashboard → API Keys*. Jellyfin 12 accepts it only as
  `Authorization: MediaBrowser Token="…"`, which is what the bot sends
- **Discord:** a bot with the *Message Content* intent. Invite it with
  `scope=bot+applications.commands` and `permissions=18700556168272` (post, embed, attach, react,
  manage channels, roles, messages and events, create events, timeout members). Its role must sit
  above the roles it hands out

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # fill in, then:
set -a; . ./.env; set +a; python bot.py
```

**Releasing:** merge to `main`, then tag `vX.Y.Z` and push the tag. The workflow builds the image,
checks that the bot imports, and publishes `X.Y.Z` (and `sha-…` for every `main` commit). The
server pins a version, so nothing changes there until its compose file moves to the new tag.

## History

Written for a media server that ran on a gaming PC, then moved with it to a Proxmox homelab in
2026-10. It lived in that homelab's repo until it got its own here. Earlier history is not
carried over.
