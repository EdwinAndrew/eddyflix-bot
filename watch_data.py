"""The watch-history database: tables, recording plays, streaks and achievements."""
from datetime import datetime

import aiosqlite

import config


async def init_database():
    """Initialize SQLite database with tables for watch history and subscriptions"""
    async with aiosqlite.connect(config.DB_PATH) as db:
        # Watch history table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS watch_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                user_name TEXT NOT NULL,
                item_id TEXT NOT NULL,
                item_name TEXT NOT NULL,
                item_type TEXT NOT NULL,
                series_name TEXT,
                season_number INTEGER,
                episode_number INTEGER,
                start_time TIMESTAMP NOT NULL,
                end_time TIMESTAMP,
                duration_seconds INTEGER,
                play_method TEXT,
                device_name TEXT
            )
        """)

        # Subscriptions table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_user_id TEXT NOT NULL,
                discord_username TEXT NOT NULL,
                show_name TEXT NOT NULL,
                show_id TEXT,
                subscribed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(discord_user_id, show_name)
            )
        """)

        # Popular content cache table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS popular_content (
                item_id TEXT PRIMARY KEY,
                item_name TEXT NOT NULL,
                item_type TEXT NOT NULL,
                series_name TEXT,
                total_watch_time INTEGER DEFAULT 0,
                watch_count INTEGER DEFAULT 0,
                last_watched TIMESTAMP
            )
        """)

        # Migration: ensure `series_name` column exists for older databases
        try:
            async with db.execute("PRAGMA table_info(popular_content)") as cursor:
                cols = await cursor.fetchall()
                col_names = [c[1] for c in cols]
            if 'series_name' not in col_names:
                await db.execute("ALTER TABLE popular_content ADD COLUMN series_name TEXT")
        except Exception:
            # If anything goes wrong here, continue - table will be created above for new DBs
            pass

        # Achievements/milestones table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS achievements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_name TEXT NOT NULL,
                achievement_type TEXT NOT NULL,
                achievement_name TEXT NOT NULL,
                item_name TEXT,
                value INTEGER,
                earned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                reported BOOLEAN DEFAULT 0
            )
        """)

        # Streaks table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS streaks (
                user_name TEXT PRIMARY KEY,
                current_streak INTEGER DEFAULT 0,
                longest_streak INTEGER DEFAULT 0,
                last_watch_date TEXT,
                streak_start_date TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.commit()
        print("Database initialized successfully")

async def record_watch_start(session_id: str, session: dict):
    """Record when a user starts watching content"""
    now_playing = session.get('NowPlayingItem')
    if not now_playing:
        return

    user_name = session.get('UserName', 'Unknown')
    item_id = now_playing.get('Id')
    item_name = now_playing.get('Name', 'Unknown')
    item_type = now_playing.get('Type', 'Unknown')
    device_name = session.get('DeviceName', 'Unknown')
    play_method = session.get('PlayState', {}).get('PlayMethod', 'Unknown')

    # Series info
    series_name = now_playing.get('SeriesName')
    season_number = now_playing.get('ParentIndexNumber')
    episode_number = now_playing.get('IndexNumber')

    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("""
            INSERT INTO watch_history
            (session_id, user_name, item_id, item_name, item_type, series_name,
             season_number, episode_number, start_time, play_method, device_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (session_id, user_name, item_id, item_name, item_type, series_name,
              season_number, episode_number, datetime.now(), play_method, device_name))
        await db.commit()

async def record_watch_end(jf, session_id: str, item_id: str):
    """Record when a user stops watching content"""
    async with aiosqlite.connect(config.DB_PATH) as db:
        # Find the most recent watch entry for this session and item
        async with db.execute("""
            SELECT id, start_time, user_name, series_name, item_name, item_type FROM watch_history
            WHERE session_id = ? AND item_id = ? AND end_time IS NULL
            ORDER BY start_time DESC LIMIT 1
        """, (session_id, item_id)) as cursor:
            row = await cursor.fetchone()

            if row:
                entry_id, start_time, user_name, series_name, item_name, item_type = row
                end_time = datetime.now()
                start_dt = datetime.fromisoformat(start_time)
                duration = int((end_time - start_dt).total_seconds())

                await db.execute("""
                    UPDATE watch_history
                    SET end_time = ?, duration_seconds = ?
                    WHERE id = ?
                """, (end_time, duration, entry_id))

                # Update popular content cache
                await db.execute("""
                    INSERT INTO popular_content (item_id, item_name, item_type, series_name, total_watch_time, watch_count, last_watched)
                    SELECT item_id, item_name, item_type, series_name, ?, 1, ?
                    FROM watch_history WHERE id = ?
                    ON CONFLICT(item_id) DO UPDATE SET
                        total_watch_time = total_watch_time + ?,
                        watch_count = watch_count + 1,
                        last_watched = ?
                """, (duration, end_time, entry_id, duration, end_time))

                # Check for achievements/milestones
                await check_achievements(jf, user_name, series_name, item_name, item_type, item_id, db)

                # Update streaks
                await update_streak(user_name, db)

                await db.commit()

async def update_streak(user_name: str, db, today=None):
    """Update user's watch streak and check for milestone achievements.

    today is a datetime.date; it defaults to the real date and exists for tests.
    """
    from datetime import date, timedelta

    day = today or date.today()
    today = day.isoformat()
    
    # Get current streak info
    async with db.execute("""
        SELECT current_streak, longest_streak, last_watch_date, streak_start_date
        FROM streaks WHERE user_name = ?
    """, (user_name,)) as cursor:
        row = await cursor.fetchone()
    
    if row:
        current_streak, longest_streak, last_watch_date, streak_start_date = row
        
        # Check if user already watched today
        if last_watch_date == today:
            return  # Already counted for today
        
        # Check if streak continues (watched yesterday)
        yesterday = (day - timedelta(days=1)).isoformat()
        if last_watch_date == yesterday:
            # Continue streak
            new_streak = current_streak + 1
        else:
            # Streak broken, start new one
            new_streak = 1
            streak_start_date = today
    else:
        # First time tracking this user
        new_streak = 1
        longest_streak = 0
        streak_start_date = today
    
    # Update longest streak if needed
    if new_streak > (longest_streak or 0):
        longest_streak = new_streak
    
    # Insert or update
    await db.execute("""
        INSERT INTO streaks (user_name, current_streak, longest_streak, last_watch_date, streak_start_date, updated_at)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_name) DO UPDATE SET
            current_streak = ?, longest_streak = ?, last_watch_date = ?, streak_start_date = ?, updated_at = CURRENT_TIMESTAMP
    """, (user_name, new_streak, longest_streak, today, streak_start_date,
          new_streak, longest_streak, today, streak_start_date))
    
    # Check for streak milestones (7, 30, 100 days)
    streak_milestones = [7, 30, 100]
    for milestone in streak_milestones:
        if new_streak == milestone:
            # Award achievement
            ach_name = f"{milestone} Day Streak"
            async with db.execute("""
                SELECT id FROM achievements
                WHERE user_name = ? AND achievement_type = 'streak_milestone' AND value = ?
            """, (user_name, milestone)) as check:
                if not await check.fetchone():
                    await db.execute("""
                        INSERT INTO achievements (user_name, achievement_type, achievement_name, value, earned_at, reported)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (user_name, 'streak_milestone', ach_name, milestone, datetime.now().isoformat(), 0))

