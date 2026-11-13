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
| **Activity feed** | What people watch, not when they open an app: one post per sitting with the poster, **▶️ Now watching**, edited as they move on to the next episode and into **⏹️ Watched** with the time when they stop. Stopping and playing again within 10 minutes, on any device, carries on the same post. Plays under 5 minutes (browsing, sampling) are never posted. Two or more people on the same title at about the same point get one **🍿 Watching together** post instead, edited as people join and turned into **🎬 Watched together** at the end. Connections are not posted |
| **Weekly digest** | Sundays at 6 PM: watch time, top shows, who watched most |
| **Stats** | `/stats`, one command with a choice of view: server status and who is streaming, recently added, most watched, a chart of watch time per day, or the streak leaderboard |
| **New on EddyFlix** | Sundays a minute after the digest, in the same channel: what was added in the last 7 days. New films, new shows, and shows with new episodes on one line each ("The Simpsons · 25 new episodes · S09E01–E25"), with links and one uploaded poster grid. It ends with "Missing something? Ask for it with `/request`", with a link to Seerr when `SEERR_PUBLIC_URL` is set; that line is left out when neither `/request` nor `SEERR_PUBLIC_URL` is set up. Skipped on a week with nothing new. `/admin weekly-new` posts it now (owner only) |
| **Admin channel** | Optional, private, for the owner (`ADMIN_CHANNEL_ID`). Sundays at 6 PM, or now with `/admin summary`: how many plays were transcoded and why, by device (Jellyfin's transcode reasons, recorded as plays happen), split into full video (the picture re-encoded), audio only (the picture copied, the sound converted, e.g. Dolby audio in Chrome) and repackaged (both copied into a new container); only full video costs the server real work; what was added to the library; who watched and which Jellyfin users have not used `/link`; bot and Jellyfin health and disk space. The warning when three streams re-encode video at once (audio-only and repackaged ones don't count) goes here, once each time it happens; without the channel it stays in the activity feed. Sonarr, Radarr and Seerr are not in it yet: the bot has no API keys for them |
| **Achievements and streaks** | Watch-time tiers with badges, popcorn from the cob up: Corn at 10 h, Kernel 50, Popcorn 100, Salted 250, Buttery Popcorn 500, Bottomless Bucket 1,000. Small versions are uploaded as the bot's own emojis at start-up, so the card shows the collection inline; finished series (every episode in the library watched), daily watch streaks, movie nights attended. `/achievements` shows them as one card, `/streaks` your streak; `/link`, or the 👤 button in `#roles`, ties a Discord member to their Jellyfin user |
| **New-episode DMs** | `/subscribe` to a show from the library and the bot DMs you when new episodes land, one message per show per batch ("6 new episodes of Fallout: S02E01–E06") with the poster and links. Checked every five minutes; a show still importing is held for ten minutes so a whole season is one message. Closed DMs get a mention in the activity channel instead. You don't have to know the command: finish the newest episode the library has of a show, once you have used `/link`, and the bot DMs you a **🔔 Tell me when new episodes land** button that subscribes you the same way. Finished means the feed last saw 90% of it played, or Jellyfin marks it played. Closed DMs get the mention instead. Asked once per member per show, ever, whatever you do with it; not for a show you are subscribed to already or one Jellyfin lists as Ended. The button keeps working after a restart |
| **Find something to watch** | `/watch` searches films and shows and answers only you: poster, overview, runtime, a link to open it in Jellyfin and, once you have used `/link`, where you left off (the next episode, or the time you stopped a film at). For a show, it also says where the other linked members are, so you can dodge spoilers or catch up: "Sarah is 3 episodes ahead of you", "2 episodes behind you", "at the same episode". Only members who have watched some of it are listed |
| **Getting started** | A guide in `#roles` for newcomers: how to get an account, which app on an LG TV, an iPhone or in a browser, the address, where to request things, and what to do on Discord after. The picker next to it has a **Which Jellyfin user are you?** button, the same as `/link` without knowing the command |
| **Pick something** | `/pick` picks a random film or show, with the same card as `/watch` and a **🎲 Reroll** button that swaps it in place (it goes after ten minutes unused). Narrow it by type, genre (from the library) and length (for a show, the episode length). Once you have used `/link` it picks only what you haven't watched, or haven't finished for a show, unless you ask for `include_watched`. `with:@member` picks only what neither of you has watched (or finished, for a show), and rerolls keep to that; the card says "Neither of you has seen this". You both need `/link` (or the 👤 button in `#roles`), and it tells you who doesn't. `include_watched` turns the filter off for both. A show's card says where the others are, as on `/watch` |
| **Continue watching** | `/continue` lists what you were in the middle of, up to eight, newest first and linked to Jellyfin: films you stopped part-way, and for each show the episode you stopped in or the next one, with where the other linked members are in it ("↳ Sarah is 3 episodes ahead of you"). Only you see it; it needs `/link` |
| **Requests** | `/request` searches Seerr for any film or show and answers only you: poster, overview, and whether it is in the library, partly there, already requested, or can be requested. A button sends the request (for a show: all missing seasons, or just the latest), made as your Seerr user once you have used `/link`. When it can be watched the bot DMs you the poster and a Jellyfin link; for a show, once every season you asked for is at least partly in. Checked every five minutes. Closed DMs get a mention in the activity channel instead. Off unless `SEERR_URL` and `SEERR_API_KEY` are set |
| **Movie night** | Themed roles with a picker in `#roles`, `/movienight` and `/movievote`, a reminder 15 minutes before, the event shown as live, a recap of who was in the Theatre with a 1–5 🍿 rating, and `/movienight-history`. `/spoiler` puts someone in *Spoiler Alert* for a while. See below |
| **Health** | A heartbeat file for Docker's health check, written only while Discord *and* Jellyfin answer, and an optional Uptime Kuma push. `/admin selftest` (owner) checks everything end to end; see [Testing](#testing) |

