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
| **New on EddyFlix** | Sundays a minute after the digest, in the same channel: what was added in the last 7 days. New films, new shows, and shows with new episodes on one line each ("The Simpsons · 25 new episodes · S09E01–E25"), with links and one uploaded poster grid. Skipped on a week with nothing new. `/jf-weekly-new` posts it now (owner only) |
| **Achievements and streaks** | Watch-time tiers with badges, popcorn from the cob up: Corn at 10 h, Kernel 50, Popcorn 100, Salted 250, Buttery Popcorn 500, Bottomless Bucket 1,000. Small versions are uploaded as the bot's own emojis at start-up, so the card shows the collection inline; finished series (every episode in the library watched), daily watch streaks, movie nights attended. `/jf-achievements` shows them as one card; `/jf-link`, or the 👤 button in `#roles`, ties a Discord member to their Jellyfin user |
| **New-episode DMs** | `/jf-subscribe` to a show from the library and the bot DMs you when new episodes land, one message per show per batch ("6 new episodes of Fallout: S02E01–E06") with the poster and links. Checked every five minutes; a show still importing is held for ten minutes so a whole season is one message. Closed DMs get a mention in the activity channel instead |
| **Find something to watch** | `/watch` searches films and shows and answers only you: poster, overview, runtime, a link to open it in Jellyfin and, once you have used `/jf-link`, where you left off (the next episode, or the time you stopped a film at) |
| **Getting started** | A guide in `#roles` for newcomers: how to get an account, which app on an LG TV, an iPhone or in a browser, the address, where to request things, and what to do on Discord after. The picker next to it has a **Which Jellyfin user are you?** button, the same as `/jf-link` without knowing the command |
| **Pick something** | `/pick` picks a random film or show, with the same card as `/watch` and a **🎲 Reroll** button that swaps it in place (it goes after ten minutes unused). Narrow it by type, genre (from the library) and length (for a show, the episode length). Once you have used `/jf-link` it picks only what you haven't watched, or haven't finished for a show, unless you ask for `include_watched` |
| **Continue watching** | `/continue` lists what you were in the middle of, up to eight, newest first and linked to Jellyfin: films you stopped part-way, and for each show the episode you stopped in or the next one. Only you see it; it needs `/jf-link` |
| **Movie night** | Themed roles with a picker in `#roles`, `/movienight` and `/movievote`, a reminder 15 minutes before, the event shown as live, a recap of who was in the Theatre with a 1–5 🍿 rating, and `/movienight-history`. `/spoiler` puts someone in *Spoiler Alert* for a while. See below |
| **Health** | A heartbeat file for Docker's health check, written only while Discord *and* Jellyfin answer, and an optional Uptime Kuma push. `/jf-selftest` (owner) checks everything end to end; see [Testing](#testing) |

### Commands

`/jf-health` · `/jf-stats` · `/jf-status` · `/jf-recent` · `/jf-stats-graph` · `/jf-popular` ·
`/jf-subscribe` · `/jf-unsubscribe` · `/jf-subscriptions` · `/jf-weekly-digest` · `/jf-weekly-new` ·
`/jf-achievements` · `/jf-achievements-all` · `/jf-link` · `/jf-streaks` · `/jf-streaks-leaderboard` ·
`/jf-debug-sessions` · `/jf-selftest` · `/jf-help` · `/watch` · `/pick` · `/continue` · `/movienight` · `/movievote` · `/spoiler` · `/unspoiler` · `/movienight-history`

### Movie night

On start the bot creates whatever is missing, and never deletes anything:

- **Roles.** 🎬 Director (admins) and 🔦 Usher (mods), hoisted, with moderation permissions.
  🎟️ Ticket Holder, opt-in, pinged for movie nights. Thirteen cosmetic flairs, "the friend
  everyone has on movie night" (*Talks During the Movie*, *Late Arrival*, *Spoiler Alert*…)
- **`#roles`** gets two messages, each found by its title and edited in place on every start, so
  a change to the text or the settings shows up after a restart:
  - **A getting-started guide**: ask the owner or a 🎬 Director for an account; the Jellyfin app
    on an LG TV (webOS) or an iPhone, or a browser; the address (`JELLYFIN_PUBLIC_URL`); requests
    on Seerr (`SEERR_PUBLIC_URL`, left out when unset); Canada only; then link yourself, take a
    ticket, and a few commands. It lives in `#roles` rather than a channel of its own because
    its last steps are the buttons right there. A new `#roles` gets it above the picker
  - **A picker**: a menu for flairs, a button for the ticket, and **👤 Which Jellyfin user are
    you?**. That one answers, only to you, with a menu of the Jellyfin users nobody has linked yet
    (the first 25; past that, `/jf-link`). Already linked, it says who you are and lets you switch
    to a free user; a user someone else has claimed can only be moved by the owner with
    `/jf-link member:`. The buttons survive restarts
- **A 🎬 Movie Night category**: `#movie-night` (announcements, staff only), `#watch-chat`, and
  a 🎥 Theatre voice channel
- **`/movienight film day time`**: the film from the library, a day (`tonight`, `friday`,
  `2026-10-31`) and a time (`8pm`, `20:00`). Creates a Discord event with the film's backdrop
  and posts an announcement with the poster and SyncPlay steps. **SyncPlay does not open the film
  for the others**: everyone opens the same film, joins the group, then playback stays in sync
