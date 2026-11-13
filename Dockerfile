# Pinned by version and digest so two builds of one commit are the same image.
# Dependabot proposes newer ones. Keep CI's python-version (build.yml) in step.
FROM python:3.15.2-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot code
COPY *.py ./
COPY cogs/ ./cogs/
COPY assets/ ./assets/

# Run as a non-root user. UID 1000 matches the PUID the rest of the stack
# uses and the owner of the bind-mounted data directory, so the bot can
# still write its SQLite database.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin botuser \
    && mkdir -p /app/data \
    && chown -R botuser:botuser /app
USER botuser

# The bot has no HTTP endpoint, so health is judged by the heartbeat file it
# rewrites every 30s. Restart-on-exit alone would not catch a process that is
# alive but disconnected from Discord. The bot also stops writing it after five
# minutes without a successful authenticated Jellyfin call.
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
    CMD python -c "import os,sys,time; sys.exit(0 if os.path.exists('/tmp/bot_heartbeat') and time.time()-os.path.getmtime('/tmp/bot_heartbeat') < 120 else 1)"

# Run bot
CMD ["python", "-u", "bot.py"]
