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
| **Activity feed** | Connections, playback starts, switches and stops with posters, and a warning at three transcodes. Two or more people on the same title at about the same point get one **🍿 Watching together** post instead, edited as people join and turned into **🎬 Watched together** at the end |
| **Weekly digest** | Sundays at 6 PM: watch time, top shows, who watched most |
| **Achievements and streaks** | Watch-time tiers with badges, popcorn from the cob up: Corn at 10 h, Kernel 50, Popping 100, Popcorn 250, Salted 500, Buttery Popcorn 1,000. Small versions are uploaded as the bot's own emojis at start-up, so the card shows the collection inline; finished series (every episode in the library watched), daily watch streaks. `/jf-achievements` shows them as one card; `/jf-link` ties a Discord member to their Jellyfin user |
| **Movie night** | Themed roles with a picker in `#roles`, `/movienight` and `/movievote`. See below |
| **Health** | A heartbeat file for Docker's health check, written only while Discord *and* Jellyfin answer, and an optional Uptime Kuma push |

### Commands

`/jf-health` · `/jf-stats` · `/jf-status` · `/jf-recent` · `/jf-stats-graph` · `/jf-popular` ·
`/jf-subscribe` · `/jf-unsubscribe` · `/jf-subscriptions` · `/jf-weekly-digest` ·
`/jf-achievements` · `/jf-achievements-all` · `/jf-link` · `/jf-streaks` · `/jf-streaks-leaderboard` ·
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

Names in [cogs/movie_night.py](cogs/movie_night.py) are the source of truth. Rename a role there, not in
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

### Layout

| | |
|---|---|
| `bot.py` | Entry point: the bot class, start-up order, loading the cogs |
| `config.py` | Settings from the environment |
| `jellyfin_client.py` | Jellyfin API calls |
| `watch_data.py` | The SQLite database: recording plays, streaks, achievements |
| `checks.py` | The owner-only check and the slash-command error handler |
| `parties.py` | Working out watch parties from Jellyfin's sessions |
| `tiers.py` | Watch-time tiers and drawing their badges. Art in `assets/`, licences in [assets/LICENSES.md](assets/LICENSES.md) |
| `cogs/` | One feature each: `dashboard`, `activity` (the feed, and where plays are recorded), `digest`, `health` (heartbeat), `server` (status, stats, recent, popular, graph), `subscriptions`, `achievements`, `movie_night`, `help` |

Background loops start when the bot dispatches `services_ready`, ten seconds after it connects
and has set up the database and the movie night channels. Each cog with a loop listens for it.

### Running locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q    # the tests CI runs
cp .env.example .env    # fill in, then:
set -a; . ./.env; set +a; python bot.py
```

**Tests** live in `tests/`: time parsing, the role picker, streaks and achievements against a
throwaway database, and the command set. CI runs them on every push and pull request, before
anything is built.

**Releasing:** merge to `main`, then tag `vX.Y.Z` and push the tag. The workflow runs the tests,
builds the image, checks that the bot imports, and publishes `X.Y.Z` (and `sha-…` for every `main` commit). The
server pins a version, so nothing changes there until its compose file moves to the new tag.

## History

Written for a media server that ran on a gaming PC, then moved with it to a Proxmox homelab in
2026-10. It lived in that homelab's repo until it got its own here. Earlier history is not
carried over.
