import threading
import os
from flask import Flask, request, render_template, jsonify # type: ignore
from flask_compress import Compress  # type: ignore # ✨ 1. Import Compress
import requests
import time
import json
from datetime import datetime, timedelta
import psycopg2
from psycopg2 import pool
from google import genai
from google.genai import types # type: ignore
import hmac
import hashlib
from urllib.parse import parse_qsl

# --- DATABASE CONFIGURATION ---
# We use environment variables so your password isn't exposed on GitHub
DB_URL = os.environ.get("DATABASE_URL")

# High-speed connection pool to handle massive group traffic instantly
db_pool = psycopg2.pool.ThreadedConnectionPool(1, 8, DB_URL)

# ✨ NEW: REDIS CONFIGURATION (Fail-Safe)
import redis # type: ignore
REDIS_URL = os.environ.get("REDIS_URL")
# If the URL exists, connect. Otherwise, set to None so the app doesn't crash.
redis_client = redis.from_url(REDIS_URL) if REDIS_URL else None

def acquire_submission_lock(attempt_id):
    """
    Attempts to place a 5-minute lock on an attempt_id.
    Returns True if safe to process, False if it's a duplicate.
    """
    if not redis_client:
        return True  # Fail-open: If Redis is missing, allow it through to Supabase
        
    try:
        lock_key = f"lock:submit:{attempt_id}"
        # nx=True (Only set if it doesn't exist) | ex=300 (Auto-delete after 5 mins)
        is_acquired = redis_client.set(lock_key, "processing", nx=True, ex=300)
        return bool(is_acquired)
    except Exception as e:
        print(f"⚠️ Redis Connection Error: {e}")
        return True  # Fail-open: Do not penalise the student for a server glitch

# --- REDIS RATE LIMITER (Fail-Safe) ---
def check_and_set_cooldown(key, seconds=10):
    """
    Places a temporary cooldown lock in Redis.
    Returns True if currently ON cooldown (should block).
    Returns False if clear to proceed (and sets the cooldown).
    """
    if not redis_client:
        return False # Fail-open to local RAM if Redis is down
        
    try:
        # nx=True sets it only if it doesn't exist. ex=seconds sets the auto-expiry.
        is_acquired = redis_client.set(key, "cooldown", nx=True, ex=seconds)
        return not bool(is_acquired)
    except Exception as e:
        print(f"⚠️ Redis Cooldown Error: {e}")
        return False

# --- TELEGRAM SECURE AUTHENTICATION ---
def get_verified_user():
    """
    Validates the Telegram initData securely using HMAC-SHA-256.
    Returns the user dictionary if valid, otherwise returns None.
    """
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    if not init_data:
        return None

    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)
        
        if not received_hash:
            return None

        # Sort parameters alphabetically and construct the data-check-string
        data_check_string = "\n".join(f"{key}={parsed[key]}" for key in sorted(parsed))
        
        # Hash the bot token with "WebAppData"
        secret_key = hmac.new(b"WebAppData", TELEGRAM_TOKEN.encode(), hashlib.sha256).digest()
        
        # Calculate the final hash
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        # Compare hashes securely
        if not hmac.compare_digest(calculated_hash, received_hash):
            return None

        # Ensure the data isn't stale (e.g., older than 24 hours)
        auth_date = int(parsed.get("auth_date", 0))
        if time.time() - auth_date > 86400:
            return None

        user_raw = parsed.get("user")
        if not user_raw:
            return None
            
        return json.loads(user_raw)
        
    except Exception as e:
        print(f"⚠️ Auth Verification Error: {e}")
        return None

CRON_SECRET = os.environ.get("CRON_SECRET")

GITHUB_PAT = os.environ.get("GITHUB_PAT", "")

def get_db():
    # Try up to 3 times to find a breathing connection in the pool
    max_retries = 3
    for _ in range(max_retries):
        try:
            conn = db_pool.getconn()
            # ✨ INSTANT RAM CHECK: Zero network delay!
            if conn.closed == 0:
                return conn
            else:
                db_pool.putconn(conn, close=True)
        except Exception:
            pass
            
    # Fallback: if the pool is totally exhausted, grab one last time
    return db_pool.getconn()

def release_db(conn):
    try:
        # Return the healthy connection to the pool so others can use it
        db_pool.putconn(conn)
    except Exception as e:
        print(f"⚠️ Error releasing connection to pool: {e}")

# --- AI Configuration ---
# --- AI Configuration ---
RAW_API_KEYS = [
    os.environ.get("GEMINI_KEY_1"),
    os.environ.get("GEMINI_KEY_2"),
    os.environ.get("GEMINI_KEY_3"),
    os.environ.get("GEMINI_KEY_4"),
    os.environ.get("GEMINI_KEY_5"),
    os.environ.get("GEMINI_KEY_6")
]
API_KEYS = [k for k in RAW_API_KEYS if k]

AI_MODELS = [
    'gemini-3.5-flash-lite',
    'gemini-3.1-flash-lite'
]

from collections import deque

current_key_index = 0
LAST_AI_REPLY_TIME_MAIN = 0        # Tracks cooldown for the main group
LAST_AI_REPLY_TIME_THREADS = {}    # Tracks cooldown for support threads
THREAD_HISTORY = {}                # Tracks memory for support threads

app = Flask(__name__)
Compress(app)

# ✨ NEW: Enable CORS so Cloudflare Pages can fetch data from Render
@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    
    # 🟢 FIX: We MUST explicitly allow our new custom security headers!
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Telegram-Init-Data, X-Cron-Secret'
    
    response.headers['Access-Control-Allow-Methods'] = 'GET,PUT,POST,DELETE,OPTIONS'
    return response

# ✨ NEW: The High-Speed Tunnel to Telegram and GitHub
http_session = requests.Session()

# --- IN-MEMORY CACHE TO SAVE BANDWIDTH ---
RAM_CACHE = {
    "master_data": None,
    "last_bake_time": 0
}
CACHE_LOCK = threading.Lock() # ✨ NEW: Protects Render from Cache Stampedes
POLL_CACHE = {} # ✨ NEW: Caches poll correct options in RAM

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = "-1003875580290"
LIVE_MESSAGE_ID = 2662 
ADD_DB_KEY = os.environ.get("ADD_DB_KEY")
TELEGRAM_THREAD_ID = '2972'
ANNOUNCEMENT_THREAD_ID = 11 

SOURCE_CHAT_ID = "-1004333232429" 
THREAD_MAPPING = {
    6: 5, 4: 2343, 2: 3, 11: 7438, 86: 11, 88: 824, 182: 2972,
}

TARGET_CHANNEL_ID = "-1003094340896"
CHANNEL_SOURCE_THREAD = 271
COMMUNITY_GROUP_URL = "https://t.me/ezeditorialgroup"
MINI_APP_URL = "https://t.me/Ez_vocab_bot/leaderboard"

COUNTDOWN_THREAD_ID = 6539
COUNTDOWN_MESSAGE_ID = 6542 

# --- IN-MEMORY TELEGRAM AVATAR CACHE ---
AVATAR_CACHE = {}  # Format: { user_id: {"url": "https://...", "ts": 1234567890} }

def get_telegram_avatar_url(user_id):
    """Fetches user profile photo URL from Telegram with 24-hour in-memory caching."""
    if not user_id:
        return None
        
    now = time.time()
    if user_id in AVATAR_CACHE:
        cached = AVATAR_CACHE[user_id]
        if now - cached["ts"] < 86400:  # Valid for 24 hours
            return cached["url"]

    try:
        # 1. Ask Telegram Bot API for the user's profile photo
        res = http_session.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUserProfilePhotos",
            params={"user_id": user_id, "limit": 1},
            timeout=3
        )
        if res.status_code != 200:
            AVATAR_CACHE[user_id] = {"url": None, "ts": now}
            return None
            
        data = res.json()
        photos = data.get("result", {}).get("photos", [])
        if not photos or not photos[0]:
            AVATAR_CACHE[user_id] = {"url": None, "ts": now}
            return None

        # 2. Get the lowest-resolution thumbnail for instant load
        file_id = photos[0][0]["file_id"]

        # 3. Request the direct download path
        file_res = http_session.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile",
            params={"file_id": file_id},
            timeout=3
        )
        if file_res.status_code != 200:
            AVATAR_CACHE[user_id] = {"url": None, "ts": now}
            return None
            
        file_data = file_res.json()
        file_path = file_data.get("result", {}).get("file_path")
        if not file_path:
            AVATAR_CACHE[user_id] = {"url": None, "ts": now}
            return None

        full_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
        AVATAR_CACHE[user_id] = {"url": full_url, "ts": now}
        return full_url

    except Exception:
        return None

def notify_prathu(message):
    admin_chat_id = "716496729"
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": admin_chat_id,
        "text": f"🎩 🤖 **Lixie System Report:**\n{message}",
        "parse_mode": "Markdown"
    }
    try:
        http_session.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"⚠️ Could not send DM to Prathu: {e}")

@app.route('/add_poll', methods=['POST'])
def add_poll():
    data = request.get_json()
    if data.get('secret') != ADD_DB_KEY:
        return "Unauthorized", 401

    poll_id = data.get('poll_id')
    correct_index = data.get('correct_index')

    if poll_id is None or correct_index is None:
        return "Missing data", 400

    conn = get_db()
    c = conn.cursor()
    current_day_str = (datetime.utcnow() + timedelta(hours=5, minutes=30)).strftime('%a')
    
    c.execute("""
        INSERT INTO polls (poll_id, correct_index, poll_day) VALUES (%s, %s, %s)
        ON CONFLICT (poll_id) DO UPDATE SET 
            correct_index = EXCLUDED.correct_index, 
            poll_day = EXCLUDED.poll_day
    """, (poll_id, correct_index, current_day_str))
    
    conn.commit()
    c.close()
    release_db(conn)
    return "Poll successfully saved to remote DB!", 200

# ✨ FIX: Make queue_id optional for Redis compatibility
def process_answer(c, queue_id, user_id, first_name, poll_id, chosen_option): 
    max_retries = 3
    
    for attempt in range(max_retries):
        try:
            c.execute("SELECT 1 FROM user_answers WHERE user_id=%s AND poll_id=%s", (user_id, poll_id))
            if c.fetchone():
                # If they already answered, delete the duplicate ticket from the queue
                if queue_id:
                    c.execute("DELETE FROM answer_queue WHERE id = %s", (queue_id,))
                c.connection.commit()
                return

            global POLL_CACHE
            if poll_id in POLL_CACHE:
                correct_index, poll_day = POLL_CACHE[poll_id]
            else:
                c.execute("SELECT correct_index, poll_day FROM polls WHERE poll_id=%s", (poll_id,))
                poll_data = c.fetchone()
                if not poll_data:
                    return
                correct_index = poll_data[0]
                current_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
                poll_day = poll_data[1] if poll_data[1] else current_ist.strftime('%a')
                POLL_CACHE[poll_id] = (correct_index, poll_day)

            # ✨ FIX: Shifted this line OUTSIDE the 'else' block so it always runs
            is_correct = (chosen_option == correct_index)

            c.execute("""
                INSERT INTO users (user_id, first_name) VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET first_name = EXCLUDED.first_name
            """, (user_id, first_name))

            c.execute("""
                INSERT INTO user_answers (user_id, poll_id, is_correct, poll_day, chosen_option)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id, poll_id) DO UPDATE SET 
                    is_correct = EXCLUDED.is_correct, 
                    poll_day = EXCLUDED.poll_day, 
                    chosen_option = EXCLUDED.chosen_option
            """, (user_id, poll_id, int(is_correct), poll_day, chosen_option))

            c.execute("""
                UPDATE users
                SET weekly_attempts = weekly_attempts + 1,
                    daily_attempts = daily_attempts + 1,
                    last_updated = %s
                WHERE user_id = %s
            """, (time.time(), user_id))

            # ✨ FIX: Delete this specific answer from the queue ONLY because it succeeded!
            c.execute("DELETE FROM answer_queue WHERE id = %s", (queue_id,))

            # Commit the success and the deletion simultaneously
            c.connection.commit()
            break

        except Exception as db_err:
            # Rollback instantly to clear the aborted state
            c.connection.rollback()
            
            error_str = str(db_err).lower()
            if "deadlock" in error_str or "aborted" in error_str:
                if attempt < max_retries - 1:
                    time.sleep(1.5)
                    continue
            
            print(f"🚨 Memory batch execution error for user {user_id} (Attempt {attempt + 1}): {db_err}")
            # The loop breaks, but the answer STAYS in the queue for the next minute's cron job!
            break

def update_live_leaderboard():
    conn = get_db()
    c = conn.cursor()

    current_ist_time = datetime.utcnow() + timedelta(hours=5, minutes=30)
    monday_date = current_ist_time - timedelta(days=current_ist_time.weekday())
    sunday_date = monday_date + timedelta(days=6)
    date_range = f"{monday_date.strftime('%d %b')} – {sunday_date.strftime('%d %b')}"

    weekday_idx = current_ist_time.weekday()
    if weekday_idx == 6:
        phase_text = "Final Day! • Locks at Midnight"
    else:
        phase_text = f"Day {weekday_idx + 1} of 6 • Competition Active"

    c.execute("SELECT COUNT(*) FROM polls")
    total_quizzes = c.fetchone()[0]

    c.execute("SELECT value FROM bot_settings WHERE key='live_max_points'")
    max_pts_row = c.fetchone()
    if max_pts_row and max_pts_row[0]:
        raw_max = float(max_pts_row[0])
        max_pts = int(raw_max) if raw_max % 1 == 0 else round(raw_max, 2)
    else:
        max_pts = total_quizzes * 3 

    c.execute("""
        SELECT weekly_score, weekly_attempts, weekly_correct
        FROM users WHERE weekly_attempts > 0
    """)
    all_active_users = c.fetchall()

    total_active = len(all_active_users)

    target_average = 0
    safe_zone_count = 0
    global_accuracy_pct = 0

    if total_active > 0:
        sum_weighted_points = 0.0
        sum_weights = 0.0
        total_correct_global = 0
        total_attempts_global = 0

        for score, attempts, exact_correct in all_active_users:
            if attempts == 0:
                continue

            correct = exact_correct if exact_correct is not None else 0
            # Cap accuracy strictly between 0.0 and 1.0 (100%)
            accuracy = min(1.0, correct / attempts) if attempts > 0 else 0.0

            total_correct_global += correct
            total_attempts_global += attempts

            volume_weight = attempts / (attempts + 10.0)
            final_weight = volume_weight * accuracy
            if score < 0:
                final_weight = 0.0

            sum_weighted_points += (score * final_weight)
            sum_weights += final_weight

        if sum_weights > 0:
            target_average = int((sum_weighted_points / sum_weights) + 0.5)

        safe_zone_count = sum(1 for row in all_active_users if row[0] >= target_average)

        if total_attempts_global > 0:
            global_accuracy_pct = int((total_correct_global / total_attempts_global) * 100)

    # 1. Fetch House Cup Top 10
    c.execute("SELECT user_id, first_name, weekly_score, faction, is_captain FROM users WHERE weekly_attempts > 0 ORDER BY weekly_score DESC, last_updated ASC LIMIT 10")
    top_10 = c.fetchall()

    # 2. ✨ Fetch Elo Top 10 (Filtering out 7-day inactive users)
    seven_days_ago = time.time() - (7 * 24 * 3600)
    c.execute("""
        SELECT user_id, first_name, live_elo 
        FROM users 
        WHERE live_elo IS NOT NULL AND COALESCE(last_updated, 0) >= %s 
        ORDER BY live_elo DESC, last_updated ASC 
        LIMIT 10
    """, (seven_days_ago,))
    top_10_elo = c.fetchall()

    c.close()
    release_db(conn)

    # --- BUILD HOUSE CUP MESSAGE (MESSAGE 7628) ---
    msg_text = "🏆 **THE ENGLISH SCHOLARS' BATTLE** 🏆\n"
    msg_text += f"📅 {date_range} | 👥 {total_active} Active Students\n"
    msg_text += f"⏳ {phase_text}\n"
    msg_text += "━━━━━━━━━━━━━━━━━━━━\n\n"

    msg_text += "📊 **COMMUNITY PULSE**\n\n"
    msg_text += f"➪ Quizzes Released: {total_quizzes}\n"
    msg_text += f"➪ Maximum Score: {max_pts} pts\n"
    msg_text += f"➪ Promotion Cut-off: {target_average} pts\n"
    msg_text += f"➪ Safe Zone: {safe_zone_count} students\n"
    msg_text += f"➪ Global Accuracy: {global_accuracy_pct}%\n"
    msg_text += "━━━━━━━━━━━━━━━━━━━━\n\n"

    msg_text += "🎓 **TOP 10 ENGLISH SCHOLARS**\n\n"

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

    for i, user in enumerate(top_10):
        u_id, name, score, faction_val, is_captain = user
        clean_score = int(score) if score % 1 == 0 else round(score, 2)
        
        msg_text += f"{medals[i]} [{name}](tg://user?id={u_id}) ➪ {clean_score} pts\n"

    msg_text += "\n━━━━━━━━━━━━━━━━━━━━"

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText"

    payload = {
        "chat_id": CHAT_ID,
        "message_id": 12073,
        "text": msg_text,
        "parse_mode": "Markdown",
        "reply_markup": {
            "inline_keyboard": [[
                {
                    "text": "👤📊 My Dashboard",
                    "url": "https://t.me/Ez_vocab_bot/leaderboard"
                }
            ]]
        }
    }

    max_retries = 10
    for attempt in range(max_retries):
        try:
            res = http_session.post(url, json=payload, timeout=10)
            if res.status_code == 200 or (res.status_code == 400 and "message is not modified" in res.text.lower()):
                break
            elif res.status_code == 429:
                sleep_time = res.json().get("parameters", {}).get("retry_after", 3)
                time.sleep(sleep_time + 1)
            else:
                time.sleep(2)
        except requests.exceptions.RequestException as e:
            time.sleep(3 + attempt)