### Commands

Everyone: `/watch` · `/pick` · `/continue` · `/request` · `/subscribe` · `/unsubscribe` ·
`/subscriptions` · `/link` · `/achievements` · `/streaks` · `/stats` · `/movienight-history` · `/help`

`/stats` is one command with a `view`: server status and streams (the default), recently added,
most watched, a watch time chart (`days`), or the streak leaderboard.

Staff (🎬 Directors and 🔦 Ushers): `/movienight` · `/movievote` · `/spoiler` · `/unspoiler`. Discord
lists them only for people with *Timeout Members*, which both staff roles have; each command still
checks for the role (or *Manage Events*) itself.

Owner: `/admin summary` · `/admin weekly-digest` · `/admin weekly-new` · `/admin selftest` ·
`/admin health` · `/admin sessions` (plays never closed in the database) · `/admin achievements`
(everyone's). The `/admin` group is listed only for Administrators, and every command in it checks
for the server owner (or the bot's owner).

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
    (the first 25; past that, `/link`). Already linked, it says who you are and lets you switch
    to a free user; a user someone else has claimed can only be moved by the owner with
    `/link member:`. The buttons survive restarts
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
    `/achievements`, per Discord member, so it shows up once someone uses `/link`
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
      - ADMIN_CHANNEL_ID=${ADMIN_CHANNEL_ID:-0}
    volumes:
      - ./data:/app/data        # SQLite: watch history, streaks, subscriptions. Owned by uid 1000
    restart: unless-stopped
```

All settings are in [.env.example](.env.example).

- **Jellyfin:** an API key from *Dashboard → API Keys*. Jellyfin 12 accepts it only as
  `Authorization: MediaBrowser Token="…"`, which is what the bot sends
- **Seerr (optional, for `/request`):** `SEERR_URL=http://seerr:5055` and the API key from
  *Settings → General*. The key acts as Seerr's admin, which lets the bot make a request as the
  member's own Seerr user (their quota and approval rules apply). Members who have not used
  `/link`, or whose Jellyfin user is not in Seerr, request with the admin account
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
| `seerr_client.py` | Seerr API calls, for `/request` |
| `media_requests.py` | What Seerr says about a title, which seasons to offer, and when a request is ready to announce |
| `watch_data.py` | The SQLite database: recording plays, streaks, achievements |
| `checks.py` | The owner-only check and the slash-command error handler |
| `selftest.py` | Read-only checks against the live Jellyfin and the database, run after each deploy and by `/admin selftest` |
| `parties.py` | Working out watch parties from Jellyfin's sessions |
| `viewings.py` | The feed's one post per sitting: following plays, the short-play rule, the post |
| `admin_summary.py` | The weekly admin summary's sections, and Jellyfin's transcode reasons in words |
| `screenings.py` | Movie night timing: when to remind, start and end, who attended, the recap |
| `new_episodes.py` | Finding newly added episodes and grouping them for subscription DMs; subscribing, for `/subscribe` and the 🔔 offer |
| `catch_up.py` | The 🔔 offer to someone who just caught up on a show: when it counts, the once-per-show record, the persistent button |
| `new_this_week.py` | The weekly "New on EddyFlix" post: what was added in 7 days, grouped, and its poster grid |
| `household.py` | Watching as a household: who else is linked, `/pick with:` for two, and how many episodes ahead or behind the others are in a show |
| `ratings.py` | Movie night ratings: who may rate, the persistent 🍿 buttons, the history lines |
| `tiers.py` | Watch-time tiers and drawing their badges. Art in `assets/`, licences in [assets/LICENSES.md](assets/LICENSES.md) |
| `cogs/` | One feature each: `dashboard`, `activity` (the feed, and where plays are recorded), `digest`, `weekly_new` (New on EddyFlix), `admin` (the owner's `/admin` group and the weekly admin summary), `health` (heartbeat), `server` (`/stats`, and the health check behind `/admin health`), `subscriptions` (and the new-episode poll, and the 🔔 catch-up offer), `requests` (and the Seerr poll), `achievements`, `movie_night` (and `#roles`: the picker, the Jellyfin link button and the guide), `movie_history` (`/movienight-history`), `spoiler`, `watch` (with `/pick` and `/continue`), `help` |

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
   night timing and attendance, movie night ratings and history, new-episode batching, the 🔔 catch-up offer (when it is made, once only, the button), Seerr requests (statuses, seasons, one DM per request), the weekly
   "new this week" grouping and poster grid, the feed's viewing posts, the admin summary, `/pick with:` and "episodes ahead of you", the command set, and the Discord calls
   against fake objects. `tests/test_recorded_jellyfin.py` runs `/watch` and the new-episode DMs
   against **real Jellyfin responses** in `tests/fixtures/jellyfin/`, recorded from the server
   (trimmed, no user names) by `tests/fixtures/record_jellyfin.py`. Re-record when Jellyfin is
   upgraded; the docstring says how
2. **After each deploy**, against the live Jellyfin, read-only:
   `docker exec jellyfin-discord-bot python selftest.py`. One line per check, exit 1 on failure
3. **`/admin selftest`** in Discord, owner only, once per release: the same checks plus the ones only
   Discord can answer. It DMs you samples of the new-episode DM, the movie night reminder and the
   recap; creates, starts, ends and deletes a private test event in 🎥 Theatre; checks the bot can
   post and ping Ticket Holders in `#movie-night`, and post in the admin channel if there is one;
   and, if you are in Theatre, that it sees you. It pings nobody and leaves nothing behind

What only a real night shows: how quickly Discord ends an empty live event, and the recap with
real people.

**Releasing:** merge to `main`, then tag `vX.Y.Z` and push the tag. The workflow runs the tests,
builds the image, checks that the bot imports, and publishes `X.Y.Z` (and `sha-…` for every `main` commit). The
server pins a version, so nothing changes there until its compose file moves to the new tag.
After deploying, run `selftest.py` in the container, then `/admin selftest` in Discord.
The bot syncs its slash commands globally on every start, which also removes any it no longer has;
a Discord client may need a restart (Ctrl+R) to show the change.

## History

Written for a media server that ran on a gaming PC, then moved with it to a Proxmox homelab in
2026-10. It lived in that homelab's repo until it got its own here. Earlier history is not
carried over.
