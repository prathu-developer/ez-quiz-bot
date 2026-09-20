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

import secrets

def get_verified_user():
    """
    Validates either:
    1. Telegram Mini App initData (X-Telegram-Init-Data)
    2. Web / Android App Session Token (Authorization: Bearer <token>)
    """
    # 1. Check for Website or Android App session token first
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1].strip()
        if redis_client:
            try:
                cached_user = redis_client.get(f"session:{token}")
                if cached_user:
                    # Refresh session expiration (30 days)
                    redis_client.expire(f"session:{token}", 30 * 86400)
                    return json.loads(cached_user)
            except Exception as e:
                print(f"⚠️ Redis session error: {e}")

    # 2. Check for Telegram Mini App initData
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    if not init_data:
        return None

    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)
        
        if not received_hash:
            return None

        data_check_string = "\n".join(f"{key}={parsed[key]}" for key in sorted(parsed))
        secret_key = hmac.new(b"WebAppData", TELEGRAM_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(calculated_hash, received_hash):
            return None

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
    'gemini-3.1-flash-lite',
    'gemini-2.5-flash-lite'
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
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Telegram-Init-Data, X-Cron-Secret, X-Magazine-Secret'
    
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
BOT_ID = int(TELEGRAM_TOKEN.split(':')[0]) if (TELEGRAM_TOKEN and ':' in TELEGRAM_TOKEN) else None
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

def process_ai_query(chat_id, user_id, first_name, text, message_id, thread_id, replied_text=None, is_reply_to_bot=False):
    text_clean = (text or "").strip()
    if not text_clean:
        return

    text_lower = text_clean.lower()
    ADMIN_IDS = [716496729, 5103843488, 6251430317]
    is_admin = user_id in ADMIN_IDS

    # 1. Check if explicitly summoned or continuing a direct conversation with the bot
    is_explicitly_summoned = (
        is_reply_to_bot or
        "lixie" in text_lower or
        "@ez_vocab_bot" in text_lower or
        text_lower.startswith("bot") or
        "hey bot" in text_lower or
        "hi bot" in text_lower
    )

    # 2. Comprehensive Academic, Platform, Doubt & Orientation Detection
    doubt_keywords = [
        "what", "how", "when", "why", "where", "which", "who", "whom", "whose",
        "can you", "could you", "would you", "tell me", "please explain", "explain",
        "meaning", "synonym", "antonym", "definition", "vocab", "vocabulary", "word", "words",
        "idiom", "idioms", "phrase", "phrases", "grammar", "rule", "rules", "error", "correction",
        "cloze", "para jumble", "sentence improvement", "filler", "fillers", "comprehension",
        "editorial", "passage", "doubt", "doubts", "clarify", "clarification",
        "help", "question", "difference between", "distinction", "nuance",
        "is it", "does it", "should i", "correct", "wrong", "false", "solution",
        "rank", "score", "cutoff", "cut-off", "cut off", "points", "marks", "elo",
        "rating", "exam", "exams", "quiz", "quizzes", "poll", "polls", "test", "tests",
        "mock", "schedule", "timetable", "drop", "pdf", "magazine", "attendance",
        "mini app", "app", "login", "streak",
        "utilize", "utilise", "use this group", "how to use", "how to start", "new student",
        "new member", "new here", "guide", "guidance", "routine", "roadmap", "overview",
        "what about", "how about", "why not", "why so", "and if", "any exception",
        "give example", "another example", "what if", "can it be", "could it be",
        "is that so", "agreed", "disagree", "elaborate", "trick", "shortcut", "mnemonic"
    ]
    is_asking_doubt = "?" in text_lower or any(word in text_lower for word in doubt_keywords)

    # 3. Known query/support topics
    is_query_thread = thread_id in [12082, 12103, 12105, 3, 2972, 10123]

    # Filters:
    # - Admins: don't interrupt admin announcements or instructions. Only respond if explicitly summoned or asking an actual question ('?' in text)
    if is_admin and not is_explicitly_summoned and not ("?" in text_clean and is_asking_doubt):
        return

    # - If replying to another member's message: only respond if explicitly summoned or asking an academic/platform doubt
    if replied_text and not is_explicitly_summoned and not is_asking_doubt:
        return

    # - General group chatter: must be explicitly summoned, asking a doubt, or posting in a query thread
    if not is_explicitly_summoned and not is_asking_doubt and not is_query_thread:
        return

    # 4. Fair Per-User Rate Limiting (Prevents spam without freezing the whole thread/chat)
    cooldown_seconds = 2 if is_explicitly_summoned else 4
    if redis_client:
        if check_and_set_cooldown(f"rate:lixie:user:{user_id}", cooldown_seconds):
            return
    else:
        global LAST_AI_REPLY_TIME_THREADS
        current_time = time.time()
        user_key = f"u_{user_id}"
        if current_time - LAST_AI_REPLY_TIME_THREADS.get(user_key, 0) < cooldown_seconds:
            return
        LAST_AI_REPLY_TIME_THREADS[user_key] = current_time

    # 5. Extract Live Thread History Memory
    global THREAD_HISTORY
    t_key = thread_id if thread_id is not None else "main"
    if t_key not in THREAD_HISTORY:
        THREAD_HISTORY[t_key] = deque(maxlen=12)

    recent_history_list = list(THREAD_HISTORY[t_key])
    conversation_history_str = ""
    # Exclude the current message from history string if already appended in webhook
    prior_turns = recent_history_list[:-1] if len(recent_history_list) > 1 else []
    if prior_turns:
        conversation_history_str = "\n[LIVE THREAD CONVERSATION HISTORY (RECENT TURNS)]\n" + "\n".join(prior_turns[-8:]) + "\n"

    current_ist_time = datetime.utcnow() + timedelta(hours=5, minutes=30)
    current_day = current_ist_time.strftime('%A')
    phase_of_week = "Active Competition"
    if current_day == "Monday" and (current_ist_time.hour < 10 or (current_ist_time.hour == 10 and current_ist_time.minute < 30)):
        phase_of_week = "Monday Pre-Game (Scores are reset to 0. The first quiz drops at 10:30 AM today.)"
    elif current_day == "Sunday" and current_ist_time.hour >= 13:
        phase_of_week = "Sunday Post-Deadline (Quizzes are over, waiting for the official Monday morning reset.)"

    total_quizzes_available = 0
    total_active_participants = 0
    exam_context = ""
    db_key_index = 0

    try:
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
    except Exception:
        pass

    reply_context = f"\n=== DIRECT REPLY CONTEXT ===\nThe user is directly replying to this message in Telegram:\n\"{replied_text}\"\n" if replied_text else ""

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

    # Thread-specific instructions
    thread_instruction = ""
    if thread_id == 12082:
        thread_instruction = """
        [CURRENT THREAD: 🐞 BUG REPORTS]
        Goal: Diagnose and log issues concisely (1-2 sentences).
        - If crucial info is missing, politely ask (e.g., "Which day/set and question number did this happen on?").
        - If clear report or follow-up details: Acknowledge and confirm it is logged for the developers.
        - Never ignore follow-up details from a user.
        """
    elif thread_id == 12103:
        thread_instruction = """
        [CURRENT THREAD: 🛟 HELP & SUPPORT]
        Goal: Resolve navigation, Mini App access, quiz timing (10:30 AM IST), score/rating, or attendance queries in 1-3 direct lines.
        - Give exact button/tab name (e.g., "Open the Mini App via 🏆 Rankings & Quizzes thread and check the Progress tab.").
        """
    elif thread_id == 12105:
        thread_instruction = """
        [CURRENT THREAD: 💡 FEATURE REQUESTS]
        Goal: Acknowledge community suggestions warmly in 1-2 lines.
        - E.g.: "Noted! Thank you for the suggestion—we'll keep this in mind for future app updates."
        """
    elif thread_id == 3:
        thread_instruction = """
        [CURRENT THREAD: 📰 TODAY'S EDITORIALS]
        Goal: Answer doubts related to "Today's Editorials" PDF magazine (expected every morning between 06:00 AM and 10:30 AM IST, Mon-Sat).
        Help students analyze editorial vocabulary, idioms, tone, or comprehension questions.
        """
    elif thread_id == 2972:
        thread_instruction = """
        [CURRENT THREAD: 🏆 RANKINGS & QUIZZES]
        Goal: Answer queries regarding the daily 10:30 AM IST quiz drops, Mini App trials, leaderboard rules, scoring, and Elo rating. Note: Quiz drop time is strictly 10:30 AM IST (morning), not evening or 4:30 PM.
        """

    system_prompt = f"""
    You are Lixie, the official AI learning mentor and moderator of Ez Editorials — an exam-oriented English learning platform for Indian competitive exam aspirants (Banking, SSC, Regulatory, UPSC, State PSCs).

    =========================================
    PERSONA & MODERATOR RULES (STRICT BREVITY)
    =========================================
    - Role: Sharp community moderator + clever, innovative English mentor.
    - Tone: Witty, grounded, razor-sharp, zero conversational fluff.
    - Format: Never start with robotic filler (e.g., "Certainly!", "I'd be happy to help", "Here is a breakdown"). Jump directly into the answer.
    - Length Limits:
      * Quick query / single word meaning / greeting -> 1 to 2 concise lines max.
      * Grammar, vocabulary, or concept doubts -> 3 to 5 punchy lines (max 40-70 words). Use the micro-format (⚡ Quick Trick -> 🎯 Exam Application -> 💡 Memory Anchor).
      * Platform, group guide, or schedule questions -> Concise bullet points or 2 to 4 punchy lines.
      * Never write long, bookish, comprehensive essays! Keep it fast, clever, and easy to digest on a mobile screen.

    =========================================
    HOW TO UTILISE THIS GROUP (NEW STUDENT GUIDE)
    =========================================
    When a student asks how to utilise/use this group, where to start, or what the routine is, explain this 4-step daily system clearly:
    1. 📰 Read "Today's Editorials" (Thread 3): Drops every morning between 06:00 AM and 10:30 AM IST (Mon–Sat). Read the PDF magazine and tap "Mark as Read" to log attendance.
    2. ⚡ 10:30 AM Daily Topic Trials (Thread 2972 / Mini App): Daily timed tests drop every morning at 10:30 AM IST (Vocab Quiz + changing grammar/RC sets). Attempt them inside the Mini App.
    3. 🏆 Leaderboard & Elo Rating: Compete in the Weekly Cup, maintain your accuracy, and climb Elo tiers (Base 1000). Scoring above class average promotes you.
    4. 💬 English Doubts & Discussion (Thread 11): Ask any vocabulary, grammar, or editorial doubt in this main chat.
    * Retention Rules: Read ≥ 1 magazine or attempt ≥ 1 quiz in your first 7 days (7-day probation), and ≥ 4 magazines or 50 quizzes every 30 days to stay in the group. Sunday is a rest day (no magazine, no quizzes).

    =========================================
    CRITICAL TIMINGS & NOMENCLATURE
    =========================================
    - Editorial Magazine Name: Strictly called "Today's Editorials" (drops in Thread 3).
    - Magazine Arrival Window: Expected every morning between 06:00 AM and 10:30 AM IST (Monday to Saturday exclusively. No Sunday issue).
    - Quiz Drop Time: Strictly 10:30 AM IST (Morning, NOT 4:30 PM, NOT 10:30 PM, NOT evening).
    - Sunday: Rest day — no editorial magazine and no quiz drops. Weekly leaderboard locks at Sunday midnight IST.

    =========================================
    CLEVER & INNOVATIVE ENGLISH MENTOR (ZERO BOOKISH JARGON)
    =========================================
    Aspirants preparing for competitive exams hate dry, heavy, textbook grammar rules. Any study or English doubt response MUST be clever, smart, quick, and innovative:
    - ZERO BOOKISH JARGON: Never recite dry academic grammar definitions (e.g., avoid "transitive subjunctive clause", "nominative absolute"). Speak human, exam-smart English.
    - SHORT TRICKS & MENTAL SHORTCUTS FIRST: Give students the instant hack they can use in an exam under 5 seconds:
      * Who vs Whom: Substitute He/Him (He = Who, Him = Whom: "Who called?" -> He called. "To [whom/him] did you give it?").
      * Affect vs Effect: RAVEN (Remember: Affect is Verb, Effect is Noun).
      * Lay vs Lie: Lay = placing an object down (Lay the book; hens lay eggs); Lie = reclining/resting oneself (Lie down to sleep).
      * Parallelism: Match the grammatical rhythm (-ing with -ing, to-verb with to-verb).
      * Subject-Verb Agreement with 'Neither...nor' / 'Either...or': The verb hugs its closest subject ("Neither the teacher nor the students ARE...").
      * Few vs A Few / Little vs A Little: 'Few/Little' = negative (almost none/barely any); 'A few / A little' = positive (at least some/helpful amount).
      * Hard vs Hardly: 'Hard' = with intense effort; 'Hardly' = almost not at all.
      * Each / Every / Either: Always treated as singular in exam questions!
    - MICRO-ANSWER FORMAT (Max 3 to 5 lines total):
      ⚡ Quick Trick / Mental Rule: The 1-line mental shortcut or mnemonic.
      🎯 Exam Application: Right vs Wrong contrast showing why the tempting trap fails.
      💡 Memory Anchor: A crisp takeaway that sticks forever.
    - BRITISH ENGLISH: Always use British spelling and grammar conventions (analyse, colour, rigour, practise as verb, practice as noun).
    - NO OPTION LABELS (A/B/C/D): Question options are randomized in the Mini App. Never say "Option A is correct." Always refer to the actual word or phrase.

    =========================================
    MULTI-PARTY CONVERSATION CONTINUITY & ADMIN FOLLOW-UP
    =========================================
    - Live Multi-Party Flow: This is an active Telegram study group. Students talk with each other, and Admins often step in to guide, correct, or provide hints.
    - Seamless Contextual Follow-Up: ALWAYS inspect [LIVE THREAD CONVERSATION HISTORY (RECENT TURNS)] and [DIRECT REPLY CONTEXT]. When a student asks a follow-up ("what about this?", "why not the second one?", "and if it's plural?", "why is that wrong?"), resolve pronouns ("it", "this", "that rule") from the recent messages. NEVER say "Could you please provide the question or sentence?" if it was already mentioned in the recent turns!
    - Admin Integration & Respect: Messages marked "Admin (Name)" come from community leaders. If an admin steps into the middle of a discussion or gives advice/hints, seamlessly acknowledge and build upon their point (e.g., "Building on what Admin mentioned..."). Never contradict an admin or act like their message didn't happen.
    - Natural Conversational Dialogue: Jump into the thread like a sharp, witty co-mentor following the conversation in real-time, picking up right where the last message left off.

    =========================================
    ECOSYSTEM MAP & LIVE CONTEXT
    =========================================
    - Current Day: {current_day} | IST Time: {current_ist_time.strftime('%I:%M %p')}
    - Weekly Phase: {phase_of_week}
    - Active Challengers: {total_active_participants}

    [Today's 10:30 AM Test Schedule]
    {today_schedule}

    [Weekly Timetable Overview]
    • Mon & Thu: RC (1 Passage • 8Q)
    • Tue & Fri: Para Jumbles (5 Sets)
    • Wed & Sat: Cloze Test (1 Passage • 8Q)
    • Daily (Mon-Sat): Vocab Quiz (15Q) drops every day alongside changing grammar sets (Error Detection, Fillers, Sentence Improvement, Word Usage).
    • Sunday: No quiz drops. Leaderboard locks at midnight IST.

    [Community Rules & Threads]
    • 📰 Today's Editorials (Thread 3): Mon-Sat morning PDFs (06:00–10:30 AM IST) with attendance buttons. No Sunday issues.
    • 🏆 Rankings & Quizzes (Thread 2972): 10:30 AM drop notifications and Mini App links.
    • 📊 Cut-offs & Promotion: Scoring above the Class Average promotes a student; below causes demotion.
    • 🧠 Elo Rating: Lifetime rating (1000 base) tracking accuracy across difficulty tiers.
    • 🧹 Purge Rule: Students must read at least 4 Magazines OR complete 50 Quizzes every 30 days.
    • 📚 Grammar 101 / Word 101: Archived courses. Never promise new drops.
    • QUIZ & EDITORIAL ECOSYSTEM ROUTING:
        - Morning (06:00–10:30 AM IST): "Today's Editorials" PDFs drop in Thread 3. Students must tap "Mark as Read".
        - Morning (10:30 AM IST): 5 Daily Topic Trial sets drop inside the Mini App (accessible via Thread 2972 or Bot Menu).
        - If a user asks where polls or quizzes are, reply concisely:
            "Daily quizzes have moved from Telegram polls to our interactive Mini App for timed test practice and solutions! Read your morning PDF in Thread 3, then tap below to attempt today's 10:30 AM trials."

    [Upcoming Exams]
    {exam_context}

    {thread_instruction}

    [User Interacting]
    - Name: {first_name} (Admin: {is_admin}, Explicitly Addressed: {is_explicitly_summoned})
    {reply_context}
    {conversation_history_str}

    =========================================
    RESPONSE VS SILENCE RULES
    =========================================
    1. DIRECT ENGAGEMENT (NEVER IGNORE):
       If the user explicitly addresses you (summoned "Lixie", tagged the bot, or replied to your message), you MUST ALWAYS respond. Even for casual greetings ("Hi Lixie", "Good morning"), reply warmly and briefly (1 sentence). NEVER output IGNORE when directly addressed.
    2. GENUINE DOUBTS & QUERIES:
       If the message asks an English doubt, grammar rule, vocabulary question, platform query, or exam question, answer directly, accurately, and concisely.
    3. SILENCE DIRECTIVE:
       Output ONLY the single word: IGNORE
       ONLY when human members are casually bantering with each other, exchanging personal greetings without addressing you, sharing random stickers/emojis, or when no question or assistance is requested.
    """

    if not API_KEYS:
        return

    ai_reply = None
    safety_settings = [
        types.SafetySetting(
            category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
            threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH,
        ),
        types.SafetySetting(
            category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
            threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH,
        ),
        types.SafetySetting(
            category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,
            threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH,
        ),
        types.SafetySetting(
            category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
            threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH,
        ),
    ]

    for model_name in AI_MODELS:
        if ai_reply:
            break

        for attempt in range(len(API_KEYS)):
            try:
                active_key = API_KEYS[db_key_index % len(API_KEYS)]
                temp_client = genai.Client(api_key=active_key)
                response = temp_client.models.generate_content(
                    model=model_name,
                    contents=text_clean,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        temperature=0.3,
                        safety_settings=safety_settings
                    )
                )
                if response.text and response.text.strip():
                    ai_reply = response.text.strip()
                    break
            except Exception:
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
                continue

    if not ai_reply:
        return

    clean_check = ai_reply.strip().strip('"\'`.*_').upper()
    if clean_check == "IGNORE":
        return

    # Record bot answer in memory
    THREAD_HISTORY[t_key].append(f"Lixie: {ai_reply}")

    send_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": ai_reply,
        "parse_mode": "Markdown",
        "reply_to_message_id": message_id
    }
    if thread_id:
        payload["message_thread_id"] = thread_id

    for attempt in range(5):
        try:
            res = http_session.post(send_url, json=payload, timeout=12)
            if res.status_code == 200:
                break
            # Fallback to plain text if Telegram Markdown parsing fails
            if res.status_code == 400 and ("parse" in res.text.lower() or "entity" in res.text.lower()):
                payload.pop("parse_mode", None)
                res = http_session.post(send_url, json=payload, timeout=12)
                if res.status_code == 200:
                    break
            elif res.status_code == 429:
                time.sleep(res.json().get("parameters", {}).get("retry_after", 3) + 1)
            else:
                time.sleep(2)
        except requests.exceptions.RequestException:
            time.sleep(2)