def process_ai_query(chat_id, user_id, first_name, text, message_id, thread_id, replied_text=None):
    text_lower = text.lower()
    ADMIN_IDS = [716496729, 5103843488, 6251430317]
    is_admin = user_id in ADMIN_IDS
    is_explicitly_summoned = "lixie" in text_lower
    is_asking_rank = "rank" in text_lower or "score" in text_lower
    doubt_keywords = [
        "what", "how", "when", "why", "where", "can you", "explain", 
        "meaning", "synonym", "antonym", "rank", "score", "cutoff", 
        "exam", "quiz", "quizzes", "poll", "polls", "test", "tests", 
        "today quiz", "link", "schedule", "pdf", "magazine"
    ]
    is_asking_doubt = "?" in text_lower or any(word in text_lower for word in doubt_keywords)

    if is_admin and not is_explicitly_summoned:
        return
    if replied_text and not is_explicitly_summoned:
        return
    if not is_admin and not is_explicitly_summoned and not is_asking_doubt:
        return

    # --- ✨ LIXIE CATCH-ALL RANK & RANKING INTERCEPT ✨ ---
    if "rank" in text_lower or "score" in text_lower or "points" in text_lower:
        process_ranking_command(chat_id, user_id, message_id, thread_id)
        return

    # ✨ SECURE REDIS RATE LIMITING (10s Cooldown)
    if redis_client:
        if check_and_set_cooldown("rate:lixie:main", 10):
            return
    else:
        # Fallback to local RAM if Redis is offline
        global LAST_AI_REPLY_TIME_MAIN
        current_time = time.time()
        if current_time - LAST_AI_REPLY_TIME_MAIN < 10:
            return
        LAST_AI_REPLY_TIME_MAIN = current_time

    current_ist_time = datetime.utcnow() + timedelta(hours=5, minutes=30)
    current_day = current_ist_time.strftime('%A')
    phase_of_week = "Active Competition"
    if current_day == "Monday" and (current_ist_time.hour < 16 or (current_ist_time.hour == 16 and current_ist_time.minute < 30)):
        phase_of_week = "Monday Pre-Game (Scores are reset to 0. The first quiz drops at 4:30 PM today.)"
    elif current_day == "Sunday" and current_ist_time.hour >= 13:
        phase_of_week = "Sunday Post-Deadline (Quizzes are over, waiting for the official Monday morning reset.)"

    total_quizzes_available = 0
    total_active_participants = 0
    exam_context = ""
    db_key_index = 0

    try:
        # 🟢 FIX: Define the database connection before executing!
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM polls")
        total_quizzes_available = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM users WHERE weekly_attempts > 0")
        total_active_participants = c.fetchone()[0]
        
        c.execute("SELECT name, exam_date, status, display_date FROM upcoming_exams")
        for row in c.fetchall():
            try:
                exam_date = datetime.strptime(row[1], "%Y-%m-%d")
                if exam_date.date() >= current_ist_time.date():
                    exam_context += f"- {row[0]}: Scheduled for {row[3]} ({row[2]})\n"
            except ValueError:
                continue

        c.execute("SELECT value FROM bot_settings WHERE key='current_key_index'")
        key_row = c.fetchone()
        db_key_index = int(key_row[0]) if key_row else 0
        
        c.close()
        release_db(conn)
    except Exception as db_err:
        pass

    reply_context = f"\n=== CONVERSATION HISTORY ===\nThe user is directly replying to this previous message:\n\"{replied_text}\"\nUse this to answer contextual questions.\n" if replied_text else ""

    # Dynamic 4:30 PM Topic Test Timetable Map
    DAILY_SCHEDULE_MAP = {
        "Monday": "• Set 1: Vocab Quiz (15Q • 10m)\n• Set 2: Error Detection (5Q • 5m)\n• Set 3: Fill in the Blanks (5Q • 5m)\n• Set 4: Sentence Improvement (5Q • 5m)\n• Set 5: Reading Comprehension (8Q • 10-12m)",
        "Tuesday": "• Set 1: Vocab Quiz (15Q • 10m)\n• Set 2: Word Usage (5Q • 5m)\n• Set 3: Error Detection (5Q • 5m)\n• Set 4: Fill in the Blanks (5Q • 5m)\n• Set 5: Para Jumbles (5 Sets • 10-12m)",
        "Wednesday": "• Set 1: Vocab Quiz (15Q • 10m)\n• Set 2: Sentence Improvement (5Q • 5m)\n• Set 3: Fill in the Blanks (5Q • 5m)\n• Set 4: Word Usage (5Q • 5m)\n• Set 5: Cloze Test (8Q • 10m)",
        "Thursday": "• Set 1: Vocab Quiz (15Q • 10m)\n• Set 2: Error Detection (5Q • 5m)\n• Set 3: Sentence Improvement (5Q • 5m)\n• Set 4: Fill in the Blanks (5Q • 5m)\n• Set 5: Reading Comprehension (8Q • 10-12m)",
        "Friday": "• Set 1: Vocab Quiz (15Q • 10m)\n• Set 2: Word Usage (5Q • 5m)\n• Set 3: Error Detection (5Q • 5m)\n• Set 4: Sentence Improvement (5Q • 5m)\n• Set 5: Para Jumbles (5 Sets • 10-12m)",
        "Saturday": "• Set 1: Vocab Quiz (15Q • 10m)\n• Set 2: Fill in the Blanks (5Q • 5m)\n• Set 3: Error Detection (5Q • 5m)\n• Set 4: Word Usage (5Q • 5m)\n• Set 5: Cloze Test (8Q • 10m)",
        "Sunday": "No quiz drops today. Weekly Cup locks at midnight!"
    }
    today_schedule = DAILY_SCHEDULE_MAP.get(current_day, "Standard Daily Sets")

    system_prompt = f"""
    You are Lixie, the official AI learning mentor and moderator of Ez Editorials — an exam-oriented English learning platform for Indian competitive exam aspirants (Banking, SSC, Regulatory, UPSC, State PSCs).

    =========================================
    PERSONA & MODERATOR RULES (STRICT BREVITY)
    =========================================
    - Role: Sharp community moderator + expert English tutor.
    - Tone: Friendly, grounded, intelligent, zero conversational fluff.
    - Format: Never start with robotic preamble (e.g., "Certainly!", "I'd be happy to help", "Here is a breakdown"). Jump directly into the answer.
    - Length Limits:
      * Quick query / single word meaning -> 1 to 3 concise lines max.
      * Grammar or concept doubts -> Short, clear breakdown (Rule -> Context -> Why the common trap fails). Max 100-140 words.
      * Platform or schedule questions -> 1 to 2 punchy lines.

    =========================================
    ENGLISH TEACHING PHILOSOPHY
    =========================================
    1. STRICT BRITISH ENGLISH: Always use British spelling and grammar conventions (e.g., analyse, colour, rigour, practise as verb).
    2. EXAM RELEVANCE: Focus strictly on real exam patterns (subject-verb agreement, prepositions, parallelism, contextual vocabulary, idioms).
    3. NO OPTION LABELS (A/B/C/D): Question options are randomized in the Mini App. Never say "Option A is correct." Always refer to the actual word or phrase.
    4. TEACH BY CONTRAST: Show why the right answer works and why the tempting distractor is grammatically flawed.

    =========================================
    ECOSYSTEM MAP & LIVE CONTEXT
    =========================================
    - Current Day: {current_day} | IST Time: {current_ist_time.strftime('%I:%M %p')}
    - Weekly Phase: {phase_of_week}
    - Active Challengers: {total_active_participants}

    [Today's 4:30 PM Test Schedule]
    {today_schedule}

    [Weekly Timetable Overview]
    • Mon & Thu: RC (1 Passage • 8Q)
    • Tue & Fri: Para Jumbles (5 Sets)
    • Wed & Sat: Cloze Test (1 Passage • 8Q)
    • Daily (Mon-Sat): Vocab Quiz (15Q) drops every day alongside changing grammar sets (Error Detection, Fillers, Sentence Improvement, Word Usage).
    • Sunday: No quiz drops. Leaderboard locks at midnight IST.

    [Community Rules & Threads]
    • 📰 Today's Editorials (Thread 3): Mon-Sat morning PDFs with attendance buttons. No Sunday issues.
    • 🏆 Rankings & Quizzes (Thread 2972): 4:30 PM drop notifications and Mini App links.
    • 📊 Cut-offs & Promotion: Scoring above the Class Average promotes a student; below causes demotion.
    • 🧠 Elo Rating: Lifetime rating (1000 base) tracking accuracy across difficulty tiers.
    • 🧹 Purge Rule: Students must read at least 4 Magazines OR complete 50 Quizzes every 30 days.
    • 📚 Grammar 101 / Word 101: Archived courses. Never promise new drops.
    • QUIZ & EDITORIAL ECOSYSTEM ROUTING:
        - Morning (10:00–11:59 AM): Daily Editorial PDFs drop in Thread 3 ("Today's Editorials Magazine"). Students must tap "Mark as Read".
        - Evening (4:30 PM IST): 5 Daily Topic Trial sets drop inside the Mini App (accessible via Thread 2972 or Bot Menu).
        - If a user asks where polls or quizzes are, reply concisely:
            "Daily quizzes have moved from Telegram polls to our interactive Mini App for timed test practice and solutions! Read your morning PDF in Thread 3, then tap below to attempt today's 4:30 PM trials."

    [Upcoming Exams]
    {exam_context}

    [User Interacting]
    - Name: {first_name} (Admin: {is_admin})
    {reply_context}

    =========================================
    BEHAVIOURAL GUARDRAILS
    =========================================
    1. DEFAULT ACTION IS SILENCE: If members are casually chatting, greeting, or debating amongst themselves without an English or platform doubt, output ONLY the single word: IGNORE
    2. THE ADMIN RULE: Ignore Admins completely unless they explicitly call your name ("Lixie").
    3. ANTI-HALLUCINATION: Never invent platform features, exam dates, or user stats. If a student asks for their personal rank or score, direct them to open the Mini App dashboard.
    """

    ai_reply = None
    # 2-Tier Fallback: gemini-3.5-flash-lite across 6 keys -> gemini-3.1-flash-lite across 6 keys
    for model_name in AI_MODELS:
        if ai_reply:
            break

        for attempt in range(len(API_KEYS)):
            try:
                active_key = API_KEYS[db_key_index]
                temp_client = genai.Client(api_key=active_key)
                response = temp_client.models.generate_content(
                    model=model_name,
                    contents=text,
                    config=types.GenerateContentConfig(system_instruction=system_prompt, temperature=0.4)
                )
                if response.text and response.text.strip():
                    ai_reply = response.text.strip()
                    break
            except Exception as e:
                error_str = str(e).lower()
                db_key_index = (db_key_index + 1) % len(API_KEYS)
                try:
                    conn = get_db()
                    c = conn.cursor()
                    c.execute("""
                        INSERT INTO bot_settings (key, value) VALUES ('current_key_index', %s)
                        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                    """, (str(db_key_index),))
                    conn.commit()
                    c.close()
                    release_db(conn)
                except Exception:
                    pass

                if "429" in error_str or "quota" in error_str or "exhausted" in error_str or "503" in error_str:
                    continue
                else:
                    continue

    if not ai_reply or ai_reply == "IGNORE" or ai_reply == '"IGNORE"':
        return

    send_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": ai_reply,
        "parse_mode": "Markdown",
        "reply_to_message_id": message_id
    }
    if thread_id:
        payload["message_thread_id"] = thread_id

    for attempt in range(10):
        try:
            res = http_session.post(send_url, json=payload, timeout=15)
            if res.status_code == 200: break
            elif res.status_code == 429: time.sleep(res.json().get("parameters", {}).get("retry_after", 3) + 1)
            else: time.sleep(2)
        except requests.exceptions.RequestException:
            time.sleep(3 + attempt)

def process_support_threads(chat_id, user_id, first_name, text, message_id, thread_id):
    # 1. Bounded Thread Memory Buffer
    global THREAD_HISTORY
    if thread_id not in THREAD_HISTORY:
        THREAD_HISTORY[thread_id] = deque(maxlen=8)
        
    THREAD_HISTORY[thread_id].append(f"User ({first_name}): {text}")
    recent_conversation = "\n".join(THREAD_HISTORY[thread_id])

    # 2. Redis Cooldown (10s lock)
    if redis_client:
        if check_and_set_cooldown(f"rate:lixie:thread:{thread_id}", 10):
            return
    else:
        global LAST_AI_REPLY_TIME_THREADS
        if thread_id not in LAST_AI_REPLY_TIME_THREADS: LAST_AI_REPLY_TIME_THREADS[thread_id] = 0
        current_time = time.time()
        if current_time - LAST_AI_REPLY_TIME_THREADS[thread_id] < 10:
            return
        LAST_AI_REPLY_TIME_THREADS[thread_id] = current_time

    # 3. Core Identity & Ultra-Brevity Rules
    LIXIE_CORE_BRAIN = """
    You are Lixie, the AI moderator of Ez Editorials.
    
    STRICT BREVITY RULES (CRITICAL):
    - Tone: Fast, grounded, diagnostic, smart community moderator.
    - Max Length: Strictly 1 to 2 sentences (Under 35 words).
    - No Corporate Preamble: Never say "Thank you for reaching out", "I understand your frustration", or "Certainly!".
    - British English only.
    - Anti-Hallucination: Never invent features, bug resolution times, or developer promises.
    """

    # 4. Thread-Specific Moderator Rules
    if thread_id == 12082:
        THREAD_MODE = """
        [THREAD: 🐞 BUG REPORTS]
        Goal: Diagnose and log issues in 1 sentence.
        - If crucial info is missing, ask directly (e.g. "Which day/set and question number did this happen on?").
        - If clear report: "Logged! The development team will investigate this."
        - If user is providing follow-up details to an already acknowledged bug, output: IGNORE
        """
    elif thread_id == 12103:
        THREAD_MODE = """
        [THREAD: 🛟 HELP & SUPPORT]
        Goal: Resolve user navigation or Mini App access issues in 1-2 direct lines.
        - Give exact button/tab name (e.g. "Open the Mini App via 🏆 Rankings & Quizzes thread and check the Progress tab.").
        - If user says "Thanks", "Got it", or solves it themselves: IGNORE
        """
    elif thread_id == 12105:
        THREAD_MODE = """
        [THREAD: 💡 FEATURE REQUESTS]
        Goal: Acknowledge community suggestions in exactly 1 line.
        - Output: "Noted! We'll keep this idea in mind for future app updates."
        - Never promise timelines, roadmaps, or guarantee implementation.
        """
    else:
        THREAD_MODE = ""

    # 5. Silence Engine
    CONVERSATION_AWARENESS = f"""
    [IGNORE DIRECTIVE]
    If this message is casual chatter, user-to-user conversation, a simple acknowledgement, or does not require moderation, your ONLY output MUST be the single word:
    IGNORE

    [RECENT CONVERSATION HISTORY]
    {recent_conversation}
    """

    system_prompt = LIXIE_CORE_BRAIN + THREAD_MODE + CONVERSATION_AWARENESS

    # 6. Execute Gemini Request with Model & Key Fallback
    global current_key_index
    ai_reply = None

    for model_name in AI_MODELS:
        if ai_reply:
            break

        for attempt in range(len(API_KEYS)):
            try:
                active_key = API_KEYS[current_key_index]
                temp_client = genai.Client(api_key=active_key)
                response = temp_client.models.generate_content(
                    model=model_name,
                    contents=text,
                    config=types.GenerateContentConfig(system_instruction=system_prompt, temperature=0.2)
                )
                if response.text and response.text.strip():
                    ai_reply = response.text.strip()
                    break
            except Exception:
                current_key_index = (current_key_index + 1) % len(API_KEYS)
                continue

    # 7. Check for IGNORE
    if not ai_reply or ai_reply.upper() == "IGNORE" or ai_reply == '"IGNORE"':
        return

    # 8. Record in Memory & Dispatch
    THREAD_HISTORY[thread_id].append(f"Lixie: {ai_reply}")

    send_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": ai_reply,
        "parse_mode": "Markdown",
        "reply_to_message_id": message_id,
        "message_thread_id": thread_id
    }

    for attempt in range(5):
        try:
            res = http_session.post(send_url, json=payload, timeout=10)
            if res.status_code == 200: break
            elif res.status_code == 429: time.sleep(res.json().get("parameters", {}).get("retry_after", 3) + 1)
            else: time.sleep(2)
        except requests.exceptions.RequestException:
            time.sleep(3 + attempt)

def process_read_receipt(cb_id, user_id, first_name, message_id):
    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        
        # 1. Double-Tap Protection
        c.execute("SELECT 1 FROM read_receipts WHERE message_id=%s AND user_id=%s", (message_id, user_id))
        if c.fetchone():
            http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery", json={
                "callback_query_id": cb_id, "text": "You've already marked this as read! 📖", "show_alert": False
            })
            return
            
        # 2. Record the tap
        c.execute("INSERT INTO read_receipts (message_id, user_id) VALUES (%s, %s)", (message_id, user_id))
        
        # 3. Secure their 30-Day Protection!
        current_time = time.time()
        c.execute("""
            INSERT INTO users (user_id, first_name, last_updated) VALUES (%s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET 
                first_name = EXCLUDED.first_name,
                last_updated = EXCLUDED.last_updated
        """, (user_id, first_name, current_time))
        
        # 4. Count total reads to update the UI
        c.execute("SELECT COUNT(*) FROM read_receipts WHERE message_id=%s", (message_id,))
        total_reads = c.fetchone()[0]
        conn.commit()
        
        # 5. Live-Update the Button (Updates to green check & preserves 2nd button)
        read_label = f"✅ Marked as Read • {total_reads}" if total_reads > 0 else "📖 Mark as Read • 0"
        markup = {
            "inline_keyboard": [
                [{"text": read_label, "callback_data": f"read_{message_id}"}],
                [{"text": "⚡️ Daily Topic Trials", "url": "https://t.me/Ez_vocab_bot/leaderboard"}]
            ]
        }
        http_session.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageReplyMarkup",
            json={"chat_id": CHAT_ID, "message_id": message_id, "reply_markup": markup}
        )
        
        # 6. Inform the user they are safe
        http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery", json={
            "callback_query_id": cb_id, "text": "Attendance marked! You are protected from the inactivity purge. 🛡️", "show_alert": False
        })
    except Exception as e:
        print(f"Error processing read receipt: {e}")
    finally:
        if conn:
            try: c.close()
            except: pass
            release_db(conn)

@app.route(f'/{TELEGRAM_TOKEN}', methods=['POST'])
def webhook():
    update = request.get_json()
    if not update:
        return 'OK', 200

    # ✨ FAST EXIT: Ignore junk updates instantly to save CPU
    if not any(k in update for k in ['callback_query', 'chat_join_request', 'poll_answer', 'edited_message', 'message']):
        return 'OK', 200

    if 'callback_query' in update:
        cbq = update['callback_query']
        cb_data = cbq.get('data', '')
        cb_id = cbq['id']
        user_info = cbq['from']
        
        if cb_data == 'check_status':
            http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery", json={
                "callback_query_id": cb_id, "text": "Verifying status...", "show_alert": False
            })
            threading.Thread(target=handle_private_bot_start, args=(user_info['id'], user_info.get('first_name', 'Student'))).start()
            return 'OK', 200

        if cb_data.startswith('read_'):
            target_msg_id = int(cb_data.split('_')[1])
            user_id = user_info['id']
            first_name = user_info.get('first_name', '').strip()
            
            # Fire in background for instant response
            threading.Thread(target=process_read_receipt, args=(cb_id, user_id, first_name, target_msg_id)).start()
            return 'OK', 200

    if 'chat_join_request' in update:
        join_req = update['chat_join_request']
        query_id = join_req.get('query_id')
        user_id = join_req['from']['id']
        
        # ✨ Use Telegram's secret join request bypass ID
        user_chat_id = join_req.get('user_chat_id', user_id)
        
        # 🟢 UPDATED: Pointing to the new Cloudflare Pages deployment!
        MINI_APP_URL = "https://ez-editorials-app.pages.dev/captcha.html"
        markup = {"inline_keyboard": [[{"text": "⚡️ Complete Entrance Trial (10Q)", "web_app": {"url": MINI_APP_URL}}]]}
        
        if query_id:
            # ✨ 1. NATIVE POP-UP
            http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendChatJoinRequestWebApp", json={
                "chat_join_request_query_id": query_id,
                "web_app_url": MINI_APP_URL
            })
            
            # 🛡️ 2. BACKUP DM (Using user_chat_id to bypass the /start requirement)
            http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={
                "chat_id": user_chat_id,
                "text": "👋 **Welcome to Ez Editorials!**\n\nWe have received your request to join the Great Hall.\n\nTo ensure our community remains a high-quality environment for serious learners, we ask all new members to complete a quick, 10-question English Entrance Trial.\n\nTap the button below to prove your skills and instantly gain access to the group! 🪄",
                "reply_markup": markup,
                "parse_mode": "Markdown"
            })
        else:
            # 🔄 3. FALLBACK DM
            http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={
                "chat_id": user_chat_id,
                "text": "👋 **Welcome to Ez Editorials!**\n\nWe have received your request to join the Great Hall.\n\nTo ensure our community remains a high-quality environment for serious learners, we ask all new members to complete a quick, 10-question English Entrance Trial.\n\nTap the button below to prove your skills and instantly gain access to the group! 🪄",
                "reply_markup": markup,
                "parse_mode": "Markdown"
            })
            
        # ✨ THE TIME BOMB: Store in DB for the 1-minute cron job to sweep!
        # 🟢 FIX: Decreased to 300 seconds (5 mins)
        expire_time = int(time.time()) + 300 
        try:
            conn = get_db()
            c = conn.cursor()
            c.execute("""
                INSERT INTO bot_settings (key, value) VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """, (f"join_req_{user_id}", str(expire_time)))
            conn.commit()
            c.close()
            release_db(conn)
        except Exception as e:
            print(f"Failed to set join timer: {e}")
            
        return 'OK', 200

    if 'poll_answer' in update:
        ans = update['poll_answer']
        user_info = ans['user']
        f_name = user_info.get('first_name', '').strip()
        l_name = user_info.get('last_name', '').strip()
        formatted_name = f"{f_name} {l_name[0]}".strip() if l_name else f_name

        # ✨ REDIS BURST QUEUE: Push instantly to RAM, 0ms delay!
        if redis_client:
            try:
                payload = json.dumps({
                    "user_id": user_info['id'],
                    "first_name": formatted_name,
                    "poll_id": ans['poll_id'],
                    "chosen_option": ans['option_ids'][0]
                })
                redis_client.rpush("queue:poll_answers", payload)
                return 'OK', 200
            except Exception as e:
                print(f"Redis Queue Error: {e}") # Safe fail-over to DB below

        # 🛡️ FAIL-OPEN FALLBACK: Save to Supabase if Redis is dead
        conn = None
        try:
            conn = get_db()
            c = conn.cursor()
            c.execute("INSERT INTO answer_queue (user_id, first_name, poll_id, chosen_option) VALUES (%s, %s, %s, %s)",
                      (user_info['id'], formatted_name, ans['poll_id'], ans['option_ids'][0]))
            conn.commit()
            c.close()
            return 'OK', 200
        except Exception as e:
            return 'DB Locked, Retrying', 500
        finally:
            if conn:
                release_db(conn)

    elif 'edited_message' in update:
        msg = update['edited_message']
        chat_id = msg['chat']['id']

        if str(chat_id) == SOURCE_CHAT_ID:
            conn = None
            try:
                conn = get_db()
                c = conn.cursor()
                c.execute("SELECT target_msg_id FROM message_links WHERE source_msg_id = %s", (msg['message_id'],))
                row = c.fetchone()
                c.close()
                if row: sync_message_edit(msg=msg, target_msg_id=row[0])
            except Exception as e:
                pass
            finally:
                if conn:
                    release_db(conn)
        return 'OK', 200

    elif 'message' in update:
        msg = update['message']
        chat_id = msg['chat']['id']
        thread_id = msg.get('message_thread_id')
        chat_type = msg['chat'].get('type')

        if str(chat_id) == SOURCE_CHAT_ID and thread_id == CHANNEL_SOURCE_THREAD:
            threading.Thread(target=relay_to_channel_with_buttons, args=(msg['message_id'],)).start()
            return 'OK', 200

        if str(chat_id) == SOURCE_CHAT_ID and thread_id in THREAD_MAPPING:
            target_thread_id = THREAD_MAPPING[thread_id]
            relay_message(message_id=msg['message_id'], target_thread_id=target_thread_id)
            return 'OK', 200

        if chat_type == 'private':
            user_id = msg['from']['id']
            first_name = msg['from'].get('first_name', 'Student')
            threading.Thread(target=handle_private_bot_start, args=(user_id, first_name)).start()
            return 'OK', 200

        if 'text' in msg:
            text = msg.get('text', '')
            
            # --- INTERCEPT: EPHEMERAL /RANK COMMAND ---
            # 🧊 ON ICE: Temporarily disabled to drive Mini App adoption & save CPU
            # if chat_type in ['group', 'supergroup'] and text.startswith('/rank'):
            #     threading.Thread(target=process_ranking_command, kwargs={
            #         "chat_id": chat_id, 
            #         "user_id": msg['from']['id'], 
            #         "message_id": msg['message_id'], 
            #         "thread_id": thread_id
            #     }).start()
            #     return 'OK', 200

            # --- EXISTING LIXIE AI LOGIC ---
            if chat_type in ['group', 'supergroup'] and not text.startswith('/'):
                if str(chat_id) == CHAT_ID:
                    
                    # 🟢 ROUTE 1: Main Community Chat (Thread 11)
                    if thread_id == 11:
                        replied_text = msg['reply_to_message']['text'] if 'reply_to_message' in msg and 'text' in msg['reply_to_message'] else None
                        threading.Thread(target=process_ai_query, kwargs={
                            "chat_id": chat_id, "user_id": msg['from']['id'], "first_name": msg['from']['first_name'],
                            "text": text, "message_id": msg['message_id'], "thread_id": thread_id, "replied_text": replied_text
                        }).start()
                        
                    # 🟢 ROUTE 2: The New Multi-Thread Support Engine
                    elif thread_id in [12082, 12103, 12105]:
                        threading.Thread(target=process_support_threads, kwargs={
                            "chat_id": chat_id, "user_id": msg['from']['id'], "first_name": msg['from']['first_name'],
                            "text": text, "message_id": msg['message_id'], "thread_id": thread_id
                        }).start()

    return 'OK', 200

