"""Settings, all from environment variables. See .env.example."""
import os

JELLYFIN_URL = os.getenv("JELLYFIN_URL", "http://jellyfin:8096")
JELLYFIN_API_KEY = os.getenv("JELLYFIN_API_KEY")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DASHBOARD_CHANNEL_ID = int(os.getenv("DASHBOARD_CHANNEL_ID", "0"))
ACTIVITY_CHANNEL_ID = int(os.getenv("ACTIVITY_CHANNEL_ID", "0"))
UPDATE_INTERVAL = int(os.getenv("UPDATE_INTERVAL", "60"))

# Where people open Jellyfin from, e.g. https://jellyfin.example.com. Used for
# links only, never for images. Empty means no links.
JELLYFIN_PUBLIC_URL = os.getenv("JELLYFIN_PUBLIC_URL", "").rstrip("/")

# Uptime Kuma has no Docker socket, so it cannot see the container's health
# status. Each good heartbeat also hits a Kuma Push monitor, which alerts when
# the pushes stop. Optional: empty means file only.
UPTIME_KUMA_PUSH_URL = os.getenv("UPTIME_KUMA_PUSH_URL", "")

DB_PATH = "/app/data/jellyfin_bot.db"