def process_support_threads(chat_id, user_id, first_name, text, message_id, thread_id, replied_text=None, is_reply_to_bot=False):
    """Delegates support thread queries to the thread-aware process_ai_query engine."""
    process_ai_query(
        chat_id=chat_id,
        user_id=user_id,
        first_name=first_name,
        text=text,
        message_id=message_id,
        thread_id=thread_id,
        replied_text=replied_text,
        is_reply_to_bot=is_reply_to_bot
    )

def process_read_receipt(cb_id, user_id, first_name, message_id):
    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        
        # 1. Double-Tap Protection
        c.execute("SELECT 1 FROM read_receipts WHERE message_id=%s AND user_id=%s", (message_id, user_id))
        if c.fetchone():
            if cb_id:
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
        if cb_id:
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
        
        # 🟢 UPDATED: Pointing to Cloudflare Pages deployment with direct user ID tracking!
        target_chat_id = join_req.get('chat', {}).get('id') or CHAT_ID
        captcha_url = f"https://ez-editorials-app.pages.dev/captcha?uid={user_id}"
        markup = {
            "inline_keyboard": [
                [{"text": "⚡️ Complete Entrance Trial (3Q)", "web_app": {"url": captcha_url}}],
                [{"text": "🌐 Open in Browser (If Trial doesn't open)", "url": captcha_url}]
            ]
        }
        
        # 1. Native Pop-up (Supported Telegram clients when query_id is present)
        if query_id:
            try:
                # Standard Bot API method: sendChatJoinRequestWebApp takes chat_join_request_query_id and web_app_url
                res_native = http_session.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendChatJoinRequestWebApp",
                    json={
                        "chat_join_request_query_id": str(query_id),
                        "web_app_url": captcha_url
                    },
                    timeout=5
                )
                print(f"[JoinRequest] sendChatJoinRequestWebApp status: {res_native.status_code}, body: {res_native.text}")
            except Exception as e:
                print(f"[JoinRequest] sendChatJoinRequestWebApp exception: {e}")

        # 2. Direct DM with both Mini App and Browser fallback buttons (using reliable HTML formatting)
        first_name = join_req.get('from', {}).get('first_name', 'Student')
        welcome_text = (
            f"👋 <b>Welcome to Ez Editorials, {first_name}!</b>\n\n"
            "We have received your request to join the Great Hall.\n\n"
            "To ensure our community remains a high-quality environment for serious learners, we ask all new members to complete a quick, 3-question English Entrance Trial.\n\n"
            "Tap below to prove your skills and instantly gain access to the group! 🪄"
        )
        try:
            res_dm = http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={
                "chat_id": user_chat_id,
                "text": welcome_text,
                "reply_markup": markup,
                "parse_mode": "HTML"
            }, timeout=8)
            print(f"[JoinRequest] Backup DM status: {res_dm.status_code}, body: {res_dm.text}")
        except Exception as e:
            print(f"[JoinRequest] Backup DM sendMessage exception: {e}")
            
        # Store in DB with 24-hour grace period so students are never prematurely declined!
        expire_time = int(time.time()) + 86400 
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
            text = msg.get('text', '')

            # 🟢 Check if user tapped a Login Deep Link (e.g. /start login_a3f81e7b9c12)
            if text.startswith('/start login_'):
                login_code = text.split('login_')[1].strip()
                if redis_client and redis_client.exists(f"auth_code:{login_code}"):
                    user_data = {
                        "id": user_id,
                        "first_name": first_name,
                        "username": msg['from'].get('username', '')
                    }
                    # Save user info into code key for 60 seconds so polling catches it
                    redis_client.set(f"auth_code:{login_code}", json.dumps(user_data), ex=60)
                    
                    # Send instant confirmation in chat
                    http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={
                        "chat_id": user_id,
                        "text": f"✅ **Login Confirmed!**\n\nYou are now signed into **Ez Editorials**. Return to the app or browser to continue.",
                        "parse_mode": "Markdown"
                    })
                    return 'OK', 200

            # Default private chat welcome flow
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

            # --- LIXIE AI MULTI-THREAD INTELLIGENT ENGINE ---
            if chat_type in ['group', 'supergroup'] and not text.startswith('/'):
                if str(chat_id) == CHAT_ID:
                    reply_msg = msg.get('reply_to_message')
                    replied_text = None
                    is_reply_to_bot = False
                    if reply_msg:
                        replied_text = reply_msg.get('text') or reply_msg.get('caption')
                        replied_from = reply_msg.get('from', {})
                        if (BOT_ID and replied_from.get('id') == BOT_ID) or (
                            replied_from.get('is_bot') and str(replied_from.get('username', '')).lower() in ['ez_vocab_bot', 'lixie']
                        ):
                            is_reply_to_bot = True

                    user_info = msg.get('from', {})
                    user_id = user_info.get('id')
                    first_name = user_info.get('first_name', 'Student')

                    # Maintain live multi-party conversation thread memory
                    t_key = thread_id if thread_id is not None else "main"
                    if t_key not in THREAD_HISTORY:
                        THREAD_HISTORY[t_key] = deque(maxlen=12)
                    speaker_role = "Admin" if user_id in [716496729, 5103843488, 6251430317] else "Student"
                    THREAD_HISTORY[t_key].append(f"{speaker_role} ({first_name}): {text.strip()}")

                    threading.Thread(target=process_ai_query, kwargs={
                        "chat_id": chat_id,
                        "user_id": user_id,
                        "first_name": first_name,
                        "text": text,
                        "message_id": msg['message_id'],
                        "thread_id": thread_id,
                        "replied_text": replied_text,
                        "is_reply_to_bot": is_reply_to_bot
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
        "📝 Attempt at least 50 Questions OR\n"
        "📖 Read 4 Magazines (using the new 'Mark as Read' button)\n\n"
        "⏳ **Every 30 Days**\n\n"
        "❗️ Members who remain inactive for 30 days will be removed to make room for new students and keep the community active."
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
    """Copies message from Thread 271 directly to the channel with styled inline buttons."""
    copy_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/copyMessage"
    
    payload = {
        "chat_id": TARGET_CHANNEL_ID,
        "from_chat_id": SOURCE_CHAT_ID,
        "message_id": source_msg_id,
        "reply_markup": {
            "inline_keyboard": [[
                {
                    "text": "📖 Magazine",
                    "url": "https://t.me/ezeditorialgroup/3"
                },
                {
                    "text": "⚡ Quizzes",
                    "url": "https://t.me/Ez_vocab_bot"
                }
            ]]
        }
    }
    
    try:
        res = http_session.post(copy_url, json=payload, timeout=10)
        if res.status_code == 200:
            new_msg_id = res.json()["result"]["message_id"]

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
        else:
            print(f"⚠️ Telegram API error on copyMessage: {res.status_code} - {res.text}")
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

    # Approved in DB even if non-Telegram or external user
    return is_in_db, (is_group_member or is_in_db)

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
                            [{"text": "🎯 Topic Quiz (10:30 AM)", "url": "https://t.me/Ez_vocab_bot/leaderboard"}]
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
# BACKGROUND WORKER: QUIZ UNLOCK ANNOUNCEMENT (10:30 AM)
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
                    
                    notify_prathu("📢 **10:30 AM Quiz Announcement** posted successfully!")
                    break
                elif res.status_code == 429:
                    time.sleep(res.json().get("parameters", {}).get("retry_after", 3) + 1)
                else:
                    time.sleep(2)
            except Exception as e:
                time.sleep(3)
                
    except Exception as e:
        notify_prathu(f"🚨 **ERROR (Quiz Announcement):** Failed to send 10:30 AM alert.\n`{e}`")
    finally:
        if conn:
            try: c.close()
            except: pass
            release_db(conn)


from flask import Response # type: ignore


    
    
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
            response = temp_client.models.generate_content(model='gemini-3.8-flash', contents=wotd_prompt, config=types.GenerateContentConfig(temperature=0.5))
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
                response = temp_client.models.generate_content(model='gemini-3.8-flash', contents=foreign_prompt, config=types.GenerateContentConfig(temperature=0.3))
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
        "🎉 <b>Entrance Trial Complete!</b>\n\n"
        "Congratulations, and welcome to the <b>Great Hall of Ez Editorials!</b> 🪄\n\n"
        "🛡️ <b>7-Day Probation Rule:</b>\n"
        "To stay in the group, complete at least <b>1 Quiz</b> OR read <b>1 Editorial Magazine</b> (tap 'Mark as Read') within your first 7 days.\n\n"
        "📅 <b>Daily Routine:</b>\n"
        "📰 <b>Morning:</b> Read the Daily Editorial PDFs in Thread 3.\n"
        "⚡ <b>10:30 AM:</b> Attempt the Daily Vocab & Topic Trials.\n"
        "🏆 <b>Sunday:</b> The Weekly Cup locks at midnight IST.\n\n"
        "Head over to the main group, say hello, and begin your journey! 🏛️"
    )

    # 3. ✨ THE NON-EXPIRING DM FIX: If the pending request was deleted by the cron, generate a one-time use invite link!
    if not approved:
        try:
            invite_res = http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/createChatInviteLink", json={
                "chat_id": CHAT_ID,
                "member_limit": 1 # Only allows 1 person to use this link (prevents sharing)
            }, timeout=10)
            
            if invite_res.status_code == 200:
                invite_link = invite_res.json().get("result", {}).get("invite_link")
                welcome_text = (
                    "🎉 <b>Entrance Trial Complete!</b>\n\n"
                    "You passed the test! However, your original join request expired.\n\n"
                    f"👉 <b><a href=\"{invite_link}\">Click here to join the group</a></b>\n\n"
                    "🛡️ <b>7-Day Probation Rule:</b>\n"
                    "To stay in the group, complete at least <b>1 Quiz</b> OR read <b>1 Editorial Magazine</b> (tap 'Mark as Read') within your first 7 days."
                )
        except Exception as e:
            print(f"🚨 Error generating invite link: {e}")
    
    # 4. Send the Final DM to the user
    try:
        http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={
            "chat_id": user_id,
            "text": welcome_text,
            "parse_mode": "HTML"
        }, timeout=5)
    except Exception as e:
        print(f"Failed to send final DM: {e}")


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