def run_midnight_purge_background():
    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        
        # 1. Define time windows
        current_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
        now_ts = time.time()
        seven_days_ago_ts = now_ts - (7 * 24 * 60 * 60)
        thirty_days_ago_ts = now_ts - (30 * 24 * 60 * 60)
        thirty_days_ago_date = (current_ist - timedelta(days=30)).strftime('%Y-%m-%d')
        
        admin_ids = [716496729, 6251430317, 5103843488]
        
        # 2. Fetch all users older than 7 days with their 30-day activity counts
        c.execute("""
            SELECT 
                u.user_id, 
                u.first_name,
                COALESCE(SUM(dh.attempts), 0) + COALESCE(u.daily_attempts, 0) AS total_quizzes,
                COALESCE(rr.read_count, 0) AS total_reads,
                EXTRACT(EPOCH FROM u.joined_at) AS joined_ts
            FROM users u
            LEFT JOIN daily_history dh ON u.user_id = dh.user_id AND dh.date_str >= %s
            LEFT JOIN (
                SELECT user_id, COUNT(*) as read_count 
                FROM read_receipts 
                WHERE created_at >= to_timestamp(%s) 
                GROUP BY user_id
            ) rr ON u.user_id = rr.user_id
            WHERE u.joined_at IS NULL OR u.joined_at <= to_timestamp(%s)
            GROUP BY u.user_id, u.first_name, u.daily_attempts, u.joined_at, rr.read_count
        """, (thirty_days_ago_date, thirty_days_ago_ts, seven_days_ago_ts))
        
        all_users = c.fetchall()
        
        probation_purged = 0
        regular_purged = 0
        
        if all_users:
            for u in all_users:
                uid = u[0]
                first_name = u[1]
                total_quizzes = u[2]
                total_reads = u[3]
                joined_ts = u[4] if u[4] is not None else 0
                
                if uid in admin_ids:
                    continue  # Never purge admins
                
                # Check whether user is in 7-30 day probation or standard 30+ day cohort
                is_probation = (joined_ts > thirty_days_ago_ts)
                should_purge = False
                
                if is_probation:
                    # 7-Day Surveillance: Must have at least 1 Magazine Read OR 1 Quiz
                    if total_reads < 1 and total_quizzes < 1:
                        should_purge = True
                else:
                    # Standard 30-Day Rule: Must have at least 4 Magazine Reads OR 50 Quizzes
                    if total_reads < 4 and total_quizzes < 50:
                        should_purge = True
                
                if should_purge:
                    # Daily safety cap
                    if (probation_purged + regular_purged) >= 100:
                        break
                    
                    # 1. Soft-ban to remove from group
                    res_ban = http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/banChatMember", json={
                        "chat_id": CHAT_ID, "user_id": uid
                    }, timeout=5)
                    
                    if res_ban.status_code == 200 and res_ban.json().get('ok'):
                        # 2. Unban so they can rejoin later via trial if they wish
                        for _ in range(5):
                            res_unban = http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/unbanChatMember", json={
                                "chat_id": CHAT_ID, "user_id": uid, "only_if_banned": True
                            }, timeout=5)
                            if res_unban.status_code == 200:
                                break
                            time.sleep(1)
                        
                        # 3. Remove user record from DB
                        c.execute("DELETE FROM users WHERE user_id = %s", (uid,))
                        conn.commit()
                        
                        if is_probation:
                            probation_purged += 1
                        else:
                            regular_purged += 1
                            
                    time.sleep(2)
        
        c.close()
        
        # Admin debrief report
        notify_prathu(
            f"🧹 **Midnight Purge Complete!**\n\n"
            f"🔍 **7-Day Probation Purged:** {probation_purged} (0 reads & 0 quizzes)\n"
            f"🚪 **30-Day Inactive Purged:** {regular_purged} (< 4 reads & < 50 quizzes)\n"
            f"👥 **Total Removed:** {probation_purged + regular_purged}"
        )
        
    except Exception as e:
        notify_prathu(f"🚨 **Purge Error:**\n`{e}`")
    finally:
        if conn:
            release_db(conn)
            
# 🟢 DAILY PURGE TRIGGER (Midnight IST)
@app.route('/cron/daily_purge_0508', methods=['GET', 'POST'])
def trigger_daily_purge():
    threading.Thread(target=run_midnight_purge_background).start()
    return "Midnight purge sequence initiated! Admin will receive a report.", 200

def run_daily_reset_background():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT user_id, first_name, daily_score, daily_attempts, faction FROM users WHERE daily_attempts > 0")
    daily_players = c.fetchall()

    if not daily_players:
        c.close()
        release_db(conn)
        return

    yesterday_string = (datetime.utcnow() + timedelta(hours=5, minutes=30) - timedelta(days=1)).strftime('%Y-%m-%d')
    for user in daily_players:
        c.execute("INSERT INTO daily_history (user_id, date_str, score, attempts) VALUES (%s, %s, %s, %s)",
                  (user[0], yesterday_string, user[2], user[3]))

    c.execute("UPDATE users SET daily_score = 0, daily_attempts = 0")
    conn.commit()
    c.close()
    release_db(conn)
    notify_prathu("✅ **Daily Scores Reset** executed successfully!")

@app.route('/reset_daily/0508', methods=['GET', 'POST'])
def trigger_daily_reset():
    threading.Thread(target=run_daily_reset_background).start()
    return "Daily reset triggered!", 200

def run_weekly_reset_background():
    # ✨ NEW: The Live Status Tracker
    reset_status = {
        "Database_Reset": "🔴 Failed",
        "Admin_Debrief": "🔴 Failed",
        "Elo_Bleed_DM": "🔴 Failed"
    }
    
    conn = None
    try:
        conn = get_db()
        c = conn.cursor()

        c.execute("""
            SELECT user_id, first_name, weekly_score, weekly_attempts, faction, league_tier, weekly_correct
            FROM users WHERE weekly_attempts > 0 ORDER BY weekly_score DESC
        """)
        all_weekly_players = c.fetchall()

        if not all_weekly_players:
            c.execute("INSERT INTO bot_settings (key, value) VALUES ('current_week', '14') ON CONFLICT (key) DO NOTHING")
            c.execute("SELECT value FROM bot_settings WHERE key='current_week'")
            if week_row := c.fetchone():
                c.execute("UPDATE bot_settings SET value=%s WHERE key='current_week'", (str(int(week_row[0]) + 1),))
            c.execute("UPDATE users SET weekly_score = 0, weekly_attempts = 0")
            conn.commit()
            c.close()
            release_db(conn)
            return

        # ✨ FIX: Synchronized accuracy-weighted math to match the live Mini App
        sum_weighted_points = 0.0
        sum_weights = 0.0
        for row in all_weekly_players:
            score = row[2]
            attempts = row[3]
            correct = row[6] if (len(row) > 6 and row[6] is not None) else 0
            
            if attempts > 0:
                accuracy = correct / attempts
                volume_weight = attempts / (attempts + 10.0)
                final_weight = volume_weight * accuracy
                
                # Ignore negative scores in the weight pool
                if score < 0:
                    final_weight = 0.0
                    
                sum_weighted_points += (score * final_weight)
                sum_weights += final_weight
                
        target_average = int((sum_weighted_points / sum_weights) + 0.5) if sum_weights > 0 else 0

        for rank_index, row in enumerate(all_weekly_players):
            uid = row[0]
            score = row[2]
            current_league = row[5] if row[5] is not None else 0

            if score >= target_average:
                if current_league >= 15: 
                    # 🛡️ THE GENESIS GRIND: Once at Omniscient (15) or higher, strictly +1 promotion per week.
                    # Max ceiling is now 25 (Genesis X).
                    new_league = min(25, current_league + 1)
                else:
                    # Standard promotion for Ascendant (14) and below
                    if rank_index == 0: new_league = current_league + 3
                    elif rank_index < 10: new_league = current_league + 2
                    else: new_league = current_league + 1
                    
                    # 🛡️ THE OMNISCIENT GATE: Cannot skip past Omniscient (15) in a single jump.
                    if new_league > 15:
                        new_league = 15
            else:
                new_league = max(0, current_league - 1)
            c.execute("UPDATE users SET league_tier = %s WHERE user_id = %s", (new_league, uid))

        try:
            c.execute("SELECT value FROM bot_settings WHERE key='current_week'")
            week_row = c.fetchone()
            current_week_num = int(week_row[0]) if week_row else 14
            total_players_this_week = len(all_weekly_players)

            for rank_index, user in enumerate(all_weekly_players):
                uid, u_score, u_attempts = user[0], user[2], user[3]
                u_correct = user[6] if (len(user) > 6 and user[6] is not None) else 0
                c.execute("""
                    INSERT INTO weekly_rank_history (user_id, week_num, rank, total_members, score, attempts, correct)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (user_id, week_num) DO UPDATE SET
                        rank = EXCLUDED.rank, total_members = EXCLUDED.total_members,
                        score = EXCLUDED.score, attempts = EXCLUDED.attempts, correct = EXCLUDED.correct
                """, (uid, current_week_num, rank_index + 1, total_players_this_week, u_score, u_attempts, u_correct))
            # ✨ Removed the premature conn.commit() so it waits for the final lock!
        except Exception as e: print(e)

        c.execute("INSERT INTO bot_settings (key, value) VALUES ('current_week', '14') ON CONFLICT DO NOTHING")
        c.execute("SELECT value FROM bot_settings WHERE key='current_week'")
        if week_row := c.fetchone():
            c.execute("UPDATE bot_settings SET value=%s WHERE key='current_week'", (str(int(week_row[0]) + 1),))

        # --- THE ADMIN MASTERY FUNNEL DEBRIEF ---
        try:
            c.execute("SELECT COUNT(*) FROM polls")
            total_quizzes = c.fetchone()[0]

            total_active_students = len(all_weekly_players)
            
            # 1. New vs Returning Tracking
            c.execute("SELECT DISTINCT user_id FROM weekly_rank_history WHERE week_num < %s", (current_week_num,))
            past_users = set(row[0] for row in c.fetchall())
            new_challengers = sum(1 for u in all_weekly_players if u[0] not in past_users)
            returning_challengers = total_active_students - new_challengers

            # 2. General Metrics
            total_weekly_attempts = sum(u[3] for u in all_weekly_players)
            avg_attempts_per_student = round(total_weekly_attempts / total_active_students, 1) if total_active_students > 0 else 0
            completion_count = sum(1 for u in all_weekly_players if u[3] >= total_quizzes)
            completion_rate = round((completion_count / total_active_students) * 100) if total_active_students > 0 else 0

            total_weekly_correct = sum(u[6] if (len(u) > 6 and u[6] is not None) else 0 for u in all_weekly_players)
            overall_accuracy = round((total_weekly_correct / total_weekly_attempts) * 100) if total_weekly_attempts > 0 else 0
            
            engagement_rating = "HIGH" if completion_rate >= 70 else "MODERATE" if completion_rate >= 40 else "NEEDS ATTENTION"

            promoted_count = sum(1 for u in all_weekly_players if u[2] >= target_average)
            demoted_count = total_active_students - promoted_count
            promotion_rate = round((promoted_count / total_active_students) * 100) if total_active_students > 0 else 0

            # 3. Elo Gain/Loss Calculation
            # ⚡ OPTIMIZED: Only pull users who actually competed this week
            c.execute("SELECT first_name, live_elo, base_elo FROM users WHERE live_elo IS NOT NULL AND weekly_attempts > 0")
            elo_users = c.fetchall()
            highest_elo_val = 1000
            biggest_gain = 0
            biggest_loss = 0
            if elo_users:
                highest_elo_val = max((row[1] for row in elo_users if row[1] is not None), default=1000)
                diffs = [(row[1] - (row[2] or 1000)) for row in elo_users if row[1] is not None]
                if diffs:
                    biggest_gain = max(diffs)
                    biggest_loss = min(diffs)
                    if biggest_gain < 0: biggest_gain = 0
                    if biggest_loss > 0: biggest_loss = 0

            # 4. Deep Poll Analytics
            c.execute("SELECT poll_id, correct_index FROM polls")
            poll_metadata = {row[0]: row[1] for row in c.fetchall()}

            c.execute("""
                SELECT poll_id, COUNT(*) as attempts, SUM(is_correct) as correct
                FROM user_answers GROUP BY poll_id HAVING COUNT(*) > 0
            """)
            poll_stats = c.fetchall()

            t1 = t2 = t3 = t4 = t5 = 0
            lowest_acc, highest_acc = 101, -1
            hardest_poll_id = easiest_poll_id = None
            tier_map = {"T1": [], "T2": [], "T3": [], "T4": [], "T5": []}
            total_q_elo = 0
            
            tier_acc = {"T1": [0,0], "T2": [0,0], "T3": [0,0], "T4": [0,0], "T5": [0,0]}

            for p_id, p_att, p_cor in poll_stats:
                p_cor = p_cor if p_cor else 0
                p_acc = (p_cor / p_att) * 100

                if p_acc >= 86: 
                    t1 += 1; tier_map["T1"].append(p_id); total_q_elo += 800
                    tier_acc["T1"][0] += p_cor; tier_acc["T1"][1] += p_att
                elif p_acc >= 72: 
                    t2 += 1; tier_map["T2"].append(p_id); total_q_elo += 1000
                    tier_acc["T2"][0] += p_cor; tier_acc["T2"][1] += p_att
                elif p_acc >= 58: 
                    t3 += 1; tier_map["T3"].append(p_id); total_q_elo += 1200
                    tier_acc["T3"][0] += p_cor; tier_acc["T3"][1] += p_att
                elif p_acc >= 44: 
                    t4 += 1; tier_map["T4"].append(p_id); total_q_elo += 1500
                    tier_acc["T4"][0] += p_cor; tier_acc["T4"][1] += p_att
                else: 
                    t5 += 1; tier_map["T5"].append(p_id); total_q_elo += 1800
                    tier_acc["T5"][0] += p_cor; tier_acc["T5"][1] += p_att

                if p_acc < lowest_acc: lowest_acc = p_acc; hardest_poll_id = p_id
                if p_acc > highest_acc: highest_acc = p_acc; easiest_poll_id = p_id

            avg_q_elo = (total_q_elo / len(poll_stats)) if len(poll_stats) > 0 else 1000
            week_diff_score = min(10.0, max(1.0, ((avg_q_elo - 800) / 1000) * 9.0 + 1.0))
            diff_label = "Easy" if week_diff_score < 4 else "Moderate" if week_diff_score < 7 else "Brutal"

            lowest_acc = round(lowest_acc) if lowest_acc != 101 else 0
            highest_acc = round(highest_acc) if highest_acc != -1 else 0

            trap_pct, trap_opt = 0, "N/A"
            if hardest_poll_id:
                c.execute("""
                    SELECT chosen_option, COUNT(*) as count FROM user_answers
                    WHERE poll_id = %s AND is_correct = 0 GROUP BY chosen_option ORDER BY count DESC LIMIT 1
                """, (hardest_poll_id,))
                trap_row = c.fetchone()
                if trap_row:
                    c.execute("SELECT COUNT(*) FROM user_answers WHERE poll_id = %s", (hardest_poll_id,))
                    total_att_hard = c.fetchone()[0]
                    trap_pct = round((trap_row[1] / total_att_hard) * 100) if total_att_hard > 0 else 0
                    opt_map = {0: "Option A", 1: "Option B", 2: "Option C", 3: "Option D"}
                    trap_opt = opt_map.get(trap_row[0], f"Option Index {trap_row[0]}")

            # 5. Mastery & Knowledge Index
            c.execute("SELECT user_id, poll_id FROM user_answers WHERE is_correct = 1")
            user_correct_dict = {}
            for uid, pid in c.fetchall():
                if uid not in user_correct_dict: user_correct_dict[uid] = set()
                user_correct_dict[uid].add(pid)

            masters = {"T1": 0, "T2": 0, "T3": 0, "T4": 0, "T5": 0}
            for uid, correct_set in user_correct_dict.items():
                if len(tier_map["T1"]) > 0 and all(pid in correct_set for pid in tier_map["T1"]): masters["T1"] += 1
                if len(tier_map["T2"]) > 0 and all(pid in correct_set for pid in tier_map["T2"]): masters["T2"] += 1
                if len(tier_map["T3"]) > 0 and all(pid in correct_set for pid in tier_map["T3"]): masters["T3"] += 1
                if len(tier_map["T4"]) > 0 and all(pid in correct_set for pid in tier_map["T4"]): masters["T4"] += 1
                if len(tier_map["T5"]) > 0 and all(pid in correct_set for pid in tier_map["T5"]): masters["T5"] += 1

            ki_easy = round((tier_acc["T1"][0]+tier_acc["T2"][0]) / (tier_acc["T1"][1]+tier_acc["T2"][1]) * 100) if (tier_acc["T1"][1]+tier_acc["T2"][1]) > 0 else 0
            ki_med = round(tier_acc["T3"][0] / tier_acc["T3"][1] * 100) if tier_acc["T3"][1] > 0 else 0
            ki_hard = round(tier_acc["T4"][0] / tier_acc["T4"][1] * 100) if tier_acc["T4"][1] > 0 else 0
            ki_boss = round(tier_acc["T5"][0] / tier_acc["T5"][1] * 100) if tier_acc["T5"][1] > 0 else 0

            # 6. Week-on-Week Tracking (WoW)
            c.execute("SELECT value FROM bot_settings WHERE key='wow_stats'")
            wow_row = c.fetchone()
            wow_text = ""
            if wow_row and wow_row[0]:
                try:
                    last_stats = json.loads(wow_row[0])
                    def get_change(old, new):
                        if old == 0: return f"▲ +100%" if new > 0 else "0%"
                        change = ((new - old) / old) * 100
                        return f"▲ +{change:.1f}%" if change > 0 else f"▼ {change:.1f}%"
                    
                    wow_text = (
                        f"• Active Challengers: {get_change(last_stats.get('active', 0), total_active_students)}\n"
                        f"• Total Attempts: {get_change(last_stats.get('attempts', 0), total_weekly_attempts)}\n"
                        f"• Completion Rate: {get_change(last_stats.get('completion', 0), completion_rate)}\n"
                        f"• Overall Accuracy: {get_change(last_stats.get('accuracy', 0), overall_accuracy)}\n"
                        f"• Promotion Cut-off: {get_change(last_stats.get('cutoff', 0), target_average)}\n"
                        f"• Highest Elo: {get_change(last_stats.get('highest_elo', 1000), highest_elo_val)}\n"
                    )
                except: wow_text = "Data formatting error.\n"
            else: wow_text = "No previous data for comparison.\n"

            current_stats = {
                "active": total_active_students, "attempts": total_weekly_attempts,
                "completion": completion_rate, "accuracy": overall_accuracy,
                "cutoff": target_average, "highest_elo": highest_elo_val
            }
            c.execute("INSERT INTO bot_settings (key, value) VALUES ('wow_stats', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (json.dumps(current_stats),))

            current_ist_time = datetime.utcnow() + timedelta(hours=5, minutes=30)
            end_date = current_ist_time - timedelta(days=1)
            start_date = current_ist_time - timedelta(days=7)
            date_range = f"{start_date.strftime('%d %B')} – {end_date.strftime('%d %B %Y')}"

            # 8. Assemble Streamlined Admin Message
            admin_msg = (
                f"🔐 **ADMIN DEBRIEF: WEEKLY CUP SEASON {current_week_num}**\n"
                f"📅 {date_range}\n\n"
                f"═══════════════════════════════\n"
                f"👥 **1. COMMUNITY HEALTH**\n"
                f"═══════════════════════════════\n\n"
                f"• Active Challengers: {total_active_students}\n"
                f"• New Challengers: {new_challengers}\n"
                f"• Returning Challengers: {returning_challengers}\n\n"
                f"• Total Quiz Attempts: {total_weekly_attempts:,}\n"
                f"• Average Attempts per Student: {avg_attempts_per_student}\n"
                f"• Completion Rate: {completion_rate}%\n\n"
                f"• Overall Accuracy: {overall_accuracy}%\n"
                f"• Engagement Rating: {engagement_rating}\n\n"
                f"═══════════════════════════════\n"
                f"📊 **2. QUIZ ANALYTICS**\n"
                f"═══════════════════════════════\n\n"
                f"• Quizzes Dropped: {total_quizzes}\n"
                f"• Overall Difficulty: {diff_label} ({week_diff_score:.1f}/10)\n\n"
                f"Difficulty Distribution\n"
                f"• Tier 1 (Very Easy): {t1}\n"
                f"• Tier 2 (Easy): {t2}\n"
                f"• Tier 3 (Medium): {t3}\n"
                f"• Tier 4 (Hard): {t4}\n"
                f"• Tier 5 (Boss): {t5}\n\n"
                f"Hardest Question\n"
                f"↳ {lowest_acc}% answered correctly.\n\n"
                f"Easiest Question\n"
                f"↳ {highest_acc}% answered correctly.\n\n"
                f"Most Common Trap\n"
                f"↳ {trap_opt} selected by {trap_pct}% of incorrect attempts.\n\n"
                f"═══════════════════════════════\n"
                f"🏆 **3. COMPETITION HEALTH**\n"
                f"═══════════════════════════════\n\n"
                f"• Promotion Cut-off: {int(target_average)} pts\n\n"
                f"• Promotions: {promoted_count}\n"
                f"• Demotions: {demoted_count}\n\n"
                f"• Promotion Rate: {promotion_rate}%\n\n"
                f"═══════════════════════════════\n"
                f"⚔️ **4. LEAGUE & ELO**\n"
                f"═══════════════════════════════\n\n"
                f"• Highest Elo: {highest_elo_val:.1f}\n"
                f"• Biggest Elo Gain: +{biggest_gain:.1f}\n"
                f"• Biggest Elo Loss: {biggest_loss:.1f}\n\n"
                f"═══════════════════════════════\n"
                f"🎯 **5. LEARNING INSIGHTS**\n"
                f"═══════════════════════════════\n\n"
                f"Perfect Accuracy\n\n"
                f"• Tier 1 Masters: {masters['T1']}\n"
                f"• Tier 2 Masters: {masters['T2']}\n"
                f"• Tier 3 Masters: {masters['T3']}\n"
                f"• Tier 4 Masters: {masters['T4']}\n"
                f"• Tier 5 Boss Slayers: {masters['T5']}\n\n"
                f"Knowledge Index\n\n"
                f"• Easy Questions: {ki_easy}%\n"
                f"• Medium Questions: {ki_med}%\n"
                f"• Hard Questions: {ki_hard}%\n"
                f"• Boss Questions: {ki_boss}%\n\n"
                f"═══════════════════════════════\n"
                f"📈 **6. WEEK-ON-WEEK CHANGE**\n"
                f"═══════════════════════════════\n\n"
                f"Compared with Season {current_week_num - 1}\n\n"
                f"{wow_text}"
            )

            admin_ids = [716496729, 6251430317, 5103843488]
            for a_id in admin_ids:
                try: 
                    http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": a_id, "text": admin_msg, "parse_mode": "Markdown"}, timeout=5)
                    reset_status["Admin_Debrief"] = "🟢 Success"
                except: pass

        except Exception as e: notify_prathu(f"🚨 **ERROR (Weekly Debrief):** Failed to compile or send the Admin Debrief!\n`{e}`")

        # ==========================================
        # 🩸 PRIVATE DM: ELO BLEED LEADERBOARD
        # ==========================================
        try:
            c.execute("""
                SELECT user_id, first_name, COALESCE(base_elo, 1000), live_elo, weekly_attempts, weekly_correct
                FROM users 
                WHERE weekly_attempts > 0 AND live_elo < COALESCE(base_elo, 1000)
                ORDER BY (live_elo - COALESCE(base_elo, 1000)) ASC
                LIMIT 10
            """)
            bleeders = c.fetchall()

            if bleeders:
                bleed_text = "🩸 **ELO BLEED LEADERBOARD**\n\n"
                
                for idx, w in enumerate(bleeders):
                    w_id, w_name, w_base, w_live, w_att, w_cor = w
                    bleed_amount = round(w_live - w_base, 1)
                    
                    curr_acc = (w_cor / w_att) * 100 if w_att > 0 else 0
                    c.execute("SELECT attempts, correct FROM weekly_rank_history WHERE user_id = %s ORDER BY week_num ASC", (w_id,))
                    hist = c.fetchall()
                    
                    raw_growth = 0.0
                    if len(hist) > 0:
                        if len(hist) == 1:
                            base_att = hist[0][0]
                            base_corr = hist[0][1] if hist[0][1] is not None else 0
                        else:
                            base_att = hist[0][0] + hist[1][0]
                            base_corr = (hist[0][1] if hist[0][1] is not None else 0) + (hist[1][1] if hist[1][1] is not None else 0)
                        
                        base_acc = (base_corr / base_att) * 100 if base_att > 0 else 0
                        accuracy_shift = curr_acc - base_acc
                        elo_factor = (w_live - 1000) / 10.0
                        consistency_multiplier = 1.0 + (len(hist) * 0.05)
                        raw_growth = (accuracy_shift + elo_factor) * consistency_multiplier

                    clean_growth = round(raw_growth, 1)

                    if clean_growth > 2.0:
                        growth_icon = f"🟢 +{clean_growth}%"
                    elif clean_growth < -2.0:
                        growth_icon = f"🔴 {clean_growth}%"
                    else:
                        growth_icon = f"🟡 {'+' if clean_growth > 0 else ''}{clean_growth}%"

                    bleed_text += f"{idx + 1}. [{w_name}](tg://user?id={w_id}) ➪ {bleed_amount} Elo    {growth_icon}\n"

                bleed_text += "\n🟢 Positive growth\n🟡 Almost unchanged\n🔴 Negative growth"

                http_session.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", 
                    json={"chat_id": "716496729", "text": bleed_text, "parse_mode": "Markdown"}, 
                    timeout=5
                )
                reset_status["Elo_Bleed_DM"] = "🟢 Success"
        except Exception as e:
            print(f"🚨 Error generating Elo Bleed DM: {e}")

        # ✨ Execute "The Great Wipe" securely
        c.execute("UPDATE users SET base_elo = live_elo, weekly_score = 0, weekly_attempts = 0, weekly_correct = 0")
        c.execute("DELETE FROM precise_scores")

        announcement_text = "🏆 ✨ **WEEKLY CUP WRAP-UP & ANALYSIS** ✨ 🏆\n\n"
        announcement_text += "📊 **Community Performance Analysis:**\n"
        announcement_text += f"• **Active Challengers:** **{total_active_students}** students consistently competed this week.\n"
        announcement_text += f"• **Total Engagement:** A massive **{total_weekly_attempts}** questions were attempted collectively!\n"
        announcement_text += f"• **Overall Accuracy:** The class achieved a combined accuracy rate of **{overall_accuracy}%**.\n"
        announcement_text += f"• **League Progress:** The final promotion cut-off landed at **{int(target_average)} pts**, with **{promoted_count}** students successfully levelling up their league tier.\n\n"
        announcement_text += "⚡️ The leaderboards have been wiped clean. Attempt your first quiz today at 7:00 PM to kick off the new week!"

        for attempt in range(5):
            try:
                res = http_session.post(url, json={"chat_id": CHAT_ID, "message_thread_id": ANNOUNCEMENT_THREAD_ID, "text": announcement_text, "parse_mode": "Markdown"}, timeout=10) # type: ignore
                if res.json().get("ok"):
                    reset_status["Public_WrapUp"] = "🟢 Success"
                    for _ in range(3):
                        try:
                            http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/pinChatMessage", json={"chat_id": CHAT_ID, "message_id": res.json()["result"]["message_id"], "disable_notification": False}, timeout=5)
                            break
                        except: time.sleep(2)
                    break
                elif res.json().get("error_code") == 429: time.sleep(res.json().get("parameters", {}).get("retry_after", 3) + 1)
                else: break
            except: time.sleep(3 + attempt * 2)

        # --- 🧹 SUNDAY SWEEP ---
        global POLL_CACHE
        POLL_CACHE.clear()
        c.execute("DELETE FROM polls")
        c.execute("DELETE FROM user_answers")
        thirty_days_ago = (datetime.utcnow() + timedelta(hours=5, minutes=30) - timedelta(days=30)).strftime('%Y-%m-%d')
        c.execute("DELETE FROM daily_history WHERE date_str < %s", (thirty_days_ago,))
        if week_row: c.execute("DELETE FROM weekly_rank_history WHERE week_num < %s", (int(week_row[0]) - 10,))

        conn.commit()
        reset_status["Database_Reset"] = "🟢 Success"
        update_live_leaderboard()
        
        # ✨ Generate and send the final comprehensive status report!
        report_msg = (
            "🏆 **WEEKLY RESET STATUS REPORT** 🏆\n\n"
            f"🗄️ **Database Integrity:** {reset_status['Database_Reset']}\n"
            f"🔐 **Admin Debrief:** {reset_status['Admin_Debrief']}\n"
            f"🩸 **Elo Bleed DM:** {reset_status['Elo_Bleed_DM']}\n"
        )    
        
        # ✨ Send explicitly ONLY to Prathu and EZ
        for admin_id in [716496729, 5103843488]:
            try:
                http_session.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", 
                    json={"chat_id": admin_id, "text": report_msg, "parse_mode": "Markdown"}, 
                    timeout=5
                )
            except: pass
        
    except Exception as e:
        # ✨ THE ULTIMATE SAFETY NET: If ANYTHING fails, erase all changes!
        if conn:
            conn.rollback()
        print(f"🚨 Weekly Reset Error: {e}")
        notify_prathu(f"🚨 **Weekly Reset Error (Safely Rolled Back!):**\n`{e}`")
    finally:
        # ✨ Guarantees the database connection is safely returned
        if conn:
            try: c.close()
            except: pass
            release_db(conn)
            