async def check_achievements(jf, user_name: str, series_name: str, item_name: str, item_type: str, item_id: str, db):
    """Check if user has earned any achievements"""
    
    # Check if user finished a series (all episodes watched)
    if item_type == 'Episode' and series_name:
        # Count watched distinct episodes by series_name
        async with db.execute("""
            SELECT COUNT(DISTINCT item_id) as watched_episodes
            FROM watch_history
            WHERE user_name = ? AND series_name = ? AND item_type = 'Episode'
        """, (user_name, series_name)) as cursor:
            result = await cursor.fetchone()
            watched_count = result[0] if result else 0

        # Try to get accurate total episodes from Jellyfin using series id
        total_episodes = None
        try:
            # Fetch item details to get series id
            item = await jf.get_item(item_id)
            series_id = item.get('SeriesId') or item.get('SeriesId') or item.get('ParentId')
            if series_id:
                total_episodes = await jf.get_series_episode_count(series_id)
        except Exception:
            total_episodes = None

        # Determine completion: prefer accurate count if available
        completed = False
        if total_episodes and total_episodes > 0:
            if watched_count >= total_episodes:
                completed = True
        else:
            # Fallback heuristic: at least 10 distinct episodes
            if watched_count >= 10:
                completed = True

        if completed:
            async with db.execute("""
                SELECT id FROM achievements
                WHERE user_name = ? AND achievement_type = 'series_completion' AND item_name = ?
            """, (user_name, series_name)) as check:
                if not await check.fetchone():
                    await db.execute("""
                        INSERT INTO achievements (user_name, achievement_type, achievement_name, item_name, value)
                        VALUES (?, ?, ?, ?, ?)
                    """, (user_name, 'series_completion', f'Finished "{series_name}"', series_name, watched_count))
    
    # Check for watch time milestones
    async with db.execute("""
        SELECT SUM(duration_seconds) as total_seconds
        FROM watch_history
        WHERE user_name = ? AND duration_seconds IS NOT NULL
    """, (user_name,)) as cursor:
        result = await cursor.fetchone()
        if result and result[0]:
            total_hours = result[0] / 3600
            
            for milestone in config.ACHIEVEMENT_MILESTONES:
                if total_hours >= milestone:
                    async with db.execute("""
                        SELECT id FROM achievements
                        WHERE user_name = ? AND achievement_type = 'watch_milestone' AND value = ?
                    """, (user_name, milestone)) as check:
                        if not await check.fetchone():
                            await db.execute("""
                                INSERT INTO achievements (user_name, achievement_type, achievement_name, value)
                                VALUES (?, ?, ?, ?)
                            """, (user_name, 'watch_milestone', f'Watched {milestone} Hours', milestone))