# ==========================================
# PHASE 3: MINI APP API ENDPOINTS (Part 1)
# ==========================================
from flask import jsonify # type: ignore

# --- MASTER SPEC: UPDATED /api/quiz/today ---

# --- MASTER SPEC: NEW READ-ONLY ENDPOINTS ---






# ==========================================
# PHASE 3: MINI APP API ENDPOINTS (Part 2)
# ==========================================




# ==========================================
# PHASE 4: MINI APP API ENDPOINTS (Part 3)
# ==========================================

# --- MASTER SPEC: READ-ONLY CONTENT ENDPOINTS ---




# ==========================================
# MULTI-PLATFORM AUTHENTICATION (WEB & ANDROID)
# ==========================================






# 🟢 GOOGLE PLAY COMPLIANCE: Account Deletion Endpoint


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)


# ==============================================================================
# 🟢 FLASK BLUEPRINTS REGISTRATION (Sprint 0: Modularization)
# ==============================================================================
from routes.api_auth import auth_bp
from routes.api_quiz import quiz_bp
from routes.api_profile import profile_bp
from routes.api_leaderboard import leaderboard_bp
from routes.bot_cron import cron_bp
from routes.api_magazine import magazine_bp

app.register_blueprint(auth_bp)
app.register_blueprint(quiz_bp)
app.register_blueprint(profile_bp)
app.register_blueprint(leaderboard_bp)
app.register_blueprint(cron_bp)
app.register_blueprint(magazine_bp)

# Backward-compatibility re-exports for route handlers
from routes.api_auth import (
    auth_telegram_widget, request_login_code, delete_account,
    verify_login_code, approve_captcha, system_status, admin_approve_pending_joins
)
from routes.api_quiz import (
    get_todays_quizzes, start_quiz, submit_quiz, get_quiz_result, add_poll
)
from routes.api_profile import (
    get_profile, update_target, get_my_progress, get_upcoming_exams,
    get_daily_digest, mark_digest_read, get_wotd, get_foreign_expressions
)
from routes.api_leaderboard import (
    get_mini_app_leaderboard, get_elo_ranking, get_weekly_results
)
from routes.bot_cron import (
    trigger_daily_purge, trigger_daily_reset, trigger_weekly_reset,
    cron_process_leaderboard, cron_heavy_math, cron_update_telegram_text,
    trigger_dispatcher, trigger_sunday_announcement, trigger_daily_vocab,
    trigger_countdown_update, trigger_quiz_announcement, cron_refresh_snapshot,
    trigger_word_of_the_day, trigger_miniapp_ingestion
)
from routes.api_magazine import (
    ingest_magazine_edition, get_magazine_week, get_magazine_day, get_magazine_read_status,
    sync_read_receipt
)