@app.route('/reset_weekly/0508', methods=['GET', 'POST'])
def trigger_weekly_reset():
    threading.Thread(target=run_weekly_reset_background).start()
    return "Weekly reset triggered!", 200

def auto_finalize_abandoned_attempts():
    """Sweeps attempts running past duration_seconds + 30s and marks them submitted."""
    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        current_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
        
        c.execute("""
            SELECT a.id, a.started_at, s.duration_seconds
            FROM quiz_attempts a
            JOIN quiz_sets s ON a.quiz_set_id = s.id
            WHERE a.submitted_at IS NULL
              AND a.started_at < (%s - (s.duration_seconds || ' seconds')::interval)
        """, (current_ist,))
        
        expired_attempts = c.fetchall()
        for attempt in expired_attempts:
            att_id = attempt[0]
            c.execute("""
                UPDATE quiz_attempts
                SET submitted_at = started_at + (SELECT duration_seconds * INTERVAL '1 second' FROM quiz_sets WHERE id = quiz_attempts.quiz_set_id),
                    score = COALESCE((
                        SELECT SUM(CASE WHEN is_correct THEN 1.0 ELSE -0.25 END)
                        FROM quiz_responses
                        WHERE attempt_id = %s AND selected_index IS NOT NULL
                    ), 0.0)
                WHERE id = %s
            """, (att_id, att_id))
            
        conn.commit()
    except Exception as e:
        print(f"⚠️ Sweeper error: {e}")
    finally:
        if conn: release_db(conn)

def run_queue_processor_background():
    # Automatically sweep expired unsubmitted attempts
    auto_finalize_abandoned_attempts()

    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        
        # ✨ 1. Drain the Ultra-Fast Redis Queue FIRST
        if redis_client:
            # Pop up to 100 items per cycle to prevent Render timeouts
            for _ in range(100):
                item = redis_client.lpop("queue:poll_answers")
                if not item:
                    break
                
                data = json.loads(item)
                process_answer(
                    c, queue_id=None, user_id=data['user_id'], 
                    first_name=data['first_name'], poll_id=data['poll_id'], 
                    chosen_option=data['chosen_option']
                )

        # 🛡️ 2. Sweep the Legacy Supabase Queue (Fail-Open Fallback)
        c.execute("SELECT id, user_id, first_name, poll_id, chosen_option FROM answer_queue WHERE status IN ('pending', 'processing')")
        pending_answers = c.fetchall()

        if pending_answers:
            for row in pending_answers:
                c.execute("UPDATE answer_queue SET status = 'processing' WHERE id = %s", (row[0],))
            conn.commit()

            for row in pending_answers:
                # ✨ FIX: We now pass the unique queue ID (row[0]) to the processor
                process_answer(c, queue_id=row[0], user_id=row[1], first_name=row[2], poll_id=row[3], chosen_option=row[4])

        # 2. ✨ NEW: Sweep pending join requests (The 5-Minute Time Bomb)
        c.execute("SELECT key, value FROM bot_settings WHERE key LIKE 'join_req_%'")
        pending_joins = c.fetchall()
        current_time = int(time.time())
        
        for key, val in pending_joins:
            try:
                expire_time = int(val)
                if current_time >= expire_time:
                    u_id = key.replace("join_req_", "")
                    
                    # Execute the decline
                    res = http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/declineChatJoinRequest", json={"chat_id": CHAT_ID, "user_id": u_id}, timeout=5)
                    
                    # If it succeeds, or if the request is already resolved/manually approved (400 Bad Request), remove it from DB
                    if res.status_code == 200 or res.status_code == 400:
                        c.execute("DELETE FROM bot_settings WHERE key = %s", (key,))
                        conn.commit()
            except Exception as inner_e:
                print(f"Error processing join req {key}: {inner_e}")
            
        c.close()
    except Exception as e:
        error_str = str(e).lower()
        if "ssl" in error_str or "eof" in error_str or "closed" in error_str or "timeout" in error_str:
            pass 
        else:
            try:
                http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={
                    "chat_id": "716496729",
                    "text": f"🚨 **CRITICAL CRON ERROR (Queue Processor)** 🚨\n\n`{e}`",
                    "parse_mode": "Markdown"
                }, timeout=5)
            except: pass
    finally:
        if conn:
            release_db(conn)

@app.route('/cron/process_leaderboard_0508', methods=['GET', 'POST'])
def cron_process_leaderboard():
    # 🔒 SECURITY GATE
    if request.headers.get("X-Cron-Secret") != CRON_SECRET:
        return "Unauthorized", 401
    
    # ✨ FIX 3: Instantly answer the cron request to prevent 30s timeouts
    threading.Thread(target=run_queue_processor_background).start()
    return "Queue Processor triggered in background!", 200

def run_heavy_math_background():
    # ✨ REDIS DISTRIBUTED LOCK (Prevents concurrent heavy database math)
    if redis_client:
        # Lock for 120 seconds. If another worker is already doing math, stop.
        acquired = redis_client.set("lock:leaderboard-recalculate", "1", nx=True, ex=120)
        if not acquired:
            print("⏳ Heavy math already running on another worker. Skipping.")
            return

    conn = None
    try:
        recalculate_dynamic_scores()
        
        conn = get_db()
        c = conn.cursor()
        c.execute("""
            INSERT INTO bot_settings (key, value) VALUES ('telegram_needs_update', '1') 
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """)
        # ✨ NEW: Tell the Snapshot cron that fresh math is ready!
        c.execute("""
            INSERT INTO bot_settings (key, value) VALUES ('needs_snapshot', '1') 
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """)
        conn.commit()
        c.close()
    except Exception as e:
        try:
            http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={
                "chat_id": "716496729",
                "text": f"🚨 **CRITICAL CRON ERROR (Heavy Math Engine)** 🚨\n\n`{e}`",
                "parse_mode": "Markdown"
            }, timeout=5)
        except: pass
    finally:
        # ✨ Safely release the lock and the database connection
        if redis_client:
            redis_client.delete("lock:leaderboard-recalculate")
        if conn:
            release_db(conn)

@app.route('/cron/heavy_math_0508', methods=['GET', 'POST'])
def cron_heavy_math():
    # 🔒 SECURITY GATE
    if request.headers.get("X-Cron-Secret") != CRON_SECRET:
        return "Unauthorized", 401

    # ✨ FIX: Instantly answer the cron request, then run the heavy math in the background
    threading.Thread(target=run_heavy_math_background).start()
    return "Math Engine triggered in background!", 200

@app.route('/cron/update_telegram_text_0508', methods=['GET', 'POST'])
def cron_update_telegram_text():
    # 🔒 SECURITY GATE
    if request.headers.get("X-Cron-Secret") != CRON_SECRET:
        return "Unauthorized", 401

    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT value FROM bot_settings WHERE key='telegram_needs_update'")
        row = c.fetchone()
        
        if row and row[0] == '1':
            c.execute("UPDATE bot_settings SET value='0' WHERE key='telegram_needs_update'")
            conn.commit()
            c.close()
            
            update_live_leaderboard()
            return "Telegram banner updated!", 200
        else:
            c.close()
            return "No update needed.", 200
    except Exception as e:
        try:
            http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={
                "chat_id": "716496729",
                "text": f"🚨 **CRITICAL CRON ERROR (Telegram Updater)** 🚨\n\n`{e}`",
                "parse_mode": "Markdown"
            }, timeout=5)
        except: pass
        return f"Error: {e}", 500
    finally:
        if conn:
            release_db(conn)


@app.route('/cron/dispatcher_0508', methods=['GET', 'POST'])
def trigger_dispatcher():
    threading.Thread(target=dispatch_practice_sets).start()
    return "Dispatcher triggered!", 200

# ==========================================
# SECURED: PRACTICE SET DISPATCHER
# ==========================================
def dispatch_practice_sets():
    CONNECT_TO_LEADERBOARD = True
    PRACTICE_THREAD_ID = 10123
    current_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    current_day = current_ist.strftime('%a')

    try:
        cache_buster = int(time.time())
        # 👉 CHANGED: Updated to the authenticated API URL structure for private contents
        github_grammar_url = f"https://api.github.com/repos/prathu-developer/exam-scraper-api/contents/grammar.json?ref=main&t={cache_buster}"
        
        headers = {
            "Authorization": f"token {GITHUB_PAT}",
            "Accept": "application/vnd.github.v3.raw"  # Tells GitHub to return the raw JSON file text directly
        }
        
        response = http_session.get(github_grammar_url, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()
        titles, set_a, set_b, set_c = data.get("titles", []), data.get("set_a", []), data.get("set_b", []), data.get("set_c", [])
    except Exception as e:
        notify_prathu(f"🚨 **CRITICAL ERROR (Grammar):** Failed to fetch `grammar.json` from GitHub. Practice sets did NOT drop!\n`{e}`")
        return

    if not set_a or not set_b or not set_c: return
    
    # ✨ NEW: Completely shuffle the questions within each set before sending
    import random
    random.shuffle(set_a)
    random.shuffle(set_b)
    random.shuffle(set_c)

    def safe_send_text(text, pin=False):
        for attempt in range(10):
            try:
                res = http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "message_thread_id": PRACTICE_THREAD_ID, "text": text, "parse_mode": "Markdown"}, timeout=20)
                if res.status_code == 200:
                    if pin: http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/pinChatMessage", json={"chat_id": CHAT_ID, "message_id": res.json()["result"]["message_id"]}, timeout=10)
                    return True
                elif res.status_code == 429: time.sleep(res.json().get("parameters", {}).get("retry_after", 3) + 1)
                else: time.sleep(2)
            except: time.sleep(3 + attempt)
        return False

    def send_and_link_poll(q_list, default_ui_type, shuffle, start_q_num):
        import random
        conn = get_db()
        c = conn.cursor()
        for i, mcq in enumerate(q_list):
            # ✨ Exact next-day 11:59 PM timer calculated milliseconds before sending
            now_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
            dynamic_open_period = int(((now_ist + timedelta(days=1)).replace(hour=23, minute=59, second=59) - now_ist).total_seconds())
            if dynamic_open_period < 5: dynamic_open_period = 5

            options = mcq['options']
            if mcq['correct_answer'] not in options: options[0] = mcq['correct_answer']
            
            # Python shuffles the list if the rule demands it
            if shuffle:
                random.shuffle(options)
                
            correct_index = options.index(mcq['correct_answer'])
            current_ui = f'Choose the best replacement for the words "{mcq["target_phrase"]}".' if 'target_phrase' in mcq else mcq.get('custom_ui', default_ui_type)

            poll_payload = {
                "chat_id": CHAT_ID, "message_thread_id": PRACTICE_THREAD_ID,
                "question": f"Que {start_q_num + i}: {current_ui}\n\n{mcq['sentence']}"[:300],
                "options": json.dumps([opt[:100] for opt in options]),
                "type": "quiz", "correct_option_id": correct_index, "explanation": mcq['explanation'][:200],
                "is_anonymous": False, "open_period": dynamic_open_period
            }
            for tg_attempt in range(10):
                try:
                    res = http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPoll", json=poll_payload, timeout=20)
                    if res.status_code == 200:
                        if CONNECT_TO_LEADERBOARD:
                            poll_id = res.json()['result']['poll']['id']
                            c.execute("""
                                INSERT INTO polls (poll_id, correct_index, poll_day) VALUES (%s, %s, %s)
                                ON CONFLICT (poll_id) DO UPDATE SET correct_index = EXCLUDED.correct_index, poll_day = EXCLUDED.poll_day
                            """, (poll_id, correct_index, current_day))
                            conn.commit()
                        break
                    elif res.status_code == 429: time.sleep(res.json().get("parameters", {}).get("retry_after", 3) + 1)
                    else: time.sleep(2)
                except: time.sleep(3 + tg_attempt)
            time.sleep(4)
        c.close()
        release_db(conn)

    intro_text = f"🗓 `{current_ist.strftime('%d %B %Y')}`\n📰 **THE HINDU EDITORIAL ANALYSIS**\n\n"
    if titles: intro_text += f"**Today's Focus:**\n1️⃣ *{titles[0]}*\n2️⃣ *{titles[1]}*\n\n"
    intro_text += "Read the editorials carefully. Your advanced grammar trials begin below! 👇"
    safe_send_text(intro_text, pin=True)
    time.sleep(2)
    safe_send_text("🎯 **SET A — Error Detection**\n⬇️")
    time.sleep(2)
    # ✨ Changed shuffle to True
    send_and_link_poll(set_a, "Identify the part containing the error.", shuffle=True, start_q_num=1)
    time.sleep(300)
    safe_send_text("🎯 **SET B — Sentence Improvement**\n⬇️")
    time.sleep(2)
    # ✨ Changed shuffle to True to scramble all options
    send_and_link_poll(set_b, "Choose the best replacement.", shuffle=True, start_q_num=6)
    time.sleep(300)
    safe_send_text("🎯 **SET C — Fill in the Blank**\n⬇️")
    time.sleep(2)
    send_and_link_poll(set_c, "Choose the most appropriate option.", shuffle=True, start_q_num=11)
    notify_prathu("✅ **Grammar Practice Sets** generated and dispatched!")