- **`/movievote`**: a Discord poll of up to four films, random ones if none are given
- **`/spoiler @member [hours]`** (Directors and Ushers): *🙊 Spoiler Alert* for that many hours
  (default 24, up to a week) and a line in `#watch-chat`. The bot takes it off when the time is up,
  checking once a minute; the expiry is in the database, so a restart does not lose it. While it
  runs, the `#roles` picker keeps it on and says until when. Giving it again starts the clock over
  from now, longer or shorter. **`/unspoiler @member`** lifts it early. Someone who wore the flair
  by choice before keeps it afterwards
- **On the night** the bot follows every event it made, checking once a minute. Discord's own
  reminder only reaches people who pressed *Interested*, so:
  - **15 minutes before**, Ticket Holders get a ping in `#movie-night` with the SyncPlay steps
    again. Not when the night was announced less than half an hour before
  - **From the start time** it counts who is in 🎥 Theatre, and starts the Discord event (shown
    as live) once someone is there. Discord ends a voice event a few minutes after its channel
    empties, so starting it into an empty channel would end it before anyone came
  - **At the end time** (runtime + 15 minutes), or once the Theatre has been empty for 15
    minutes in the second half, it ends the event and posts who was there. **15 minutes in the
    Theatre counts as attending**; less is listed as *dropped by*. The count goes on
    `/jf-achievements`, per Discord member, so it shows up once someone uses `/jf-link`
  - A cancelled or deleted event is dropped without a recap. An event Discord ends early, or
    someone ends by hand, does not end the night: the bot keeps counting until the end time
  - Nights are kept in the database, so a restart neither repeats the reminder or the recap nor
    loses the minutes counted. Events made before this existed are picked up from Discord
- **Rate it.** The recap has 1–5 🍿 buttons, open for 24 hours from the recap, for anyone who was
  in the Theatre that night (attended or dropped by; anyone at all if nobody was counted). One
  rating per member per night, pressing another replaces it, and the recap shows the running
  average. The buttons keep working after a restart
- **`/movienight-history`**: finished nights, newest first, with the date, film, host, how many
  came (15 minutes in the Theatre) and the average rating, and the club's top-rated films once two
  or more have ratings

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
| `selftest.py` | Read-only checks against the live Jellyfin and the database, run after each deploy and by `/jf-selftest` |
| `parties.py` | Working out watch parties from Jellyfin's sessions |
| `screenings.py` | Movie night timing: when to remind, start and end, who attended, the recap |
| `new_episodes.py` | Finding newly added episodes and grouping them for subscription DMs |
| `new_this_week.py` | The weekly "New on EddyFlix" post: what was added in 7 days, grouped, and its poster grid |
| `ratings.py` | Movie night ratings: who may rate, the persistent 🍿 buttons, the history lines |
| `tiers.py` | Watch-time tiers and drawing their badges. Art in `assets/`, licences in [assets/LICENSES.md](assets/LICENSES.md) |
| `cogs/` | One feature each: `dashboard`, `activity` (the feed, and where plays are recorded), `digest`, `weekly_new` (New on EddyFlix), `health` (heartbeat), `server` (status, stats, recent, popular, graph), `subscriptions` (and the new-episode poll), `achievements`, `movie_night` (and `#roles`: the picker, the Jellyfin link button and the guide), `movie_history` (`/movienight-history`), `spoiler`, `watch` (with `/pick` and `/continue`), `help` |

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

### Testing

Three layers, from cheapest to the real thing:

1. **Unit tests** in `tests/`, run by CI on every push and pull request before anything is built:
   time parsing, the role picker (and its Spoiler Alert lock), the Jellyfin link
   menu and the getting-started guide, `/spoiler` expiry, streaks and achievements against a throwaway database, movie
   night timing and attendance, movie night ratings and history, new-episode batching, the weekly
   "new this week" grouping and poster grid, the command set, and the Discord calls
   against fake objects. `tests/test_recorded_jellyfin.py` runs `/watch` and the new-episode DMs
   against **real Jellyfin responses** in `tests/fixtures/jellyfin/`, recorded from the server
   (trimmed, no user names) by `tests/fixtures/record_jellyfin.py`. Re-record when Jellyfin is
   upgraded; the docstring says how
2. **After each deploy**, against the live Jellyfin, read-only:
   `docker exec jellyfin-discord-bot python selftest.py`. One line per check, exit 1 on failure
3. **`/jf-selftest`** in Discord, owner only, once per release: the same checks plus the ones only
   Discord can answer. It DMs you samples of the new-episode DM, the movie night reminder and the
   recap; creates, starts, ends and deletes a private test event in 🎥 Theatre; checks the bot can
   post and ping Ticket Holders in `#movie-night`; and, if you are in Theatre, that it sees you.
   It pings nobody and leaves nothing behind

What only a real night shows: how quickly Discord ends an empty live event, and the recap with
real people.

**Releasing:** merge to `main`, then tag `vX.Y.Z` and push the tag. The workflow runs the tests,
builds the image, checks that the bot imports, and publishes `X.Y.Z` (and `sha-…` for every `main` commit). The
server pins a version, so nothing changes there until its compose file moves to the new tag.
After deploying, run `selftest.py` in the container, then `/jf-selftest` in Discord.

## History

Written for a media server that ran on a gaming PC, then moved with it to a Proxmox homelab in
2026-10. It lived in that homelab's repo until it got its own here. Earlier history is not
carried over.
