import sqlite3
import psycopg2

# --- Configurations ---
SQLITE_PATH = "/home/prathu/quiz_bot/leaderboard.db"
SUPABASE_URL = "postgresql://postgres:FlUVu8dA8xy02woL@db.wuhoozvbufnwjsfpkojp.supabase.co:5432/postgres"

def migrate_sunday_data():
    print("🔌 Connecting to both databases...")
    sqlite_conn = sqlite3.connect(SQLITE_PATH)
    sqlite_cursor = sqlite_conn.cursor()

    pg_conn = psycopg2.connect(SUPABASE_URL)
    pg_cursor = pg_conn.cursor()

    print("📦 Extracting permanent student profiles (Elos, Leagues, Factions)...")
    sqlite_cursor.execute("""
        SELECT user_id, first_name, faction, weekly_attempts, daily_attempts,
               last_updated, weekly_score, daily_score, base_elo, live_elo,
               weekly_correct, league_tier, is_captain
        FROM users
    """)
    users = sqlite_cursor.fetchall()

    print(f"🚀 Found {len(users)} students. Injecting into Supabase...")
    for user in users:
        pg_cursor.execute("""
            INSERT INTO users (
                user_id, first_name, faction, weekly_attempts, daily_attempts,
                last_updated, weekly_score, daily_score, base_elo, live_elo,
                weekly_correct, league_tier, is_captain
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                base_elo = EXCLUDED.base_elo,
                live_elo = EXCLUDED.live_elo,
                league_tier = EXCLUDED.league_tier,
                faction = EXCLUDED.faction;
        """, user)

    print("📦 Extracting historical weekly rank data...")
    try:
        sqlite_cursor.execute("SELECT user_id, week_num, rank, total_members, score, attempts, correct FROM weekly_rank_history")
        history = sqlite_cursor.fetchall()

        for h in history:
            pg_cursor.execute("""
                INSERT INTO weekly_rank_history (user_id, week_num, rank, total_members, score, attempts, correct)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id, week_num) DO NOTHING;
            """, h)
    except sqlite3.OperationalError:
        print("⚠️ No weekly history found to migrate, skipping...")

    print("📦 Extracting bot settings (Week Number, etc.)...")
    sqlite_cursor.execute("SELECT key, value FROM bot_settings")
    settings = sqlite_cursor.fetchall()

    for s in settings:
        pg_cursor.execute("""
            INSERT INTO bot_settings (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
        """, s)

    pg_conn.commit()
    sqlite_conn.close()
    pg_conn.close()
    print("✅ Migration Bridge complete! All data successfully transferred.")

if __name__ == "__main__":
    migrate_sunday_data()