def recalculate_dynamic_scores():
    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        
        # ⚡ 100% Zero-Egress Native Database Execution
        c.execute("SELECT recalculate_dynamic_scores();")
        conn.commit()
        
    except Exception as e:
        print(f"🚨 Native Math Engine Error: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            try: c.close()
            except: pass
            release_db(conn)

# ==========================================
# BACKGROUND WORKERS: SUNDAY ANNOUNCEMENTS SUITE
# ==========================================

def run_no_editorials_announcement():
    text = "_There won't be any Today's Editorials today; Editorials will be available Monday through Saturday exclusively._"
    for attempt in range(10):
        try:
            res = http_session.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "message_thread_id": 3, "text": text, "parse_mode": "Markdown"},
                timeout=20
            )
            if res.status_code == 200:
                notify_prathu("📢 **No Editorials Notice** posted successfully!")
                break
            elif res.status_code == 429:
                time.sleep(res.json().get("parameters", {}).get("retry_after", 5) + 1)
            else:
                time.sleep(2)
        except Exception:
            time.sleep(3 + attempt * 2)

def run_activity_requirement_announcement():
    text = (
        "📢 **Activity Requirement**\n\n"
        "📝 Attempt at least 50 Quizzes or\n"
        "📖 Read 4 Magazines (using the new 'Mark as Read' button)\n\n"
        "⏳ **Every 15 Days**\n\n"
        "❗️ Members who remain inactive for 15 days will be removed to make room for new students and keep the community active."
    )
    for attempt in range(10):
        try:
            res = http_session.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "message_thread_id": 5, "text": text, "parse_mode": "Markdown"},
                timeout=20
            )
            if res.status_code == 200:
                notify_prathu("📢 **Sunday Activity Requirement Announcement** posted successfully!")
                break
            elif res.status_code == 429:
                time.sleep(res.json().get("parameters", {}).get("retry_after", 5) + 1)
            else:
                time.sleep(2)
        except Exception:
            time.sleep(3 + attempt * 2)

def run_sunday_final_reminder():
    current_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    total_seconds = int((current_ist.replace(hour=23, minute=59, second=59) - current_ist).total_seconds())
    time_str = f"{total_seconds // 3600} Hours and {(total_seconds % 3600) // 60} Minutes" if total_seconds > 0 else "0 Minutes"

    text = (
        f"<blockquote>⏱ <b>{time_str} Remaining:</b> The weekly leaderboard officially locks tonight at midnight!\n\n"
        f"⚡️ This is your final reminder to complete your pending <b>Vocab and Grammar</b> quizzes before time runs out. "
        f"Every point counts towards the House Cup!</blockquote>"
    )

    for attempt in range(10):
        try:
            res = http_session.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "message_thread_id": 11, "text": text, "parse_mode": "HTML"},
                timeout=20
            )
            if res.status_code == 200:
                http_session.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/pinChatMessage",
                    json={"chat_id": CHAT_ID, "message_id": res.json()["result"]["message_id"], "disable_notification": False},
                    timeout=10
                )
                notify_prathu("⏱️ **Sunday Final Midnight Reminder** posted successfully!")
                break
            elif res.status_code == 429:
                time.sleep(res.json().get("parameters", {}).get("retry_after", 5) + 1)
            else:
                time.sleep(2)
        except Exception:
            time.sleep(3 + attempt * 2)

def run_all_sunday_announcements():
    run_no_editorials_announcement()
    time.sleep(3)
    run_activity_requirement_announcement()
    time.sleep(3)
    run_sunday_final_reminder()

@app.route('/sunday_announcement/0508', methods=['GET', 'POST'])
def trigger_sunday_announcement():
    threading.Thread(target=run_all_sunday_announcements).start()
    return "All Sunday announcements triggered in background!", 200

# ==========================================
# SECURED: DAILY VOCAB & QUIZZES
# ==========================================
def run_daily_vocab_and_quizzes():
    current_ist_time = datetime.utcnow() + timedelta(hours=5, minutes=30)
    text = f"⚡ ⟨ **{current_ist_time.strftime('%d').lstrip('0')} {current_ist_time.strftime('%B %Y')}** ⟩ ⚡\n📚 **Daily Vocab Quiz** 🖋️\n\n*Let the magical trials commence!* 🔮✨"

    for attempt in range(10):
        try:
            res = http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "message_thread_id": 246, "text": text, "parse_mode": "Markdown"}, timeout=20)
            if res.status_code == 200:
                http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/pinChatMessage", json={"chat_id": CHAT_ID, "message_id": res.json()["result"]["message_id"], "disable_notification": False}, timeout=20)
                break
            elif res.status_code == 429: time.sleep(res.json().get("parameters", {}).get("retry_after", 5) + 1)
            else: time.sleep(2)
        except: time.sleep(3 + attempt * 2)
    time.sleep(3)

    try:
        # 👉 CHANGED: Updated URL layout and authenticated header
        github_vocab_url = f"https://api.github.com/repos/prathu-developer/exam-scraper-api/contents/questions.json?ref=main&t={int(time.time())}"
        headers = {
            "Authorization": f"token {GITHUB_PAT}",
            "Accept": "application/vnd.github.v3.raw"
        }
        mcqs = http_session.get(github_vocab_url, headers=headers, timeout=15).json()
    except Exception as e: 
        notify_prathu(f"🚨 **CRITICAL ERROR (Vocab):** Failed to fetch `questions.json` from GitHub. Quizzes did NOT drop!\n`{e}`")
        return

    import random
    for mcq in mcqs:
        # ✨ Exact next-day 11:59 PM timer calculated milliseconds before sending
        now_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
        dynamic_open_period = int(((now_ist + timedelta(days=1)).replace(hour=23, minute=59, second=59) - now_ist).total_seconds())
        if dynamic_open_period < 5: dynamic_open_period = 5

        options = mcq['options']
        if mcq['correct_answer'] not in options: options[0] = mcq['correct_answer']
        
        # Vocab options should ALWAYS be randomized
        random.shuffle(options)
        
        correct_index = options.index(mcq['correct_answer'])

        poll_payload = {
            "chat_id": CHAT_ID, "message_thread_id": 246, "question": mcq['question'], "options": json.dumps(options),
            "type": "quiz", "correct_option_id": correct_index, "explanation": mcq.get('explanation', '')[:200],
            "is_anonymous": False, "open_period": dynamic_open_period
        }

        for attempt in range(10):
            try:
                poll_res = http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPoll", json=poll_payload, timeout=20)
                if poll_res.status_code == 200:
                    try:
                        conn = get_db()
                        c = conn.cursor()
                        c.execute("""
                            INSERT INTO polls (poll_id, correct_index, poll_day) VALUES (%s, %s, %s)
                            ON CONFLICT (poll_id) DO UPDATE SET correct_index = EXCLUDED.correct_index, poll_day = EXCLUDED.poll_day
                        """, (poll_res.json()["result"]["poll"]["id"], correct_index, current_ist_time.strftime('%a')))
                        conn.commit()
                        c.close()
                        release_db(conn)
                    except: pass
                    break
                elif poll_res.status_code == 429: time.sleep(poll_res.json().get("parameters", {}).get("retry_after", 5) + 1)
                else: time.sleep(2)
            except: time.sleep(3 + attempt * 2)
            
        # ✨ FIX: 5-second delay = 12 msgs/min (100% immune to Telegram's spam filter)
        # Completes safely at 7:02 PM, leaving a 3-minute cooldown before Grammar drops at 7:05 PM!
        time.sleep(5)
        
    notify_prathu("✅ **Daily Vocab Quiz** generated and dispatched!")
    
@app.route('/daily_vocab/0508', methods=['GET', 'POST'])
def trigger_daily_vocab():
    threading.Thread(target=run_daily_vocab_and_quizzes).start()
    return "Daily Vocab triggered!", 200

def run_sunday_final_reminder():
    current_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    total_seconds = int((current_ist.replace(hour=23, minute=59, second=59) - current_ist).total_seconds())
    time_str = f"{total_seconds // 3600} Hours and {(total_seconds % 3600) // 60} Minutes" if total_seconds > 0 else "0 Minutes"

    # ✨ REVAMPED: Simple, punchy, and zero database math required!
    text = f"<blockquote>⏱ <b>{time_str} Remaining:</b> The weekly leaderboard officially locks tonight at midnight!\n\n⚡️ This is your final reminder to complete your pending <b>Vocab and Grammar</b> quizzes before time runs out. Every point counts towards the House Cup!</blockquote>"

    for attempt in range(10):
        try:
            res = http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "message_thread_id": 11, "text": text, "parse_mode": "HTML"}, timeout=20)
            if res.status_code == 200:
                http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/pinChatMessage", json={"chat_id": CHAT_ID, "message_id": res.json()["result"]["message_id"], "disable_notification": False}, timeout=10)
                notify_prathu("⏱️ **Sunday Final Midnight Reminder** posted successfully!")
                break
        except: time.sleep(3 + attempt * 2)

# ==========================================
# BACKGROUND WORKER: EXAM COUNTDOWN RESTORED
# ==========================================
def run_countdown_and_commentary():
    fetch_and_update_exams_db()
    
    # --- MASTER SPEC: Migrated to Mini App (Section 4) ---
    # time.sleep(3)
    # update_exam_countdown()
    # time.sleep(5)
    # generate_and_send_commentary()
    
    notify_prathu("📅 **Exam Database** synced successfully! (Telegram posts disabled per migration)")

@app.route('/update_countdown/0508', methods=['GET', 'POST'])
def trigger_countdown_update():
    threading.Thread(target=run_countdown_and_commentary).start()
    return "Countdown triggered!", 200

# ==========================================
# SECURED: EXAM COUNTDOWN DATABASE UPDATE
# ==========================================
def fetch_and_update_exams_db():
    try:
        # 👉 CHANGED: Updated URL layout and authenticated header for exams.json
        github_exams_url = f"https://api.github.com/repos/prathu-developer/exam-scraper-api/contents/exams.json?ref=main&t={int(time.time())}"
        headers = {
            "Authorization": f"token {GITHUB_PAT}",
            "Accept": "application/vnd.github.v3.raw"
        }
        latest_exams = http_session.get(github_exams_url, headers=headers, timeout=10).json()
        
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM upcoming_exams")
        for exam in latest_exams:
            c.execute("""
                INSERT INTO upcoming_exams (name, exam_date, status, is_exact_date, display_date) 
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (name) DO UPDATE SET 
                    exam_date = EXCLUDED.exam_date, status = EXCLUDED.status, 
                    is_exact_date = EXCLUDED.is_exact_date, display_date = EXCLUDED.display_date
            """, (exam['name'], exam['date'], exam['status'], int(exam['is_exact_date']), exam['display_date']))
        conn.commit()
        c.close()
        release_db(conn)
    except Exception as e: print(f"⚠️ Failed to update exam dates from private repo: {e}")

def relay_to_channel_with_buttons(source_msg_id):
    """Copies message from Thread 271 to the channel and posts follow-up buttons."""
    copy_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/copyMessage"
    payload = {
        "chat_id": TARGET_CHANNEL_ID,
        "from_chat_id": SOURCE_CHAT_ID,
        "message_id": source_msg_id
    }
    
    try:
        res = http_session.post(copy_url, json=payload, timeout=10)
        if res.status_code == 200:
            new_msg_id = res.json()["result"]["message_id"]

            # Save link to DB for sync edit compatibility
            try:
                conn = get_db()
                c = conn.cursor()
                c.execute("""
                    INSERT INTO message_links (source_msg_id, target_msg_id) VALUES (%s, %s)
                    ON CONFLICT (source_msg_id) DO UPDATE SET target_msg_id = EXCLUDED.target_msg_id
                """, (source_msg_id, new_msg_id))
                conn.commit()
                c.close()
                release_db(conn)
            except Exception:
                pass

            # Dispatch buttons with an invisible character so no text line appears
            button_payload = {
                "chat_id": TARGET_CHANNEL_ID,
                "text": "\u200b",
                "reply_markup": {
                    "inline_keyboard": [[
                        {
                            "text": "Magazine",
                            "url": "https://t.me/ezeditorialgroup/3"
                        },
                        {
                            "text": "Quizzes",
                            "url": "https://t.me/Ez_vocab_bot"
                        }
                    ]]
                }
            }
            http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=button_payload, timeout=10)
    except Exception as e:
        print(f"⚠️ Error relaying to target channel: {e}")

def check_student_membership(user_id):
    """Verifies whether the student exists in the database and is present in CHAT_ID."""
    is_in_db = False
    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT 1 FROM users WHERE user_id = %s", (user_id,))
        is_in_db = c.fetchone() is not None
    except Exception:
        pass
    finally:
        if conn:
            release_db(conn)

    is_group_member = False
    try:
        res = http_session.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getChatMember",
            params={"chat_id": CHAT_ID, "user_id": user_id},
            timeout=5
        )
        if res.status_code == 200:
            status = res.json().get("result", {}).get("status")
            if status in ["member", "administrator", "creator", "restricted"]:
                is_group_member = True
    except Exception:
        pass

    return is_in_db, is_group_member

def handle_private_bot_start(user_id, first_name):
    """Gatekeeper flow when a user interacts with the bot directly."""
    is_in_db, is_group_member = check_student_membership(user_id)

    # Sync membership into DB if they joined without registering
    if is_group_member and not is_in_db:
        try:
            conn = get_db()
            c = conn.cursor()
            c.execute("""
                INSERT INTO users (user_id, first_name, joined_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (user_id) DO UPDATE SET first_name = EXCLUDED.first_name
            """, (user_id, first_name))
            conn.commit()
            c.close()
            release_db(conn)
            is_in_db = True
        except Exception:
            pass

    # Approved student in both DB and group
    if is_in_db and is_group_member:
        payload = {
            "chat_id": user_id,
            "text": (
                f"👋 **Welcome back, {first_name}!**\n\n"
                "Your entrance trial is verified and your student profile is active.\n\n"
                "Tap below to launch your trials and view your rank!"
            ),
            "parse_mode": "Markdown",
            "reply_markup": {
                "inline_keyboard": [[
                    {
                        "text": "⚡ Launch Mini App & Leaderboard",
                        "url": MINI_APP_URL
                    }
                ]]
            }
        }
    else:
        # Prompt to join the community group and complete the trial
        payload = {
            "chat_id": user_id,
            "text": (
                f"👋 **Welcome to Ez Vocab Bot, {first_name}!**\n\n"
                "To access daily timed tests and compete on the leaderboard, you must join our group and complete the entrance trial:\n\n"
                "1️⃣ **Join the Group:** Send a request to our study group.\n"
                "2️⃣ **Entrance Trial:** Clear the quick 10-question English test on joining.\n\n"
                "Once accepted, tap 'Check My Status' below!"
            ),
            "parse_mode": "Markdown",
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": "🏛 Join Ez Editorial Group", "url": COMMUNITY_GROUP_URL}],
                    [{"text": "🔄 Check My Status", "callback_data": "check_status"}]
                ]
            }
        }

    http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload, timeout=10)

def relay_message(message_id, target_thread_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/copyMessage"
    payload = {"chat_id": CHAT_ID, "from_chat_id": SOURCE_CHAT_ID, "message_id": message_id, "message_thread_id": target_thread_id}
    for attempt in range(5):
        try:
            res = http_session.post(url, json=payload, timeout=10)
            if res.status_code == 200:
                new_msg_id = res.json()["result"]["message_id"]
                try:
                    conn = get_db()
                    c = conn.cursor()
                    c.execute("""
                        INSERT INTO message_links (source_msg_id, target_msg_id) VALUES (%s, %s)
                        ON CONFLICT (source_msg_id) DO UPDATE SET target_msg_id = EXCLUDED.target_msg_id
                    """, (message_id, new_msg_id))
                    conn.commit()
                    c.close()
                    release_db(conn)
                except: pass

                # ✨ Inject dual buttons into Thread 3 (Editorials)
                if target_thread_id == 3:
                    markup = {
                        "inline_keyboard": [
                            [{"text": "📖 Mark as Read • 0", "callback_data": f"read_{new_msg_id}"}],
                            [{"text": "🎯 Topic Quiz (4:30 PM)", "url": "https://t.me/Ez_vocab_bot/leaderboard"}]
                        ]
                    }
                    http_session.post(
                        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageReplyMarkup",
                        json={"chat_id": CHAT_ID, "message_id": new_msg_id, "reply_markup": markup},
                        timeout=5
                    )
                return
        except: time.sleep(3)
            
def sync_message_edit(msg, target_msg_id):
    try:
        # If it's a standard text message
        if 'text' in msg:
            payload = {
                "chat_id": CHAT_ID,
                "message_id": target_msg_id,
                "text": msg['text']
            }
            if 'entities' in msg:
                payload['entities'] = msg['entities']
            http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText", json=payload, timeout=10)
            
        # If it's a photo/document with a caption
        elif 'caption' in msg:
            payload = {
                "chat_id": CHAT_ID,
                "message_id": target_msg_id,
                "caption": msg['caption']
            }
            if 'caption_entities' in msg:
                payload['caption_entities'] = msg['caption_entities']
            http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageCaption", json=payload, timeout=10)
    except Exception as e:
        print(f"Sync edit error: {e}")

def process_ranking_command(chat_id, user_id, message_id, thread_id):
    try:
        # 1. ✨ STEALTH MODE: Instantly delete the student's /rank text message
        try:
            http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteMessage", json={
                "chat_id": chat_id,
                "message_id": message_id
            }, timeout=5)
        except Exception as e:
            pass

        # 2. ⚡ READ DIRECTLY FROM RAM CACHE (No DB Query)
        global RAM_CACHE
        if not RAM_CACHE.get("master_data"):
            try:
                bake_miniapp_cache()
            except Exception as e:
                print(f"Error auto-baking cache for /rank: {e}")

        master_data = RAM_CACHE.get("master_data")
        if not master_data:
            return

        # Read directly from the python dictionary (No JSON parsing needed!)
        leaderboard = master_data.get('leaderboard', [])
        elo_ranking = master_data.get('elo_ranking', [])
        total_quizzes = master_data.get('total_quizzes', 0)

        # 3. Find the Student's pre-calculated stats
        user_stats = next((u for u in leaderboard if u['id'] == user_id), None)

        if not user_stats or user_stats.get('attempts', 0) == 0:
            payload = {
                "chat_id": chat_id,
                "receiver_user_id": user_id,
                "text": "🔮 **You haven't attempted any magical trials this week yet!** Drop into the daily quizzes to get ranked.",
                "parse_mode": "Markdown",
                "message_thread_id": 11,
                "reply_markup": {
                    "inline_keyboard": [[{"text": "📊 Open Full Dashboard", "url": "https://t.me/Ez_vocab_bot/leaderboard"}]]
                }
            }
            http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload, timeout=10)
            return

        u_score = user_stats['score']
        u_attempts = user_stats['attempts']
        u_correct = user_stats['history']['correct']
        u_wrong = user_stats['history']['wrong']
        u_league = user_stats['league']
        u_elo = user_stats['elo']
        accuracy = user_stats['history']['accuracy']
        weekly_rank = user_stats['rank']
        total_active = len(leaderboard)

        # 4. Instant In-Memory Average Calculation
        sum_weighted_points, sum_weights = 0.0, 0.0
        for u in leaderboard:
            att = u.get('attempts', 0)
            if att == 0: continue
            acc = u['history']['accuracy'] / 100.0 
            vol_w = att / (att + 10.0)
            final_w = vol_w * acc
            if u['score'] < 0: final_w = 0.0
            sum_weighted_points += (u['score'] * final_w)
            sum_weights += final_w

        target_average = int((sum_weighted_points / sum_weights) + 0.5) if sum_weights > 0 else 0

        # 5. Extract Global Elo Rank
        global_elo_rank = next((eu['rank'] for eu in elo_ranking if eu['id'] == user_id), "N/A")

        # 6. Map League Tier Icon and Name
        LEAGUE_INFO = {
            0: ("🛡️", "Unranked"), 1: ("🥉", "Bronze"), 2: ("🥈", "Silver"),
            3: ("🥇", "Gold"), 4: ("💠", "Platinum"), 5: ("💎", "Diamond"),
            6: ("👑", "Champion"), 7: ("🎖️", "Master"), 8: ("⚡", "Elite"),
            9: ("🌟", "Legend"), 10: ("🔮", "Mythic"), 11: ("⚛️", "Prodigy"),
            12: ("☄️", "Celestial"), 13: ("🧿", "Zenith"), 14: ("🌌", "Ascendant")
        }
        lg_icon, lg_name = LEAGUE_INFO.get(u_league, ("🛡️", "Unranked"))
        clean_score = int(u_score) if u_score % 1 == 0 else round(u_score, 1)
        clean_u_elo = int(round(u_elo)) # ✨ Added whole number formatting

        # 7. Dynamic Status Line & Target Gap
        if u_score >= target_average:
            status_symbol = "🟢"
            if weekly_rank > 20 and total_active >= 20:
                target_pts = leaderboard[19]['score']
                pts_needed = round(max(0.1, target_pts - u_score + 0.1), 1)
                clean_gap = int(pts_needed) if pts_needed % 1 == 0 else pts_needed
                status_line = f"{status_symbol} Above cut-off. Need {clean_gap} pts to reach Top 20."
            elif weekly_rank > 10 and total_active >= 10:
                target_pts = leaderboard[9]['score']
                pts_needed = round(max(0.1, target_pts - u_score + 0.1), 1)
                clean_gap = int(pts_needed) if pts_needed % 1 == 0 else pts_needed
                status_line = f"{status_symbol} Above cut-off. Need {clean_gap} pts to reach Top 10."
            elif weekly_rank > 1:
                target_pts = leaderboard[0]['score']
                pts_needed = round(max(0.1, target_pts - u_score + 0.1), 1)
                clean_gap = int(pts_needed) if pts_needed % 1 == 0 else pts_needed
                status_line = f"{status_symbol} Above cut-off. Need {clean_gap} pts to claim #1."
            else:
                status_line = f"{status_symbol} Above cut-off. Ruling at #1 place!"
        else:
            status_symbol = "🔴"
            pts_needed = round(max(0.1, target_average - u_score + 0.1), 1)
            clean_gap = int(pts_needed) if pts_needed % 1 == 0 else pts_needed
            status_line = f"{status_symbol} Need {clean_gap} pts to reach the Promotion Zone."

        # 8. Format Message Body
        reply_text = (
            f"🏆 **Your Weekly Progress**\n\n"
            f"🏅 **Rank:** #{weekly_rank} / {total_active}\n\n"
            f"⭐ **Score:** {clean_score} pts\n\n"
            # ✨ Updated variable here
            f"{lg_icon} **League:** {lg_name} • 🧠 **{clean_u_elo} Elo**\n\n"
            f"🎯 **{accuracy}% Accuracy** • ✅**{u_correct}** • ❌**{u_wrong}** • 📝**{u_attempts}/{total_quizzes}**\n\n"
            f"🌍 **Global Rank:** #{global_elo_rank}\n\n"
            f"{status_line}"
        )

        # 9. Send Ephemerally
        payload = {
            "chat_id": chat_id,
            "receiver_user_id": user_id,
            "text": reply_text,
            "parse_mode": "Markdown",
            "message_thread_id": 11,
            "reply_markup": {
                "inline_keyboard": [[
                    {
                        "text": "📊 Open Full Dashboard",
                        "url": "https://t.me/Ez_vocab_bot/leaderboard"
                    }
                ]]
            }
        }
        http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload, timeout=10)
    except Exception as e:
        print(f"🚨 Error executing /rank command: {e}")

def bake_miniapp_cache():
    conn = None
    try:
        conn = get_db()
        c = conn.cursor()

        c.execute("SELECT value FROM bot_settings WHERE key='current_week'")
        week_row = c.fetchone()
        try: current_week_val = int(week_row[0]) if week_row else 14
        except: current_week_val = 14

        c.execute("SELECT COUNT(*) FROM polls")
        total_quizzes_val = c.fetchone()[0]
    
        c.execute("""
            SELECT user_id, first_name, weekly_score, faction, is_captain, weekly_attempts, league_tier, weekly_correct, live_elo, last_updated
            FROM users WHERE weekly_attempts > 0 ORDER BY weekly_score DESC, last_updated ASC
        """)
        top_users = c.fetchall()

        # ✨ NEW: Calculate the EXACT True Average (Matches Telegram Message)
        sum_weighted_points = 0.0
        sum_weights = 0.0
        for user in top_users:
            u_score = float(user[2]) if user[2] is not None else 0.0
            u_attempts = int(user[5]) if user[5] is not None else 0
            u_correct = int(user[7]) if user[7] is not None else 0
            if u_attempts > 0:
                accuracy = u_correct / u_attempts
                volume_weight = u_attempts / (u_attempts + 10.0)
                final_weight = volume_weight * accuracy
                if u_score < 0: final_weight = 0.0
                sum_weighted_points += (u_score * final_weight)
                sum_weights += final_weight
                
        target_average = int((sum_weighted_points / sum_weights) + 0.5) if sum_weights > 0 else 0
    
        # ⚡ OPTIMIZED: Only pull users who are active and have an Elo rating
        seven_days_ago = time.time() - (7 * 24 * 3600)
        
        c.execute("""
            SELECT user_id, first_name, live_elo, last_updated 
            FROM users 
            WHERE live_elo IS NOT NULL 
              AND (weekly_attempts > 0 OR last_updated >= %s)
            ORDER BY live_elo DESC, last_updated ASC
        """, (seven_days_ago,))
        all_elo_users = c.fetchall()
    
        elo_leaderboard = [{"rank": i + 1, "id": eu[0], "name": eu[1], "elo": round(eu[2] if eu[2] is not None else 1000, 1), "is_active": True if (eu[3] if eu[3] else 0) >= seven_days_ago else False} for i, eu in enumerate(all_elo_users)]
        
        c.execute("""
            SELECT user_id, week_num, rank, total_members, score, attempts, correct 
            FROM weekly_rank_history 
            WHERE user_id IN (SELECT user_id FROM users WHERE weekly_attempts > 0)
            AND CAST(week_num AS INTEGER) >= %s 
            ORDER BY week_num DESC
        """, (current_week_val - 4,))
        
        rank_hist_dict = {}
        for r in c.fetchall():
            if r[0] not in rank_hist_dict: rank_hist_dict[r[0]] = []
            rank_hist_dict[r[0]].append({"week": r[1], "rank": r[2], "total": r[3], "score": r[4], "attempts": r[5], "correct": r[6]})
    
        c.execute("""
            SELECT user_id, day_label, score, attempts, correct_answers 
            FROM precise_scores 
            WHERE user_id IN (SELECT user_id FROM users WHERE weekly_attempts > 0)
        """)
        precise_scores_dict = {}
        for r in c.fetchall():
            if r[0] not in precise_scores_dict: precise_scores_dict[r[0]] = []
            precise_scores_dict[r[0]].append(r)

        def get_exact_history_fast(uid): return {row[1]: {"score": row[2], "attempts": row[3], "correct": row[4]} for row in precise_scores_dict.get(uid, [])}

        weighted_daily_sums = {"Mon": 0, "Tue": 0, "Wed": 0, "Thu": 0, "Fri": 0, "Sat": 0, "Sun": 0}
        sum_weights_chart = 0
        topper_history_dict = {}
        leaderboard_list = []
        day_order = {"Mon": 1, "Tue": 2, "Wed": 3, "Thu": 4, "Fri": 5, "Sat": 6, "Sun": 7}

        for index, user in enumerate(top_users):
            uid = user[0]
            u_score = int(user[2]) if user[2] % 1 == 0 else round(user[2], 2)
            u_attempts = user[5] if user[5] is not None else 0
            u_correct = user[7] if user[7] is not None else 0
            
            weight = u_attempts ** 0.5
            sum_weights_chart += weight

            user_hist = get_exact_history_fast(uid)
            sorted_user_hist = sorted(user_hist.items(), key=lambda x: day_order.get(x[0], 99))

            for day, stats in user_hist.items():
                weighted_daily_sums[day] = weighted_daily_sums.get(day, 0) + (stats["score"] * weight)

            if index == 0: topper_history_dict = {k: (int(v["score"]) if v["score"] % 1 == 0 else round(v["score"], 2)) for k, v in user_hist.items()}

            hist = rank_hist_dict.get(uid, [])
            lifetime_growth_text = "Calibrating..."
            
            if len(hist) > 0:
                curr_acc = (u_correct / u_attempts) * 100 if u_attempts > 0 else 0
                u_elo_val = user[8] if user[8] is not None else 1000
                
                if len(hist) == 1:
                    base_att = hist[0]['attempts']
                    base_corr = hist[0]['correct'] if hist[0]['correct'] else 0
                else:
                    base_att = hist[-1]['attempts'] + hist[-2]['attempts']
                    base_corr = (hist[-1]['correct'] if hist[-1]['correct'] else 0) + (hist[-2]['correct'] if hist[-2]['correct'] else 0)
                    
                base_acc = (base_corr / base_att) * 100 if base_att > 0 else 0
                accuracy_shift = curr_acc - base_acc
                elo_factor = (u_elo_val - 1000) / 10.0
                consistency_multiplier = 1.0 + (len(hist) * 0.05)
                
                raw_growth = (accuracy_shift + elo_factor) * consistency_multiplier
                
                if raw_growth > 0:
                    lifetime_growth_text = f"+{int(raw_growth)}%"

            # Fetch avatar URL only for top 50 competitors to keep cache baking instant
            user_avatar = get_telegram_avatar_url(uid) if index < 50 else None

            leaderboard_list.append({
                "rank": index + 1, "id": uid, "name": user[1], "score": u_score,
                "photo_url": user_avatar,
                "elo": round(user[8] if user[8] is not None else 1000, 1), 
                "last_updated": user[9] if user[9] else 0,
                "house": str(user[3]), "is_captain": user[4], "attempts": u_attempts, "league": user[6] if user[6] else 0,
                "lifetime_growth": lifetime_growth_text, 
                "rank_history": hist,
                "history": {
                    "labels": [k for k, v in sorted_user_hist], "scores": [(int(v["score"]) if v["score"] % 1 == 0 else round(v["score"], 2)) for k, v in sorted_user_hist],
                    "daily_correct": [v["correct"] for k, v in sorted_user_hist], "daily_attempts": [v["attempts"] for k, v in sorted_user_hist],
                    "accuracy": round((u_correct / u_attempts) * 100) if u_attempts > 0 else 0, "correct": u_correct, "wrong": max(0, u_attempts - u_correct)
                }
            })

        class_avg_history_dict = {day: round(weighted_daily_sums[day] / sum_weights_chart) if sum_weights_chart > 0 else 0 for day in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]}

        global RAM_CACHE
        RAM_CACHE["master_data"] = {
            "current_week": current_week_val, 
            "total_quizzes": total_quizzes_val, 
            "target_average": target_average,
            "total_active": len(top_users), # ✨ Pass the REAL total count
            "leaderboard": leaderboard_list, 
            "topper_history": topper_history_dict, 
            "class_avg_history": class_avg_history_dict, 
            "elo_ranking": elo_leaderboard
        }
        RAM_CACHE["last_bake_time"] = time.time()
        
    except Exception as e:
        print(f"🚨 Cache Bake Error: {e}")
    finally:
        if conn:
            try: c.close()
            except: pass
            release_db(conn)

# ==========================================
# BACKGROUND WORKER: QUIZ UNLOCK ANNOUNCEMENT (4:30 PM)
# ==========================================
def run_quiz_unlock_announcement():
    current_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    date_str = current_ist.strftime('%A, %d %b %Y')
    
    text = (
        f"🚨 **TODAY's QUIZZES ARE LIVE!** 🚨\n\n"
        f"📅 **{date_str}**\n\n"
        f"Today's Topic Trials have been officially unlocked. "
        f"Test your skills and secure your spot on the leaderboard before the weekly deadline!\n\n"
        f"👇 Tap below to begin your trials."
    )
    
    payload = {
        "chat_id": CHAT_ID,
        "message_thread_id": 2972,
        "text": text,
        "parse_mode": "Markdown",
        "reply_markup": {
            "inline_keyboard": [[
                {
                    "text": "⚡️ Access Quiz Here",
                    "url": "https://t.me/Ez_vocab_bot/leaderboard"
                }
            ]]
        }
    }

    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        
        # 1. Automatically Delete Yesterday's Announcement
        c.execute("SELECT value FROM bot_settings WHERE key='last_quiz_announcement_msg_id'")
        row = c.fetchone()
        if row and row[0]:
            old_msg_id = row[0]
            try:
                http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteMessage", json={
                    "chat_id": CHAT_ID,
                    "message_id": int(old_msg_id)
                }, timeout=5)
            except Exception as e:
                pass # Message might already be deleted manually

        # 2. Send the New Message
        for attempt in range(5):
            try:
                res = http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload, timeout=10)
                if res.status_code == 200:
                    new_msg_id = res.json()["result"]["message_id"]
                    
                    # (Pinning feature has been disabled)
                    
                    # 3. Save the New Message ID to the Database for tomorrow (so it can still auto-delete it!)
                    c.execute("""
                        INSERT INTO bot_settings (key, value) VALUES ('last_quiz_announcement_msg_id', %s)
                        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                    """, (str(new_msg_id),))
                    conn.commit()
                    
                    notify_prathu("📢 **4:30 PM Quiz Announcement** posted successfully!")
                    break
                elif res.status_code == 429:
                    time.sleep(res.json().get("parameters", {}).get("retry_after", 3) + 1)
                else:
                    time.sleep(2)
            except Exception as e:
                time.sleep(3)
                
    except Exception as e:
        notify_prathu(f"🚨 **ERROR (Quiz Announcement):** Failed to send 4:30 PM alert.\n`{e}`")
    finally:
        if conn:
            try: c.close()
            except: pass
            release_db(conn)

@app.route('/cron/quiz_announcement_0508', methods=['GET', 'POST'])
def trigger_quiz_announcement():
    threading.Thread(target=run_quiz_unlock_announcement).start()
    return "Quiz announcement triggered! Check Telegram.", 200

from flask import Response # type: ignore

@app.route('/api/leaderboard', methods=['GET'])
def get_mini_app_leaderboard():
    global RAM_CACHE

    # ✨ STRICT AUTH: No fallback.
    verified_user = get_verified_user()
    if not verified_user:
        return jsonify({"error": "Unauthorized"}), 401
    user_id = int(verified_user.get('id'))
    
    if not RAM_CACHE["master_data"]:
        with CACHE_LOCK:
            if not RAM_CACHE["master_data"]:
                try: bake_miniapp_cache()
                except Exception as e: return jsonify({"error": "Syncing..."}), 503

    master_data = RAM_CACHE.get("master_data")
    if not master_data:
        return jsonify({"error": "Syncing data, please refresh..."}), 503
        
    custom_leaderboard = []
    target_avg = master_data.get("target_average", 0)
    demotion_count = 0
    
    for index, u in enumerate(master_data["leaderboard"]):
        is_me = (u["id"] == user_id)
        is_promo = u["score"] >= target_avg
        
        if not is_promo:
            demotion_count += 1
            
        if is_promo or demotion_count <= 10 or is_me:
            custom_leaderboard.append({
                "rank": u["rank"],
                "id": u["id"],
                "name": u["name"],
                "score": u["score"],
                "photo_url": u.get("photo_url"),
                "elo": u["elo"],
                "house": u["house"],
                "is_captain": u["is_captain"],
                "attempts": u["attempts"],
                "league": u["league"],
                "lifetime_growth": u["lifetime_growth"],
                "last_updated": u["last_updated"],
                "history": {
                    "accuracy": u.get("history", {}).get("accuracy", 0),
                    "correct": u.get("history", {}).get("correct", 0),
                    "wrong": u.get("history", {}).get("wrong", 0)
                }
            })

    # --- EXTRACT OR CONSTRUCT CURRENT_USER DATA ---
    current_user_data = next((u for u in master_data["leaderboard"] if u["id"] == user_id), None)
    if current_user_data:
        # Attach caller's avatar directly
        current_user_data["photo_url"] = get_telegram_avatar_url(user_id)
    else:
        current_user_data = {
            "id": user_id,
            "name": "You",
            "score": 0,
            "photo_url": get_telegram_avatar_url(user_id),
            "rank": "N/A",
            "league": 0,
            "house": "🏳️ Unsorted",
            "is_captain": 0,
            "elo": 1000,
            "attempts": 0,
            "lifetime_growth": "Calibrating...",
            "rank_history": [],
            "history": {"labels": [], "scores": [], "accuracy": 0, "correct": 0, "wrong": 0}
        }

    # --- FIX: EXTRACT OR CONSTRUCT CURRENT_USER DATA ---
    current_user_data = next((u for u in master_data["leaderboard"] if u["id"] == user_id), None)
    if not current_user_data:
        current_user_data = {
            "id": user_id,
            "name": "You",
            "score": 0,
            "rank": "N/A",
            "league": 0,
            "house": "🏳️ Unsorted",
            "is_captain": 0,
            "elo": 1000,
            "attempts": 0,
            "lifetime_growth": "Calibrating...",
            "rank_history": [],
            "history": {"labels": [], "scores": [], "accuracy": 0, "correct": 0, "wrong": 0}
        }

    # ✨ RESTORED: The dictionary definition without the heavy Elo payload
    response_data = {
        "current_week": master_data["current_week"],
        "total_quizzes": master_data["total_quizzes"],
        "target_average": target_avg, 
        "total_active": master_data.get("total_active", len(master_data["leaderboard"])),
        "topper_history": master_data["topper_history"],
        "class_avg_history": master_data["class_avg_history"],
        "current_user": current_user_data, 
        "leaderboard": custom_leaderboard
    }

    res = Response(json.dumps(response_data), mimetype='application/json')
    res.headers["Cache-Control"] = "private, max-age=30"
    return res

@app.route('/api/elo', methods=['GET'])
def get_elo_ranking():
    global RAM_CACHE
    
    # ✨ STRICT AUTH: No fallback.
    verified_user = get_verified_user()
    if not verified_user:
        return jsonify({"error": "Unauthorized"}), 401
    user_id = int(verified_user.get('id'))
    
    if not RAM_CACHE.get("master_data"):
        return jsonify({"error": "Syncing data, please refresh..."}), 503

    master_data = RAM_CACHE.get("master_data")
    
    custom_elo = []
    # Send only top 50, plus the current user's rank
    for index, eu in enumerate(master_data.get("elo_ranking", [])):
        if index < 50 or eu["id"] == user_id:
            custom_elo.append(eu)
            
    # Extract current user's specific Elo data
    current_user_elo = next((eu for eu in custom_elo if eu["id"] == user_id), None)
    if not current_user_elo:
        # Fallback if unranked
        current_user_elo = {"id": user_id, "name": "You", "elo": 1000, "rank": "N/A", "is_active": True}

    res = Response(json.dumps({
        "current_user": current_user_elo,
        "elo_ranking": custom_elo
    }), mimetype='application/json')
    res.headers["Cache-Control"] = "private, max-age=30"
    return res
    
@app.route('/cron/refresh_snapshot_0508', methods=['GET', 'POST'])
def cron_refresh_snapshot():
    # 🔒 SECURITY GATE
    if request.headers.get("X-Cron-Secret") != CRON_SECRET:
        return "Unauthorized", 401

    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        # Check if Heavy Math actually ran and requested a snapshot
        c.execute("SELECT value FROM bot_settings WHERE key='needs_snapshot'")
        row = c.fetchone()
        
        if row and row[0] == '1':
            # Reset the flag so it doesn't run again until the next math cycle
            c.execute("UPDATE bot_settings SET value='0' WHERE key='needs_snapshot'")
            conn.commit()
            
            # Trigger the cache bake in the background
            threading.Thread(target=bake_miniapp_cache).start()
            msg = "RAM Snapshot refresh triggered in background!"
        else:
            msg = "No new math calculated. Snapshot skipped to save egress."
            
    except Exception as e:
        msg = f"Error: {e}"
    finally:
        if conn:
            try: c.close()
            except: pass
            release_db(conn)
            
    return msg, 200
    
def run_word_of_the_day():
    # ==========================================
    # PART 1: LIFETIME WORD OF THE DAY
    # ==========================================
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("SELECT word FROM lifetime_words")
        used_words = [row[0] for row in c.fetchall()]
    except Exception as e:
        used_words = []
    finally:
        c.close()
        release_db(conn)
    
    banned_words_text = ", ".join(used_words) if used_words else "None"

    wotd_prompt = f"""Select ONE "Word of the Day" from today's editorials.

Selection Rules:
• 🚫 LIFETIME BAN: DO NOT USE ANY OF THESE PREVIOUSLY USED WORDS: {banned_words_text}
• Choose a high-value word frequently seen in competitive exams.
• Prefer words that are moderately difficult—not everyday words, but not extremely rare.
• The word should be genuinely useful for editorial reading and RCs.
• ALWAYS use British English spelling for all output and synonyms.

Output exactly in this format:

📖 WORD OF THE DAY

<Word> (<Part of Speech>) <+ / − / = Connotation>

🔊 <Use simple English phonetic spelling only. Never use IPA symbols.>

💡 Think of
<Short memory trick (1–2 lines)>

📝 <Simple English meaning> (<Hindi meaning>)

🔄 <Synonym 1> • <Synonym 2> • <Synonym 3>

↔️ <Antonym 1> • <Antonym 2> • <Antonym 3>

📍 Where you'll hear it
<One short line explaining where this word commonly appears in editorials or competitive exams>

Connotation Guide:
+ = Positive
− = Negative
= = Neutral (descriptive)"""

    wotd_text = None
    successful_key_idx = 0 
    
    for idx, key in enumerate(API_KEYS):
        try:
            temp_client = genai.Client(api_key=key)
            response = temp_client.models.generate_content(model='gemini-3.7-flash', contents=wotd_prompt, config=types.GenerateContentConfig(temperature=0.5))
            if response.text:
                wotd_text = response.text.strip()
                successful_key_idx = idx
                break
        except: continue

    if wotd_text:
        try:
            lines = [line.strip() for line in wotd_text.split('\n') if line.strip()]
            extracted_word = lines[1].split(' ')[0].strip().lower()
            conn = get_db()
            c = conn.cursor()
            c.execute("INSERT INTO lifetime_words (word) VALUES (%s) ON CONFLICT (word) DO NOTHING", (extracted_word,))
            # --- MASTER SPEC: Save full text for Mini App ---
            c.execute("INSERT INTO bot_settings (key, value) VALUES ('latest_wotd_text', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (wotd_text,))
            conn.commit()
            c.close()
            release_db(conn)
            notify_prathu("📖 **Word of the Day** saved to Mini App successfully! (Telegram posts disabled)")
        except Exception as e:
            print(e)
    else:
        notify_prathu("🚨 **ERROR:** WOTD generation failed!")

    # ==========================================
    # PART 2: FOREIGN EXPRESSIONS
    # ==========================================
    time.sleep(5) 
    
    conn = get_db()
    c = conn.cursor()
    try:
        # 🟢 FIX: Use ORDER BY RANDOM() to grab 3 random unused expressions!
        c.execute("SELECT id, word FROM foreign_expressions WHERE is_used = FALSE ORDER BY RANDOM() LIMIT 3")
        foreign_batch = c.fetchall()
    except Exception as e:
        foreign_batch = []
    finally:
        c.close()
        release_db(conn)

    if len(foreign_batch) == 3:
        words_to_define = [row[1] for row in foreign_batch]
        words_ids = [row[0] for row in foreign_batch]
        words_string = ", ".join(words_to_define)

        foreign_prompt = f"""You are an expert linguistics AI. Define the following 3 foreign expressions commonly used in English literature and news editorials: {words_string}

Rules:
• ALWAYS use British English spelling.
• Do not add any introductory, acknowledging, or concluding text. 
• Strictly follow the formatting below.

Output EXACTLY in this format:

🌍 FOREIGN EXPRESSIONS

1️⃣ <Expression 1> (<Language of origin>)

🔊 <Simple English Pronunciation>

💡 <Short, simple meaning in English>.
(<Hindi meaning>)

📰 <Write one short human scenario or reaction in clear, natural English (approximately CEFR B1–B2). It MUST strictly reflect the specific political, economic, or social theme of the article.>

2️⃣ <Expression 2> (<Language of origin>)

🔊 <Simple English Pronunciation>

💡 <Short, simple meaning in English>.
(<Hindi meaning>)

📰 <Write one short human scenario or reaction in clear, natural English (approximately CEFR B1–B2). It MUST strictly reflect the specific political, economic, or social theme of the article.>

3️⃣ <Expression 3> (<Language of origin>)

🔊 <Simple English Pronunciation>

💡 <Short, simple meaning in English>.
(<Hindi meaning>)

📰 <Write one short human scenario or reaction in clear, natural English (approximately CEFR B1–B2). It MUST strictly reflect the specific political, economic, or social theme of the article.>"""

        foreign_text = None
        shifted_keys = API_KEYS[successful_key_idx + 1:] + API_KEYS[:successful_key_idx + 1]
        
        for key in shifted_keys:
            try:
                temp_client = genai.Client(api_key=key)
                response = temp_client.models.generate_content(model='gemini-3.7-flash', contents=foreign_prompt, config=types.GenerateContentConfig(temperature=0.3))
                if response.text:
                    foreign_text = response.text.strip()
                    break
            except: continue
            
        if foreign_text:
            try:
                conn = get_db()
                c = conn.cursor()
                c.execute("UPDATE foreign_expressions SET is_used = TRUE WHERE id IN %s", (tuple(words_ids),))
                # --- MASTER SPEC: Save full text for Mini App ---
                c.execute("INSERT INTO bot_settings (key, value) VALUES ('latest_foreign_text', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (foreign_text,))
                conn.commit()
                c.close()
                release_db(conn)
                notify_prathu("🌍 **Foreign Expressions** saved to Mini App successfully! (Telegram posts disabled)")
            except: pass
        else:
            notify_prathu("🚨 **ERROR:** Foreign Expressions AI generation failed!")
    elif len(foreign_batch) < 3:
        notify_prathu("🚨 **ALERT:** You are out of Foreign Expressions! The master list of 250 has been completed.")
        
@app.route('/word_of_the_day/0508', methods=['GET', 'POST'])
def trigger_word_of_the_day():
    threading.Thread(target=run_word_of_the_day).start()
    return "Word of the Day triggered!", 200

def background_approve_user(user_id):
    # ✨ NEW: Clean up the time bomb from the DB so they aren't declined!
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM bot_settings WHERE key = %s", (f"join_req_{user_id}",))
        
        # Automatically store the approved user in the database with today's timestamp!
        c.execute("""
            INSERT INTO users (user_id, first_name, joined_at)
            VALUES (%s, 'New Student', NOW())
            ON CONFLICT (user_id) DO NOTHING
        """, (user_id,))
        conn.commit()
        c.close()
        release_db(conn)
    except Exception as e:
        print(f"🚨 Error saving new member to database: {e}")

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/approveChatJoinRequest"
    payload = {
        "chat_id": CHAT_ID,
        "user_id": user_id
    }
    
    approved = False
    # 1. Try to approve the pending request safely with anti-spam retry logic
    for attempt in range(5):
        try:
            res = http_session.post(url, json=payload, timeout=10)
            if res.status_code == 200:
                approved = True
                break
            elif res.status_code == 429: # Telegram Rate Limit
                time.sleep(res.json().get("parameters", {}).get("retry_after", 3) + 1)
            else:
                # If it's a 400 error, it means the request expired or was already deleted by our 5-min cron!
                break
        except:
            time.sleep(2)
            
    # 2. Standard Welcome DM (if approval worked)
    welcome_text = (
        "🎉 **Entrance Trial Complete!**\n\n"
        "Congratulations, and welcome to the **Great Hall of Ez Editorials!** 🪄\n\n"
        "🛡️ **7-Day Probation Rule:**\n"
        "To stay in the group, complete at least **1 Quiz** OR read **1 Editorial Magazine** (tap 'Mark as Read') within your first 7 days.\n\n"
        "📅 **Daily Routine:**\n"
        "📰 **Morning:** Read the Daily Editorial PDFs in Thread 3.\n"
        "⚡ **4:30 PM:** Attempt the Daily Vocab & Topic Trials.\n"
        "🏆 **Sunday:** The Weekly Cup locks at midnight IST.\n\n"
        "Head over to the main group, say hello, and begin your journey! 🏛️"
    )

    # 3. ✨ THE NON-EXPIRING DM FIX: If the pending request was deleted by the 5-min cron, generate a one-time use invite link!
    if not approved:
        try:
            invite_res = http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/createChatInviteLink", json={
                "chat_id": CHAT_ID,
                "member_limit": 1 # Only allows 1 person to use this link (prevents sharing)
            }, timeout=10)
            
            if invite_res.status_code == 200:
                invite_link = invite_res.json().get("result", {}).get("invite_link")
                welcome_text = (
                    "🎉 **Entrance Trial Complete!**\n\n"
                    "You passed the test! However, your original join request expired.\n\n"
                    f"👉 **Click here to join the group:** {invite_link}\n\n"
                    "🛡️ **7-Day Probation Rule:**\n"
                    "To stay in the group, complete at least **1 Quiz** OR read **1 Editorial Magazine** (tap 'Mark as Read') within your first 7 days."
                )
        except Exception as e:
            print(f"🚨 Error generating invite link: {e}")
    
    # 4. Send the Final DM to the user
    try:
        http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={
            "chat_id": user_id,
            "text": welcome_text,
            "parse_mode": "Markdown"
        }, timeout=5)
    except:
        pass

@app.route('/api/approve_captcha', methods=['POST'])
def approve_captcha():
    data = request.get_json() or {}
    verified_user = get_verified_user()
    
    # Check HMAC-verified ID first, then fall back to body payload
    user_id = verified_user.get('id') if verified_user else data.get('user_id')

    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401

    try:
        user_id = int(user_id)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid user ID"}), 400

    # Run user approval in the background
    threading.Thread(target=background_approve_user, args=(user_id,)).start()

    return jsonify({"status": "success"}), 200

def is_github_file_updated_today(file_name, target_date_ist):
    """Verifies whether a file in the repo was committed today in IST."""
    try:
        url = f"https://api.github.com/repos/prathu-developer/exam-scraper-api/commits?path={file_name}&per_page=1"
        headers = {
            "Authorization": f"token {GITHUB_PAT}",
            "Accept": "application/vnd.github.v3+json"
        }
        res = http_session.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            commits = res.json()
            if commits and len(commits) > 0:
                commit_date_str = commits[0]["commit"]["committer"]["date"]
                # Parse ISO-8601 UTC and convert to IST (+5:30)
                commit_utc = datetime.strptime(commit_date_str, "%Y-%m-%dT%H:%M:%SZ")
                commit_ist = commit_utc + timedelta(hours=5, minutes=30)
                return commit_ist.date() == target_date_ist
    except Exception as e:
        print(f"⚠️ Failed to check commit date for {file_name}: {e}")
    return False

# ==========================================
# PHASE 2: MINI APP CONTENT INGESTION
# ==========================================
def run_mini_app_ingestion():
    import random
    
    current_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    today_date = current_ist.date()
    
    # 1. Stale File Guard: Abort if questions.json was not committed today
    if not is_github_file_updated_today("questions.json", today_date):
        notify_prathu(
            f"⚠️ **Mini App Ingestion Aborted:** `questions.json` on GitHub was not updated today (`{today_date}`). "
            f"Prevented ingestion of yesterday's quizzes."
        )
        return

    # 2. Timing Configuration (Instant drop upon ingestion; closes Sunday 11:59 PM)
    drop_time = current_ist
    days_until_sunday = 6 - current_ist.weekday()
    close_time = (current_ist + timedelta(days=days_until_sunday)).replace(hour=23, minute=59, second=59, microsecond=0)
    
    conn = None
    try:
        # 3. Fetch all JSONs from GitHub
        cache_buster = int(time.time())
        headers = {
            "Authorization": f"token {GITHUB_PAT}",
            "Accept": "application/vnd.github.v3.raw"
        }
        
        vocab_url = f"https://api.github.com/repos/prathu-developer/exam-scraper-api/contents/questions.json?ref=main&t={cache_buster}"
        grammar_url = f"https://api.github.com/repos/prathu-developer/exam-scraper-api/contents/grammar.json?ref=main&t={cache_buster}"
        advanced_url = f"https://api.github.com/repos/prathu-developer/exam-scraper-api/contents/comprehension_tests.json?ref=main&t={cache_buster}"
        
        # Safely fetch Vocab JSON
        try:
            vocab_resp = http_session.get(vocab_url, headers=headers, timeout=15)
            vocab_resp.raise_for_status()
            vocab_data = vocab_resp.json()
        except Exception as e:
            print(f"Vocab JSON Error: {e}")
            vocab_data = []
            
        # Safely fetch Grammar JSON
        try:
            grammar_resp = http_session.get(grammar_url, headers=headers, timeout=15)
            grammar_resp.raise_for_status()
            grammar_data = grammar_resp.json()
        except Exception as e:
            print(f"Grammar JSON Error: {e}")
            grammar_data = {}

        # Safely fetch Advanced JSON
        try:
            advanced_resp = http_session.get(advanced_url, headers=headers, timeout=15)
            advanced_resp.raise_for_status()
            advanced_data = advanced_resp.json()
        except Exception as e:
            print(f"Advanced JSON Error: {e}")
            advanced_data = {}
        
        if not isinstance(vocab_data, list):
            vocab_data = []

        set_a = grammar_data.get("set_a", []) if isinstance(grammar_data, dict) else []
        set_b = grammar_data.get("set_b", []) if isinstance(grammar_data, dict) else []
        set_c = grammar_data.get("set_c", []) if isinstance(grammar_data, dict) else []

        # --- ADVANCED JSON ADAPTER ---
        set_rc = []
        if "set_d" in advanced_data:
            passage = advanced_data["set_d"].get("passage", "")
            instruction = advanced_data["set_d"].get("instruction", "Directions: Read the following passage carefully and answer the questions given below.")
            for q in advanced_data["set_d"].get("questions", []):
                opts_list = q.get("options", [])
                corr_ans = q.get("correct_answer", "")
                if not corr_ans and opts_list: 
                    corr_ans = opts_list[0]

                set_rc.append({
                    "instruction": instruction,
                    "passage": passage,
                    "question": q.get('question', ''),
                    "options": opts_list,
                    "correct_answer": corr_ans,
                    "explanation": q.get("explanation", "")
                })

        set_cloze = []
        if "set_e" in advanced_data:
            passage = advanced_data["set_e"].get("passage", "")
            instruction = advanced_data["set_e"].get("instruction", "Directions: In the following passage, there are eight blanks. Choose the most appropriate option for each blank.")
            for q in advanced_data["set_e"].get("questions", []):
                opts_list = q.get("options", [])
                corr_ans = q.get("correct_answer", "")
                if not corr_ans and opts_list: 
                    corr_ans = opts_list[0]

                set_cloze.append({
                    "instruction": instruction,
                    "passage": passage,
                    "question": q.get('question', f"Which word fits in blank [{q.get('number', '')}]?"),
                    "options": opts_list,
                    "correct_answer": corr_ans,
                    "explanation": q.get("explanation", "")
                })

        set_pj = []
        if "set_f" in advanced_data:
            instruction = advanced_data["set_f"].get("instruction", "Directions: In the following question, six sentences are given. Sentence A is fixed in its correct position. The remaining five sentences need to be rearranged to form a coherent paragraph. Answer the questions that follow.")
            for q in advanced_data["set_f"].get("questions", []):
                sents = q.get("sentences", {})
                sent_text = "\n".join([f"{k}) {v}" for k, v in sents.items()]) if sents else q.get("passage", "")
                opts_list = q.get("options", [])
                corr_ans = q.get("correct_answer", "")
                if not corr_ans and opts_list: 
                    corr_ans = opts_list[0]
                
                set_pj.append({
                    "instruction": instruction,
                    "passage": sent_text,
                    "question": q.get("question", "Which of the following is the correct logical sequence?"),
                    "options": opts_list,
                    "correct_answer": corr_ans,
                    "explanation": q.get("explanation", "")
                })

        set_wu = []
        if "set_g" in advanced_data:
            instruction = advanced_data["set_g"].get("instruction", "Directions: Choose the sentence in which the given word is used correctly and appropriately.")
            for q in advanced_data["set_g"].get("questions", []):
                opts_list = q.get("options", [])
                corr_ans = q.get("correct_answer", "")
                if not corr_ans and opts_list: 
                    corr_ans = opts_list[0]
                
                set_wu.append({
                    "instruction": instruction,
                    "passage": "", 
                    "question": q.get('question', ''),
                    "options": opts_list,
                    "correct_answer": corr_ans,
                    "explanation": q.get("explanation", "")
                })

        # Shuffle questions where applicable
        random.shuffle(vocab_data)
        random.shuffle(set_a)
        random.shuffle(set_b)
        random.shuffle(set_c)
        random.shuffle(set_pj)
        random.shuffle(set_wu)

        # 4. Define the Quiz Sets
        quiz_configurations = [
            {"topic": "Vocab Quiz", "data": vocab_data, "duration": 600},
            {"topic": "Error Detection", "data": set_a, "duration": 300},
            {"topic": "Sentence Improvement", "data": set_b, "duration": 300},
            {"topic": "Fill in the Blank", "data": set_c, "duration": 240},
            {"topic": "Reading Comprehension", "data": set_rc, "duration": 600},
            {"topic": "Cloze Test", "data": set_cloze, "duration": 600},
            {"topic": "Para Jumbles", "data": set_pj, "duration": 600},
            {"topic": "Word Usage", "data": set_wu, "duration": 600}
        ]

        conn = get_db()
        c = conn.cursor()

        # Erase existing entries for today before re-ingesting
        c.execute("DELETE FROM quiz_sets WHERE quiz_day = %s", (today_date,))

        report_lines = []

        for config in quiz_configurations:
            q_list = config["data"]
            if not q_list:
                report_lines.append(f"⚠️ {config['topic']}: Skipped (0 questions found)")
                continue
                
            c.execute("""
                INSERT INTO quiz_sets (topic, quiz_day, question_count, duration_seconds, drop_time, close_time)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (config["topic"], today_date, len(q_list), config["duration"], drop_time, close_time))
            
            quiz_set_id = c.fetchone()[0]
            inserted_count = 0

            for mcq in q_list:
                options = mcq.get('options', [])
                correct_ans = mcq.get('correct_answer', '')
                
                if not options:
                    options = ["JSON Data Error A", "JSON Data Error B"]
                if not correct_ans:
                    correct_ans = options[0]
                
                if correct_ans not in options: 
                    options.append(correct_ans)
                
                random.shuffle(options)
                correct_index = options.index(correct_ans)
                
                q_text = mcq.get('custom_ui', f'Choose the best replacement for the words "{mcq.get("target_phrase", "")}".' if 'target_phrase' in mcq else mcq.get('question', ''))
                full_question = f"{q_text}\n\n{mcq.get('sentence', '')}".strip()

                meta_payload = {
                    "options": options,
                    "passage": mcq.get('passage', ''),
                    "instruction": mcq.get('instruction', '')
                }

                c.execute("""
                    INSERT INTO quiz_questions (quiz_set_id, question_text, options, correct_index, explanation)
                    VALUES (%s, %s, %s, %s, %s)
                """, (quiz_set_id, full_question, json.dumps(meta_payload), correct_index, mcq.get('explanation', '')))
                
                inserted_count += 1
                
            report_lines.append(f"✅ {config['topic']}: {inserted_count} questions inserted")

        conn.commit()
        report_text = "\n".join(report_lines)
        notify_prathu(f"🤖 **Mini App Ingestion Complete!**\n\n{report_text}")

        # 5. Trigger the announcement immediately after committing to the database
        run_quiz_unlock_announcement()

    except Exception as e:
        if conn: 
            conn.rollback()
        notify_prathu(f"🚨 **CRITICAL ERROR (Mini App Ingestion):**\n`{e}`")
    finally:
        if conn:
            try: c.close()
            except: pass
            release_db(conn)

# Manual Trigger for Phase 2 Testing
@app.route('/cron/ingest_miniapp_0508', methods=['GET', 'POST'])
def trigger_miniapp_ingestion():
    threading.Thread(target=run_mini_app_ingestion).start()
    return "Mini App Ingestion triggered! Check your Telegram DMs.", 200

# ==========================================
# SYSTEM SAFE MODE / MAINTENANCE HELPERS
# ==========================================
def is_maintenance():
    # Set this to "maintenance" in your Render Environment Variables to activate
    return os.environ.get("SYSTEM_MODE", "operational") == "maintenance"

def maintenance_block():
    if is_maintenance():
        return jsonify({
            "error": "SYSTEM_MAINTENANCE",
            "message": "The system is temporarily unavailable. No new quizzes can be started."
        }), 503
    return None

@app.route("/api/system-status", methods=["GET"])
def system_status():
    mode = os.environ.get("SYSTEM_MODE", "operational")
    return jsonify({
        "mode": mode,
        "maintenance": mode == "maintenance",
        "quiz_enabled": mode == "operational",
        "submissions_enabled": True, # ALWAYS True so active students can submit!
        "message": (
            "The platform is temporarily undergoing maintenance.\nActive quizzes can still be submitted, but new quizzes cannot be started."
            if mode == "maintenance" else None
        )
    }), 200

# ==========================================
# PHASE 3: MINI APP API ENDPOINTS (Part 1)
# ==========================================
from flask import jsonify # type: ignore

# --- MASTER SPEC: UPDATED /api/quiz/today ---
@app.route('/api/quiz/today', methods=['GET'])
def get_todays_quizzes():
    # ✨ FIX: Strict auth implementation
    verified_user = get_verified_user()
    if not verified_user:
        return jsonify({"error": "Unauthorized"}), 401
    user_id = int(verified_user["id"])
    
    current_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    
    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        
        # Fetch active quizzes using close_time
        c.execute("""
            SELECT id, topic, quiz_day, question_count, duration_seconds, drop_time, close_time 
            FROM quiz_sets 
            WHERE close_time > %s
            ORDER BY quiz_day DESC, id ASC
        """, (current_ist,))
        
        grouped_quizzes = {"Today": [], "Pending": []}
        
        for q_set in c.fetchall():
            set_id, topic, q_day, q_count, duration, drop_time, close_time = q_set
            drop_time = drop_time.replace(tzinfo=None)
            close_time = close_time.replace(tzinfo=None)
            
            attempted = False
            score = None
            attempt_id = None  
            
            if user_id:
                c.execute("SELECT id, score FROM quiz_attempts WHERE user_id = %s AND quiz_set_id = %s AND submitted_at IS NOT NULL", (user_id, set_id))
                attempt_row = c.fetchone()
                
                if attempt_row:
                    attempted = True
                    attempt_id = attempt_row[0] 
                    score = attempt_row[1]      
            
            if current_ist < drop_time: status = "locked"
            elif current_ist > close_time: status = "closed"
            elif attempted: status = "completed"
            else: status = "unlocked"
                
            quiz_data = {
                "id": set_id, "topic": topic, "question_count": q_count,
                "duration_seconds": duration, "drop_time": drop_time.isoformat(),
                "status": status, "score": score,
                "attempt_id": attempt_id,
                "day_num": q_day.weekday() + 1  
            }
            
            if q_day == current_ist.date():
                grouped_quizzes["Today"].append(quiz_data)
            elif q_day < current_ist.date():
                grouped_quizzes["Pending"].append(quiz_data)
        
        # 🟢 NEW: Check Editorial Read Status for Today
        has_read_editorial = False
        if user_id:
            # Get the exact UTC timestamp for 12:00 AM IST today
            midnight_epoch = (current_ist.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=5, minutes=30)).timestamp()
            
            # Check if there is a read receipt from this user since midnight
            c.execute("SELECT 1 FROM read_receipts WHERE user_id = %s AND created_at >= to_timestamp(%s)", (user_id, midnight_epoch))
            if c.fetchone():
                has_read_editorial = True
                
        return jsonify({
            "quizzes": grouped_quizzes,
            "has_read_editorial": has_read_editorial,
            "server_hour": current_ist.hour,
            "server_day": current_ist.weekday() # 0 = Monday, 6 = Sunday
        }), 200
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: release_db(conn)

# --- MASTER SPEC: NEW READ-ONLY ENDPOINTS ---
@app.route('/api/weekly-results', methods=['GET'])
def get_weekly_results():
    week_num = request.args.get('week_num', type=int)
    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        
        if not week_num:
            # Find the most recently completed week automatically
            c.execute("SELECT MAX(CAST(week_num AS INTEGER)) FROM weekly_rank_history")
            max_week_row = c.fetchone()
            week_num = max_week_row[0] if max_week_row and max_week_row[0] else None
            
        if not week_num:
            return jsonify({"results": [], "week_num": None}), 200
            
        c.execute("""
            SELECT h.rank, h.user_id, u.first_name, h.score, u.faction, u.league_tier, u.live_elo
            FROM weekly_rank_history h
            JOIN users u ON h.user_id = u.user_id
            WHERE h.week_num = %s
            ORDER BY h.rank ASC
            LIMIT 10
        """, (str(week_num),))
        
        results = []
        for row in c.fetchall():
            results.append({
                "rank": row[0],
                "id": row[1],
                "name": row[2],
                "score": row[3],
                "house": row[4],
                "league": row[5],
                "elo": row[6]
            })
            
        return jsonify({"results": results, "week_num": week_num}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: release_db(conn)

@app.route('/api/word-of-day', methods=['GET'])
def get_wotd():
    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT word FROM lifetime_words ORDER BY id DESC LIMIT 1")
        row = c.fetchone()
        return jsonify({"word_of_the_day": row[0] if row else "N/A"}), 200
    except Exception as e: return jsonify({"error": str(e)}), 500
    finally:
        if conn: release_db(conn)

@app.route('/api/upcoming-exams', methods=['GET'])
def get_upcoming_exams():
    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        # Removed the LIMIT so all active exams show in the app
        c.execute("SELECT name, status, display_date, exam_date, is_exact_date FROM upcoming_exams ORDER BY exam_date ASC")
        exams = [{"name": r[0], "status": r[1], "display_date": r[2], "exam_date": r[3], "is_exact": bool(r[4])} for r in c.fetchall()]
        return jsonify({"exams": exams}), 200
    except Exception as e: 
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: release_db(conn)

@app.route('/api/profile/update-target', methods=['POST'])
def update_target():
    data = request.get_json()
    verified_user = get_verified_user()
    if not verified_user:
        return jsonify({"error": "Unauthorized"}), 401
    user_id = int(verified_user.get('id'))
    state = data.get('state')
    exam = data.get('exam')
    
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400
        
    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("""
            UPDATE users 
            SET target_state = %s, target_exam = %s 
            WHERE user_id = %s
        """, (state, exam, user_id))
        conn.commit()
        return jsonify({"success": True}), 200
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: release_db(conn)

@app.route('/api/profile/me', methods=['GET'])
def get_profile():
    verified_user = get_verified_user()
    if not verified_user:
        return jsonify({"error": "Unauthorized"}), 401
    user_id = int(verified_user.get('id'))
    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("""
            SELECT first_name, weekly_score, live_elo, league_tier, weekly_correct, weekly_attempts 
            FROM users WHERE user_id = %s
        """, (user_id,))
        user_row = c.fetchone()
        
        if not user_row: return jsonify({"error": "User not found"}), 404
        
        name, score, elo, league, correct, attempts = user_row
        accuracy = round((correct / attempts) * 100) if attempts and attempts > 0 else 0
        
        return jsonify({
            "name": name, "score": score, "elo": elo, "league": league,
            "performance": {"accuracy": accuracy, "quiz_history_count": attempts}
        }), 200
    except Exception as e: return jsonify({"error": str(e)}), 500
    finally:
        if conn: release_db(conn)

@app.route('/api/quiz/start', methods=['POST'])
def start_quiz():
    blocked = maintenance_block()
    if blocked: 
        return blocked

    data = request.get_json() or {}
    verified_user = get_verified_user()
    if not verified_user:
        return jsonify({"error": "Unauthorized"}), 401
    user_id = int(verified_user.get('id'))

    quiz_set_id = data.get('quiz_set_id')
    is_practice = data.get('is_practice', False)
    
    if not user_id or not quiz_set_id:
        return jsonify({"error": "Missing user_id or quiz_set_id"}), 400
        
    current_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    
    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        
        c.execute("SELECT drop_time, close_time, duration_seconds FROM quiz_sets WHERE id = %s", (quiz_set_id,))
        quiz_meta = c.fetchone()
        if not quiz_meta:
            return jsonify({"error": "Quiz not found"}), 404
            
        drop_time, close_time, duration_seconds = quiz_meta
        drop_time = drop_time.replace(tzinfo=None)
        close_time = close_time.replace(tzinfo=None)
        
        if current_ist < drop_time or current_ist > close_time:
            return jsonify({"error": "Quiz is currently locked or closed"}), 403

        if is_practice:
            c.execute("SELECT id, question_text, options, correct_index, explanation FROM quiz_questions WHERE quiz_set_id = %s ORDER BY id ASC", (quiz_set_id,))
            questions = [{"question_id": q[0], "text": q[1], "options": q[2], "correct_index": q[3], "explanation": q[4]} for q in c.fetchall()]
            return jsonify({
                "attempt_id": "practice_mode",
                "duration_seconds": duration_seconds,
                "remaining_seconds": duration_seconds,
                "started_at": current_ist.isoformat(),
                "questions": questions
            }), 200
            
        # Check for existing attempts
        c.execute("SELECT id, submitted_at, started_at, score FROM quiz_attempts WHERE user_id = %s AND quiz_set_id = %s", (user_id, quiz_set_id))
        attempt_row = c.fetchone()
        
        if attempt_row:
            attempt_id, submitted_at, prev_started_at, prev_score = attempt_row
            
            # Case A: Already submitted
            if submitted_at is not None:
                return jsonify({
                    "completed": True,
                    "attempt_id": attempt_id,
                    "message": "Quiz already completed"
                }), 200
                
            # Case B: Incomplete attempt
            prev_started_at = prev_started_at.replace(tzinfo=None)
            time_elapsed = (current_ist - prev_started_at).total_seconds()
            
            # If expired while window was closed -> auto-finalize now
            if time_elapsed > duration_seconds:
                c.execute("""
                    UPDATE quiz_attempts 
                    SET submitted_at = %s, score = COALESCE(score, 0)
                    WHERE id = %s
                """, (current_ist, attempt_id))
                conn.commit()
                return jsonify({
                    "completed": True,
                    "attempt_id": attempt_id,
                    "message": "Time expired while away. Results generated."
                }), 200
                
            # Still within time limit -> resume with remaining seconds
            remaining_seconds = max(5, int(duration_seconds - time_elapsed))
        else:
            # Brand new attempt
            c.execute("""
                INSERT INTO quiz_attempts (user_id, quiz_set_id, started_at) 
                VALUES (%s, %s, %s) RETURNING id
            """, (user_id, quiz_set_id, current_ist))
            attempt_id = c.fetchone()[0]
            remaining_seconds = duration_seconds
            conn.commit()
        
        # Fetch Questions
        c.execute("SELECT id, question_text, options FROM quiz_questions WHERE quiz_set_id = %s ORDER BY id ASC", (quiz_set_id,))
        questions = [{"question_id": q[0], "text": q[1], "options": q[2]} for q in c.fetchall()]
        
        return jsonify({
            "attempt_id": attempt_id,
            "duration_seconds": duration_seconds,
            "remaining_seconds": remaining_seconds,
            "started_at": current_ist.isoformat(),
            "questions": questions
        }), 200
        
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: release_db(conn)

# ==========================================
# PHASE 3: MINI APP API ENDPOINTS (Part 2)
# ==========================================

@app.route('/api/quiz/submit', methods=['POST'])
def submit_quiz():
    verified_user = get_verified_user()
    if not verified_user:
        return jsonify({"error": "Unauthorized"}), 401
    verified_user_id = int(verified_user["id"])

    data = request.get_json() or {}
    attempt_id = data.get('attempt_id')
    user_responses = data.get('responses', [])

    if not attempt_id:
        return jsonify({"error": "Missing attempt_id"}), 400

    if not acquire_submission_lock(attempt_id):
        return jsonify({"error": "Submission is already being processed."}), 409

    current_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    current_day_str = current_ist.strftime('%a')
    
    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        
        # 1. Fetch Attempt & Validate Ownership
        c.execute("""
            SELECT a.user_id, a.quiz_set_id, a.started_at, a.submitted_at, s.duration_seconds
            FROM quiz_attempts a
            JOIN quiz_sets s ON a.quiz_set_id = s.id
            WHERE a.id = %s
        """, (attempt_id,))
        attempt_meta = c.fetchone()
        
        if not attempt_meta:
            if redis_client: redis_client.delete(f"lock:submit:{attempt_id}")
            return jsonify({"error": "Attempt not found"}), 404
            
        user_id, quiz_set_id, started_at, submitted_at, duration_seconds = attempt_meta
        
        if user_id != verified_user_id:
            if redis_client: redis_client.delete(f"lock:submit:{attempt_id}")
            return jsonify({"error": "Unauthorized"}), 403
            
        # Return success immediately if already submitted
        if submitted_at is not None:
            return jsonify({"success": True, "message": "Already submitted"}), 200
            
        # 2. Fetch All Correct Answers in One Query
        c.execute("SELECT id, correct_index FROM quiz_questions WHERE quiz_set_id = %s", (quiz_set_id,))
        correct_map = {row[0]: row[1] for row in c.fetchall()}
        
        total_score = 0.0
        responses_to_insert = []
        poll_inserts = []
        user_answer_inserts = []
        
        for r in user_responses:
            q_id = r.get('question_id')
            s_idx = r.get('selected_index')
            
            if q_id not in correct_map:
                continue
                
            is_correct = False
            if s_idx is not None:
                if s_idx == correct_map[q_id]:
                    is_correct = True
                    total_score += 1.0
                else:
                    total_score -= 0.25
                    
            responses_to_insert.append((attempt_id, q_id, s_idx, is_correct))
            
            if s_idx is not None:
                poll_inserts.append((str(q_id), correct_map[q_id], current_day_str))
                user_answer_inserts.append((user_id, str(q_id), int(is_correct), current_day_str, s_idx))
            
        # 3. Batch Inserts (Eliminates loops with multiple individual database queries)
        if responses_to_insert:
            c.executemany("""
                INSERT INTO quiz_responses (attempt_id, question_id, selected_index, is_correct)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, responses_to_insert)

        if poll_inserts:
            c.executemany("""
                INSERT INTO polls (poll_id, correct_index, poll_day) 
                VALUES (%s, %s, %s)
                ON CONFLICT (poll_id) DO NOTHING
            """, poll_inserts)

        if user_answer_inserts:
            c.executemany("""
                INSERT INTO user_answers (user_id, poll_id, is_correct, poll_day, chosen_option)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id, poll_id) DO UPDATE SET 
                    is_correct = EXCLUDED.is_correct, 
                    chosen_option = EXCLUDED.chosen_option
            """, user_answer_inserts)
        
        # 4. Finalize Attempt
        c.execute("""
            UPDATE quiz_attempts 
            SET submitted_at = %s, score = %s
            WHERE id = %s
        """, (current_ist, total_score, attempt_id))
        
        conn.commit()
        return jsonify({"success": True, "score": total_score}), 200
        
    except Exception as e:
        if conn: conn.rollback()
        if redis_client: redis_client.delete(f"lock:submit:{attempt_id}")
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: release_db(conn)


@app.route('/api/quiz/result/<int:attempt_id>', methods=['GET'])
def get_quiz_result(attempt_id):
    # ✨ FIX: Strict Auth Implementation
    verified_user = get_verified_user()
    if not verified_user:
        return jsonify({"error": "Unauthorized"}), 401
    verified_user_id = int(verified_user["id"])

    conn = None
    try:
        conn = get_db()
        c = conn.cursor()

        # ✨ FIX: Verify attempt ownership first
        c.execute("SELECT user_id FROM quiz_attempts WHERE id = %s", (attempt_id,))
        attempt_owner = c.fetchone()
        if not attempt_owner or attempt_owner[0] != verified_user_id:
            return jsonify({"error": "Unauthorized"}), 403
        
        # 1. SQL-Side Aggregation: Rank & Percentile (Faster time breaks ties!)
        c.execute("""
            WITH ranks AS (
                SELECT id, score,
                       RANK() OVER (ORDER BY score DESC, (submitted_at - started_at) ASC) as rank_val,
                       PERCENT_RANK() OVER (ORDER BY score DESC, (submitted_at - started_at) ASC) as pct_val,
                       COUNT(*) OVER () as total_participants,
                       EXTRACT(EPOCH FROM (submitted_at - started_at)) as time_taken,
                       quiz_set_id
                FROM quiz_attempts
                WHERE quiz_set_id = (SELECT quiz_set_id FROM quiz_attempts WHERE id = %s)
                  AND submitted_at IS NOT NULL
            )
            SELECT rank_val, pct_val, total_participants, score, time_taken, quiz_set_id 
            FROM ranks WHERE id = %s
        """, (attempt_id, attempt_id))
        
        rank_row = c.fetchone()
        if not rank_row:
            return jsonify({"error": "Result not found or not submitted yet"}), 404
            
        rank_val, pct_val, total_participants, score, time_taken, quiz_set_id = rank_row
        
        # 2. Get Community Stats for the Review Pills
        c.execute("""
            SELECT question_id, 
                   COUNT(selected_index) as total_attempts, 
                   SUM(CASE WHEN is_correct THEN 1 ELSE 0 END) as total_correct
            FROM quiz_responses
            WHERE question_id IN (SELECT id FROM quiz_questions WHERE quiz_set_id = %s)
            GROUP BY question_id
        """, (quiz_set_id,))
        q_stats = {row[0]: {"attempts": row[1], "correct": row[2]} for row in c.fetchall()}

        c.execute("""
            SELECT AVG(EXTRACT(EPOCH FROM (submitted_at - started_at))) 
            FROM quiz_attempts 
            WHERE quiz_set_id = %s AND submitted_at IS NOT NULL
        """, (quiz_set_id,))
        avg_quiz_time = c.fetchone()[0]
        avg_quiz_time = float(avg_quiz_time) if avg_quiz_time else 0
        
        c.execute("SELECT question_count FROM quiz_sets WHERE id = %s", (quiz_set_id,))
        q_count_row = c.fetchone()
        q_count = q_count_row[0] if q_count_row and q_count_row[0] > 0 else 1
        est_time_per_q = int(avg_quiz_time / q_count)

        # 3. Get Sectional Summary & Explanations
        c.execute("""
            SELECT q.id, q.question_text, q.options, q.correct_index, q.explanation, 
                   r.selected_index, r.is_correct
            FROM quiz_questions q
            LEFT JOIN quiz_responses r ON q.id = r.question_id AND r.attempt_id = %s
            WHERE q.quiz_set_id = %s
            ORDER BY q.id ASC
        """, (attempt_id, quiz_set_id))
        
        correct_count = 0
        wrong_count = 0
        unattempted_count = 0
        question_details = []
        
        for row in c.fetchall():
            q_id, text, options, c_idx, exp, s_idx, is_corr = row
            
            if s_idx is None:
                unattempted_count += 1
            elif is_corr:
                correct_count += 1
            else:
                wrong_count += 1
                
            g_att = q_stats.get(q_id, {}).get("attempts", 0)
            g_cor = q_stats.get(q_id, {}).get("correct", 0)
            g_acc = round((g_cor / g_att) * 100) if g_att > 0 else 0
                
            question_details.append({
                "question_id": q_id,
                "text": text,
                "options": options,
                "correct_index": c_idx,
                "explanation": exp,
                "user_selected_index": s_idx,
                "is_correct": is_corr,
                "global_accuracy": g_acc,        # 🟢 NEW DATA
                "global_avg_time": est_time_per_q  # 🟢 NEW DATA
            })
        
       # Calculate Accuracy %
        total_attempted = correct_count + wrong_count
        accuracy = (correct_count / total_attempted * 100) if total_attempted > 0 else 0
        
        # 3. Fetch Top 10 Leaderboard for this specific quiz
        c.execute("""
            SELECT u.first_name, a.score, EXTRACT(EPOCH FROM (a.submitted_at - a.started_at)) as time_taken, a.user_id
            FROM quiz_attempts a
            JOIN users u ON a.user_id = u.user_id
            WHERE a.quiz_set_id = %s AND a.submitted_at IS NOT NULL
            ORDER BY a.score DESC, (a.submitted_at - a.started_at) ASC
            LIMIT 10
        """, (quiz_set_id,))
        
        top_10_list = []
        for r_row in c.fetchall():
            top_10_list.append({
                "name": r_row[0],
                "score": float(r_row[1]),
                "time_taken": int(r_row[2]),
                "user_id": r_row[3]
            })
        
        return jsonify({
            "quiz_set_id": quiz_set_id,  # 🟢 ADD THIS EXACT LINE HERE
            "summary": {
                "score": score,
                "rank": rank_val,
                "total_participants": total_participants,
                # ✨ FIX: Invert the SQL rank so the topper gets 100% and lowest gets 0%
                "percentile": round((1.0 - pct_val) * 100, 1),
                "accuracy": round(accuracy, 1),
                "time_spent_seconds": int(time_taken),
                "correct": correct_count,
                "wrong": wrong_count,
                "unattempted": unattempted_count
            },
            "solutions": question_details,
            "top_10": top_10_list
        }), 200
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: release_db(conn)

# ==========================================
# PHASE 4: MINI APP API ENDPOINTS (Part 3)
# ==========================================

# --- MASTER SPEC: READ-ONLY CONTENT ENDPOINTS ---
@app.route('/api/foreign-expressions', methods=['GET'])
def get_foreign_expressions():
    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT word FROM foreign_expressions WHERE is_used = TRUE ORDER BY id DESC LIMIT 1")
        row = c.fetchone()
        return jsonify({"expression": row[0] if row else "N/A"}), 200
    except Exception as e: return jsonify({"error": str(e)}), 500
    finally:
        if conn: release_db(conn)

@app.route('/api/progress/me', methods=['GET'])
def get_my_progress():
    verified_user = get_verified_user()
    if not verified_user:
        return jsonify({"error": "Unauthorized"}), 401
    user_id = int(verified_user.get('id'))
    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        # Fetch real quiz attempts grouped by day for the Progress Tab
        c.execute("""
            SELECT s.topic, s.quiz_day, a.score, a.started_at 
            FROM quiz_attempts a
            JOIN quiz_sets s ON a.quiz_set_id = s.id
            WHERE a.user_id = %s AND a.submitted_at IS NOT NULL
            ORDER BY a.started_at DESC LIMIT 15
        """, (user_id,))
        
        history = []
        for row in c.fetchall():
            history.append({
                "topic": row[0],
                "date": row[1].strftime('%A, %d %b'),
                "score": row[2]
            })
            
        return jsonify({"history": history}), 200
    except Exception as e: return jsonify({"error": str(e)}), 500
    finally:
        if conn: release_db(conn)

@app.route('/api/digest/today', methods=['GET'])
def get_daily_digest():
    verified_user = get_verified_user()
    if not verified_user:
        return jsonify({"error": "Unauthorized"}), 401
    user_id = int(verified_user.get('id'))
    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        
        c.execute("SELECT value FROM bot_settings WHERE key = 'latest_wotd_text'")
        wotd_row = c.fetchone()
        wotd_content = wotd_row[0] if wotd_row else "Check back later for today's word!"
        
        c.execute("SELECT value FROM bot_settings WHERE key = 'latest_foreign_text'")
        fe_row = c.fetchone()
        fe_content = fe_row[0] if fe_row else "Check back later for today's expressions!"
        
        current_date = (datetime.utcnow() + timedelta(hours=5, minutes=30)).date()
        
        # Check Read Status
        c.execute("""
            SELECT content_type FROM content_read_status 
            WHERE user_id = %s AND content_date = %s
        """, (user_id, current_date))
        read_types = [r[0] for r in c.fetchall()]
        
        digest_items = [
            {
                "id": "wotd",
                "title": "📖 Word of the Day",
                "content": wotd_content,
                "is_read": "wotd" in read_types
            },
            {
                "id": "foreign",
                "title": "🌍 Foreign Expressions",
                "content": fe_content,
                "is_read": "foreign" in read_types
            }
        ]
        
        return jsonify({"items": digest_items}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: release_db(conn)

@app.route('/api/digest/mark-read', methods=['POST'])
def mark_digest_read():
    data = request.get_json()
    verified_user = get_verified_user()
    if not verified_user:
        return jsonify({"error": "Unauthorized"}), 401
    user_id = int(verified_user.get('id'))
    content_type = data.get('content_type')
    
    if not user_id or not content_type:
        return jsonify({"error": "Missing data"}), 400
        
    current_date = (datetime.utcnow() + timedelta(hours=5, minutes=30)).date()
    
    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("""
            INSERT INTO content_read_status (user_id, content_type, content_date, read_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (user_id, content_type, content_date) DO NOTHING
        """, (user_id, content_type, current_date))
        conn.commit()
        return jsonify({"success": True}), 200
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: release_db(conn)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
