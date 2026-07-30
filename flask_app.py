import threading
import os
from flask import Flask, request, render_template, jsonify
from flask_compress import Compress  # ✨ 1. Import Compress
import requests
import time
import json
from datetime import datetime, timedelta
import psycopg2
from psycopg2 import pool
from google import genai
from google.genai import types

# --- DATABASE CONFIGURATION ---
# We use environment variables so your password isn't exposed on GitHub
DB_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:FlUVu8dA8xy02woL@db.wuhoozvbufnwjsfpkojp.supabase.co:5432/postgres")

# High-speed connection pool to handle massive group traffic instantly
db_pool = psycopg2.pool.ThreadedConnectionPool(1, 8, DB_URL)

CRON_SECRET = os.environ.get("CRON_SECRET", "Ez_Master_Key_77")

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
API_KEYS = [
    "AIzaSyDb5THxDk58CrdPJ7nVKJov6gL87_G2hQ0g",
    "AQ.Ab8RN6L9w5YfLZE350_jxGvWS7NYJnTeYVoRH6yHoZlIDMnP1A",
    "AQ.Ab8RN6LTnLSW4d1ge6MHadn7YTOO1z608dB9ulQ8qNG3EOJHdw"
]

current_key_index = 0
LAST_AI_REPLY_TIME = 0  # ✨ NEW: Tracks Lixie's cooldown directly in local RAM!
app = Flask(__name__)
Compress(app)

# ✨ NEW: The High-Speed Tunnel to Telegram and GitHub
http_session = requests.Session()

# --- IN-MEMORY CACHE TO SAVE BANDWIDTH ---
RAM_CACHE = {
    "master_data": None,
    "last_bake_time": 0
}
CACHE_LOCK = threading.Lock() # ✨ NEW: Protects Render from Cache Stampedes
POLL_CACHE = {} # ✨ NEW: Caches poll correct options in RAM

TELEGRAM_TOKEN = "8730359477:AAE4D3_koGNb6EHv40muYod79mV03JEntOQ"
CHAT_ID = "-1003875580290"
LIVE_MESSAGE_ID = 2662 
ADD_DB_KEY = "X19712006"
TELEGRAM_THREAD_ID = '2972'
ANNOUNCEMENT_THREAD_ID = 11 

SOURCE_CHAT_ID = "-1004333232429" 
THREAD_MAPPING = {
    6: 5, 4: 2343, 2: 3, 11: 7438, 86: 11, 88: 824,
}

COUNTDOWN_THREAD_ID = 6539
COUNTDOWN_MESSAGE_ID = 6542 

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

# ✨ FIX: Add queue_id as the second parameter
def process_answer(c, queue_id, user_id, first_name, poll_id, chosen_option): 
    max_retries = 3
    
    for attempt in range(max_retries):
        try:
            c.execute("SELECT 1 FROM user_answers WHERE user_id=%s AND poll_id=%s", (user_id, poll_id))
            if c.fetchone():
                # If they already answered, delete the duplicate ticket from the queue
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

            c.execute("SELECT faction FROM users WHERE user_id=%s", (user_id,))
            current_faction_row = c.fetchone()
            current_faction = current_faction_row[0] if current_faction_row else None

            if current_faction is None:
                houses = ['Gryffindor 🦁🔥', 'Slytherin 🐍💧', 'Ravenclaw 🦅💨', 'Hufflepuff 🦡🌍']
                counts = {}
                for h in houses:
                    c.execute("SELECT COUNT(*) FROM users WHERE faction=%s", (h,))
                    counts[h] = c.fetchone()[0]
                assigned_faction = min(counts, key=counts.get)
                c.execute("UPDATE users SET faction=%s WHERE user_id=%s", (assigned_faction, user_id))

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

    c.execute("SELECT faction, SUM(weekly_score) FROM users WHERE faction IS NOT NULL GROUP BY faction")
    team_scores = dict(c.fetchall())

    for house in ['Gryffindor 🦁🔥', 'Slytherin 🐍💧', 'Ravenclaw 🦅💨', 'Hufflepuff 🦡🌍']:
        if house not in team_scores:
            team_scores[house] = 0

    sorted_houses = sorted(team_scores.items(), key=lambda x: x[1], reverse=True)
    top_house, top_score = sorted_houses[0]
    second_score = sorted_houses[1][1]

    house_abbrev = {
        'Gryffindor 🦁🔥': '🦁 Gryffindor',
        'Slytherin 🐍💧': '🐍 Slytherin',
        'Ravenclaw 🦅💨': '🦅 Ravenclaw',
        'Hufflepuff 🦡🌍': '🦡 Hufflepuff'
    }

    house_text = ""
    medals_house = ["🥇", "🥈", "🥉", "4️⃣"]
    for i, (house, score) in enumerate(sorted_houses):
        abbrev = house_abbrev.get(house, "🏳️ Unknown")
        clean_score = int(score) if score % 1 == 0 else round(score, 2)
        house_text += f"{medals_house[i]} {abbrev} — {clean_score} pts\n"

    if top_score > second_score:
        margin = top_score - second_score
        clean_margin = int(margin) if margin % 1 == 0 else round(margin, 2)
        house_name_only = top_house.split()[0]
        lead_text = f"🏆 {house_name_only} leads the House Cup by {clean_margin} pts!"
    elif top_score > 0 and top_score == second_score:
        lead_text = "⚖️ The House Cup is currently tied!"
    else:
        lead_text = "⚖️ No points have been earned yet!"

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
    msg_text = "🏰 **THE BATTLE FOR THE HOUSE CUP** 🏰\n"
    msg_text += f"📅 {date_range} | 👥 {total_active} Active Students\n"
    msg_text += f"⏳ {phase_text}\n"
    msg_text += "━━━━━━━━━━━━━━━━━━━━\n\n"

    msg_text += "⚔️ **HOUSE WAR**\n\n"
    msg_text += f"{house_text}\n"
    msg_text += f"{lead_text}\n"
    msg_text += "━━━━━━━━━━━━━━━━━━━━\n\n"

    msg_text += "📊 **COMMUNITY PULSE**\n\n"
    msg_text += f"➪ Quizzes Released: {total_quizzes}\n"
    msg_text += f"➪ Maximum Score: {max_pts} pts\n"
    msg_text += f"➪ Promotion Cut-off: {target_average} pts\n"
    msg_text += f"➪ Safe Zone: {safe_zone_count} students\n"
    msg_text += f"➪ Global Accuracy: {global_accuracy_pct}%\n"
    msg_text += "━━━━━━━━━━━━━━━━━━━━\n\n"

    msg_text += "🏆 **TOP 10 WIZARDS & WITCHES**\n\n"

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

    for i, user in enumerate(top_10):
        u_id, name, score, faction_val, is_captain = user
        faction_val = str(faction_val)
        clean_score = int(score) if score % 1 == 0 else round(score, 2)

        if "Gryffindor" in faction_val: faction_emoji = "🦁"
        elif "Slytherin" in faction_val: faction_emoji = "🐍"
        elif "Ravenclaw" in faction_val: faction_emoji = "🦅"
        elif "Hufflepuff" in faction_val: faction_emoji = "🦡"
        else: faction_emoji = "🏳️"

        captain_emoji = "🪄 " if is_captain == 1 else ""
        msg_text += f"{medals[i]} {faction_emoji} {captain_emoji}[{name}](tg://user?id={u_id}) ➪ {clean_score} pts\n"

    msg_text += "\n━━━━━━━━━━━━━━━━━━━━"

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText"

    payload = {
        "chat_id": CHAT_ID,
        "message_id": 7628,
        "text": msg_text,
        "parse_mode": "Markdown",
        "reply_markup": {
            "inline_keyboard": [[
                {
                    "text": "💡 See All Ranking",
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

    # --- ✨ BUILD ELO LEADERBOARD MESSAGE (MESSAGE 10948) ---
    elo_msg_text = "🏆 **CLASS TOPPERS LEADERBOARD** 🏆\n"
    elo_msg_text += "*(Based on Global Elo Rating)*\n"
    elo_msg_text += "━━━━━━━━━━━━━━━━━━━━\n\n"

    elo_medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

    if not top_10_elo:
        elo_msg_text += "No active students found this week.\n"
    else:
        for i, user in enumerate(top_10_elo):
            u_id, name, elo = user
            # ✨ Changed to round to the nearest whole number for display
            clean_elo = int(round(elo))
            elo_msg_text += f"{elo_medals[i]} [{name}](tg://user?id={u_id}) ➪ {clean_elo} Elo\n"

    elo_msg_text += "\n━━━━━━━━━━━━━━━━━━━━"

    elo_payload = {
        "chat_id": CHAT_ID,
        "message_id": 10948,
        "text": elo_msg_text,
        "parse_mode": "Markdown",
        "reply_markup": {
            "inline_keyboard": [[
                {
                    "text": "📊 View Full Elo Leaderboard",
                    "url": "https://t.me/Ez_vocab_bot/leaderboard?startapp=elo"
                }
            ]]
        }
    }

    for attempt in range(max_retries):
        try:
            res = http_session.post(url, json=elo_payload, timeout=10)
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
    doubt_keywords = ["what", "how", "when", "why", "where", "can you", "explain", "meaning", "synonym", "antonym", "rank", "score", "cutoff", "exam", "quiz"]
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

    global LAST_AI_REPLY_TIME
    current_time = time.time()
    if current_time - LAST_AI_REPLY_TIME < 10:
        return
    LAST_AI_REPLY_TIME = current_time

    current_ist_time = datetime.utcnow() + timedelta(hours=5, minutes=30)
    current_day = current_ist_time.strftime('%A')
    phase_of_week = "Active Competition"
    if current_day == "Monday" and current_ist_time.hour < 19:
        phase_of_week = "Monday Pre-Game (Scores are reset to 0. The first quiz drops at 7:00 PM tonight.)"
    elif current_day == "Sunday" and current_ist_time.hour >= 13:
        phase_of_week = "Sunday Post-Deadline (Quizzes are over, waiting for the official Monday morning reset.)"

    total_quizzes_available = 0
    total_active_participants = 0
    exam_context = ""

    try:
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

    system_prompt = f"""
    You are Lixie, the official Moderator+ AI for the "Ez Editorials" Telegram community (6,200+ members).
    Your persona: A helpful, witty, and highly intelligent senior student monitoring the "💬 Members Discussion/Feedback" thread. Drop all robotic formality; speak naturally like a real person using texting shortcuts and emojis.

    =========================================
    MODULE 1: LIVE SYSTEM STATE & USER CONTEXT
    =========================================
    - Current Day: {current_day}
    - Current IST Time: {current_ist_time.strftime('%I:%M %p')}
    - Phase of the Week: {phase_of_week}
    - Active Participants This Week: {total_active_participants}
    - Total Quizzes Dropped: {total_quizzes_available}
    
    [User Interacting with You]
    - Name: {first_name}
    - Is Admin: {"True" if is_admin else "False"}

    =========================================
    MODULE 2: COMMUNITY THREAD MAP (KNOWLEDGE BASE)
    =========================================
    Direct members to these specific topics based on their needs:
    1. ‼️ Admin Notice / Info: Official announcements and updates from the admins.
    2. 🔥 Vocab Drill (25Q): Drops daily at 7:00 PM. Tests vocabulary derived from editorials.
    3. 🎃 Topic Drill (15Q): Focused practice sets (e.g., Grammar, Fillers, Error Detection).
    4. 🎭 Live Weekly-Cup Leaderboard: Real-time standings, cut-off points, and Mini App access.
    5. 📝 Today's Editorials Magazine: Daily PDFs dropped (Mon-Sat) between 10:00 AM - 11:59 AM.
    6. 💬 Members Discussion/Feedback: The chat thread you are currently monitoring.
    7. 💎 Words 101: "THE DAILY DISPATCH / FIELD NOTES" PDFs are uploaded here.
    8. 📰 Editor's Pick: Selected original-form editorials/articles for extended reading.
    9. 🏅 Weekly-Cup Results: Final standings, winners, and house captain announcements on Sundays.
    10. 📅 Mission Exam 2026-27: Exam countdowns and daily commentary/reminders.
    11. 📚 Grammar 101: **CRITICAL STATUS:** This course has officially ENDED. Do NOT promise new notes.

    =========================================
    MODULE 3: LEAGUES & SCORING RULES
    =========================================
    - Tiers: Unranked ➔ Bronze ➔ Silver ➔ Gold ➔ Platinum ➔ Diamond ➔ Champion ➔ Master ➔ Elite ➔ Legend ➔ Mythic ➔ Prodigy ➔ Celestial ➔ Zenith ➔ Ascendant.
    - Promotion (▲): Finish the week above the Class Average to gain +1 League. Top 10 gets +2 (Double). 1st Place gets +3 (Triple).
    - Demotion (▼): Dropping below the class average results in a -1 League demotion.

    =========================================
    MODULE 4: DYNAMIC CONTEXT
    =========================================
    [Upcoming Exams]
    {exam_context}

    [Conversation History]
    {reply_context}

    =========================================
    MODULE 5: STRICT OPERATIONAL PROTOCOL (CORE GUARDRAILS)
    =========================================
    1. THE DEFAULT ACTION IS SILENCE: If members are just chatting, debating, or greeting each other, your ONLY output must be the exact word: IGNORE.
    2. THE "ADMIN" RULE: You must completely ignore Admins unless they explicitly say "Lixie".
    3. TONE & LENGTH: Keep it short (MAX 2-3 sentences). Dive straight into the answer without pleasantries.
    4. LANGUAGE & SPELLING: ALWAYS use British English spelling for explanations and synonyms.
    5. RANK/LEADERBOARD INQUIRIES: If a user asks about their performance, tier, or standing in a way that bypassed the system's auto-intercept, do NOT give them numbers (you don't have them). Instead, wittily tell them to type the `/rank` command in the chat to instantly summon their personal stats, or to check the "🎭 Live Weekly-Cup Leaderboard" thread to access the Mini App!
    """

    ai_reply = None
    for attempt in range(len(API_KEYS)):
        try:
            active_key = API_KEYS[db_key_index]
            temp_client = genai.Client(api_key=active_key)
            response = temp_client.models.generate_content(
                model='gemini-3.5-flash-lite',
                contents=text,
                config=types.GenerateContentConfig(system_instruction=system_prompt, temperature=0.4)
            )
            ai_reply = response.text.strip()
            break
        except Exception as e:
            error_str = str(e).lower()
            if "503" in error_str or "unavailable" in error_str or "timeout" in error_str:
                return
            elif "429" in error_str or "quota" in error_str or "exhausted" in error_str:
                db_key_index = (db_key_index + 1) % len(API_KEYS)
                conn = get_db()
                c = conn.cursor()
                c.execute("""
                    INSERT INTO bot_settings (key, value) VALUES ('current_key_index', %s)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """, (str(db_key_index),))
                conn.commit()
                c.close()
                release_db(conn)
                continue
            else:
                db_key_index = (db_key_index + 1) % len(API_KEYS)
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
        
        # 5. Live-Update the Button
        markup = {"inline_keyboard": [[{"text": f"📖 Mark as Read • {total_reads}", "callback_data": f"read_{message_id}"}]]}
        http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageReplyMarkup", json={"chat_id": CHAT_ID, "message_id": message_id, "reply_markup": markup})
        
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
        
        MINI_APP_URL = "https://ez-editorials-bot.onrender.com/captcha?mode=compact"
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
            
        # ✨ THE TIME BOMB: Starts a 1-hour countdown to auto-decline lazy users
        def ignite_time_bomb():
            try:
                http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/declineChatJoinRequest", json={"chat_id": CHAT_ID, "user_id": user_id}, timeout=5)
            except: pass
            
        threading.Timer(300.0, ignite_time_bomb).start()
            
        return 'OK', 200

    if 'poll_answer' in update:
        ans = update['poll_answer']
        user_info = ans['user']
        f_name = user_info.get('first_name', '').strip()
        l_name = user_info.get('last_name', '').strip()
        formatted_name = f"{f_name} {l_name[0]}".strip() if l_name else f_name

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

        if str(chat_id) == SOURCE_CHAT_ID and thread_id in THREAD_MAPPING:
            target_thread_id = THREAD_MAPPING[thread_id]
            relay_message(message_id=msg['message_id'], target_thread_id=target_thread_id)
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
                if str(chat_id) == CHAT_ID and thread_id == 11:
                    replied_text = msg['reply_to_message']['text'] if 'reply_to_message' in msg and 'text' in msg['reply_to_message'] else None
                    threading.Thread(target=process_ai_query, kwargs={
                        "chat_id": chat_id, "user_id": msg['from']['id'], "first_name": msg['from']['first_name'],
                        "text": text, "message_id": msg['message_id'], "thread_id": thread_id, "replied_text": replied_text
                    }).start()

    return 'OK', 200

def run_midnight_purge_background():
    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        
        # Define the exact 30-day rolling window
        current_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
        thirty_days_ago_date = (current_ist - timedelta(days=30)).strftime('%Y-%m-%d')
        thirty_days_ago_ts = time.time() - (30 * 24 * 60 * 60)
        
        admin_ids = [716496729, 6251430317, 5103843488]
        
        # 🧠 THE MASTER QUERY: 
        # Calculates 30-day Quizzes and 30-day Editorials, strictly ignoring new students!
        c.execute("""
            SELECT 
                u.user_id, 
                u.first_name,
                COALESCE(SUM(dh.attempts), 0) + u.daily_attempts AS total_quizzes,
                COALESCE(rr.read_count, 0) AS total_reads
            FROM users u
            LEFT JOIN daily_history dh ON u.user_id = dh.user_id AND dh.date_str >= %s
            LEFT JOIN (
                SELECT user_id, COUNT(*) as read_count 
                FROM read_receipts 
                WHERE created_at >= to_timestamp(%s) 
                GROUP BY user_id
            ) rr ON u.user_id = rr.user_id
            WHERE u.joined_at < to_timestamp(%s)
            GROUP BY u.user_id, u.first_name, u.daily_attempts, rr.read_count
        """, (thirty_days_ago_date, thirty_days_ago_ts, thirty_days_ago_ts))
        
        all_users = c.fetchall()
        purged_count = 0
        
        if all_users:
            for u in all_users:
                uid = u[0]
                total_quizzes = u[2]
                total_reads = u[3]
                
                if uid in admin_ids:
                    continue # Never purge an admin
                    
                # ⚖️ THE EXECUTION CRITERIA: 
                # If they failed to read 4 editorials AND failed to solve 50 quizzes
                if total_reads < 4 and total_quizzes < 50:
                    
                    # 1. Soft-Ban to remove from group
                    res_ban = http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/banChatMember", json={
                        "chat_id": CHAT_ID, "user_id": uid
                    }, timeout=5)
                    
                    if res_ban.status_code == 200 and res_ban.json().get('ok'):
                        # 2. BULLETPROOF UNBAN
                        for _ in range(5):
                            res_unban = http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/unbanChatMember", json={
                                "chat_id": CHAT_ID, "user_id": uid, "only_if_banned": True
                            }, timeout=5)
                            if res_unban.status_code == 200:
                                break
                            time.sleep(1)
                        
                        # 3. Erase from DB
                        c.execute("DELETE FROM users WHERE user_id = %s", (uid,))
                        conn.commit()
                        purged_count += 1
                        
                    # 🛡️ Throttle to avoid Telegram Rate Limits
                    time.sleep(2)
        
        c.close()
        
        # 📩 DM TO ADMIN PRATHU
        # Your existing notify_prathu function is already hardcoded to send to ID: 716496729!
        notify_prathu(
            f"🧹 **Midnight Purge Complete!**\n\n"
            f"🎯 **Criteria:** < 4 Editorials & < 50 Quizzes\n"
            f"🚪 **Members Removed:** {purged_count}"
        )
        
    except Exception as e:
        notify_prathu(f"🚨 **Purge Error:**\n`{e}`")
    finally:
        if conn: release_db(conn)
            
@app.route('/cron/daily_purge_0508', methods=['GET', 'POST'])
def trigger_daily_purge():
    # Locked behind your master key
    if request.headers.get("X-Cron-Secret") != CRON_SECRET:
        return "Unauthorized", 401
        
    threading.Thread(target=run_midnight_purge_background).start()
    return "Purge Engine armed and running in background!", 200

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
        "Top10_Announcement": "🔴 Failed",
        "Admin_Debrief": "🔴 Failed",
        "Elo_Bleed_DM": "🔴 Failed",
        "Public_WrapUp": "🔴 Failed"
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

        sum_weighted_points = 0
        sum_weights = 0
        for row in all_weekly_players:
            score, attempts = row[2], row[3]
            weight = attempts ** 0.5
            sum_weighted_points += (score * weight)
            sum_weights += weight
        target_average = (sum_weighted_points / sum_weights) if sum_weights > 0 else 0

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

        # --- 1. GROUP ANNOUNCEMENT (TOP 10) ---
        top_10 = all_weekly_players[:10]
        medals = ["🥇 Rank 1", "🥈 Rank 2", "🥉 Rank 3", "4th", "5th", "6th", "7th", "8th", "9th", "10th"]

        current_ist_time = datetime.utcnow() + timedelta(hours=5, minutes=30)
        end_date = current_ist_time - timedelta(days=1)
        start_date = current_ist_time - timedelta(days=7)
        date_range = f"{start_date.strftime('%d %B')} - {end_date.strftime('%d %B')}"

        group_text = f"🌟 **WEEKLY EXAM RESULTS ARE IN!** 🌟\n📅 **{date_range}**\n\n"

        for i, user in enumerate(top_10):
            u_id, name, score, faction_val = user[0], user[1], user[2], str(user[4])
            clean_score = int(score) if score % 1 == 0 else round(score, 2)
            if "Gryffindor" in faction_val: faction_emoji = "🦁 "
            elif "Slytherin" in faction_val: faction_emoji = "🐍 "
            elif "Ravenclaw" in faction_val: faction_emoji = "🦅 "
            elif "Hufflepuff" in faction_val: faction_emoji = "🦡 "
            else: faction_emoji = ""
            group_text += f"{medals[i]}: {faction_emoji}[{name}](tg://user?id={u_id}) ({clean_score} pts)\n"

        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        for attempt in range(5):
            try:
                res = http_session.post(url, json={"chat_id": CHAT_ID, "text": group_text, "parse_mode": "Markdown", "message_thread_id": TELEGRAM_THREAD_ID}, timeout=10)
                if res.json().get("ok"):
                    reset_status["Top10_Announcement"] = "🟢 Success"
                    for _ in range(3):
                        try:
                            http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/pinChatMessage", json={"chat_id": CHAT_ID, "message_id": res.json()["result"]["message_id"], "disable_notification": False}, timeout=5)
                            break
                        except: time.sleep(2)
                    break
                elif res.json().get("error_code") == 429: time.sleep(res.json().get("parameters", {}).get("retry_after", 3) + 1)
                else: break
            except: time.sleep(3 + attempt * 2)

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
            c.execute("SELECT first_name, live_elo, base_elo FROM users WHERE live_elo IS NOT NULL")
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

            # 7. House Wars
            c.execute("SELECT faction, SUM(weekly_score) FROM users WHERE faction IS NOT NULL GROUP BY faction")
            team_scores = dict(c.fetchall())
            finals = {'Gryffindor 🦁🔥': team_scores.get('Gryffindor 🦁🔥', 0), 'Slytherin 🐍💧': team_scores.get('Slytherin 🐍💧', 0), 'Ravenclaw 🦅💨': team_scores.get('Ravenclaw 🦅💨', 0), 'Hufflepuff 🦡🌍': team_scores.get('Hufflepuff 🦡🌍', 0)}
            sorted_finals = sorted(finals.items(), key=lambda x: x[1], reverse=True)
            
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

        # ✨ 1. FIRST: Select the captains while the scores are still intact!
        c.execute("SELECT user_id, first_name FROM users WHERE faction='Gryffindor 🦁🔥' AND weekly_attempts > 0 ORDER BY weekly_score DESC LIMIT 1")
        top_gryffindor = c.fetchone()
        c.execute("SELECT user_id, first_name FROM users WHERE faction='Slytherin 🐍💧' AND weekly_attempts > 0 ORDER BY weekly_score DESC LIMIT 1")
        top_slytherin = c.fetchone()
        c.execute("SELECT user_id, first_name FROM users WHERE faction='Ravenclaw 🦅💨' AND weekly_attempts > 0 ORDER BY weekly_score DESC LIMIT 1")
        top_ravenclaw = c.fetchone()
        c.execute("SELECT user_id, first_name FROM users WHERE faction='Hufflepuff 🦡🌍' AND weekly_attempts > 0 ORDER BY weekly_score DESC LIMIT 1")
        top_hufflepuff = c.fetchone()

        # ✨ 2. SECOND: Execute "The Great Wipe" securely
        c.execute("UPDATE users SET faction = NULL WHERE weekly_score < %s", (target_average,))
        c.execute("UPDATE users SET base_elo = live_elo, weekly_score = 0, weekly_attempts = 0, is_captain = 0")
        c.execute("DELETE FROM precise_scores")

        # ✨ 3. THIRD: Reinstate the House Captains with their badges
        if top_gryffindor: c.execute("UPDATE users SET faction='Gryffindor 🦁🔥', is_captain=1 WHERE user_id=%s", (top_gryffindor[0],))
        if top_slytherin: c.execute("UPDATE users SET faction='Slytherin 🐍💧', is_captain=1 WHERE user_id=%s", (top_slytherin[0],))
        if top_ravenclaw: c.execute("UPDATE users SET faction='Ravenclaw 🦅💨', is_captain=1 WHERE user_id=%s", (top_ravenclaw[0],))
        if top_hufflepuff: c.execute("UPDATE users SET faction='Hufflepuff 🦡🌍', is_captain=1 WHERE user_id=%s", (top_hufflepuff[0],))

        gryf_cap = f"[{top_gryffindor[1]}](tg://user?id={top_gryffindor[0]})" if top_gryffindor else "None"
        slyth_cap = f"[{top_slytherin[1]}](tg://user?id={top_slytherin[0]})" if top_slytherin else "None"
        rav_cap = f"[{top_ravenclaw[1]}](tg://user?id={top_ravenclaw[0]})" if top_ravenclaw else "None"
        huff_cap = f"[{top_hufflepuff[1]}](tg://user?id={top_hufflepuff[0]})" if top_hufflepuff else "None"

        winner_house = sorted_finals[0][0]
        winner_score = sorted_finals[0][1]
        second_score = sorted_finals[1][1] if len(sorted_finals) > 1 else 0

        winning_banner = ""
        if winner_score > second_score:
            house_name, emoji = winner_house.split()[0].upper(), winner_house.split()[1]
            clean_win_score = int(winner_score) if winner_score % 1 == 0 else round(winner_score, 2)
            winning_banner = f"🥇 **TEAM {house_name} WINS!** {emoji}\nSecuring the top spot with **{clean_win_score}** points! Your reigning Team Captains for this new week are:\n\n"
        elif winner_score > 0 and winner_score == second_score:
            clean_win_score = int(winner_score) if winner_score % 1 == 0 else round(winner_score, 2)
            winning_banner = f"⚖️ **TEAM TIE!**\nThe top teams tied with **{clean_win_score}** points. Your reigning Team Captains for this new week are:\n\n"
        else:
            winning_banner = "⚖️ **THE WEEK HAS ENDED!**\nNo points were earned this week. Your reigning Team Captains for this new week are:\n\n"

        cutoff_change_text = ""

        announcement_text = "🏆 ✨ **WEEKLY CUP WRAP-UP & ANALYSIS** ✨ 🏆\n\n"
        announcement_text += winning_banner
        announcement_text += f"🦁 **Gryffindor:** 👑 {gryf_cap}\n🐍 **Slytherin:** 👑 {slyth_cap}\n🦅 **Ravenclaw:** 👑 {rav_cap}\n🦡 **Hufflepuff:** 👑 {huff_cap}\n\n"
        announcement_text += "📊 **Community Performance Analysis:**\n"
        announcement_text += f"• **Active Challengers:** **{total_active_students}** students consistently competed this week.\n"
        announcement_text += f"• **Total Engagement:** A massive **{total_weekly_attempts}** questions were attempted collectively!\n"
        announcement_text += f"• **Overall Accuracy:** The class achieved a combined accuracy rate of **{overall_accuracy}%**.\n"
        announcement_text += f"• **League Progress:** The final promotion cut-off landed at **{int(target_average)} pts**{cutoff_change_text}, with **{promoted_count}** students successfully levelling up their league tier.\n\n"
        announcement_text += "⚡️ The leaderboards have been wiped clean. Attempt your first quiz today at 7:00 PM to kick off the new week!"

        for attempt in range(5):
            try:
                res = http_session.post(url, json={"chat_id": CHAT_ID, "message_thread_id": ANNOUNCEMENT_THREAD_ID, "text": announcement_text, "parse_mode": "Markdown"}, timeout=10)
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
            f"🥇 **Top 10 Blast:** {reset_status['Top10_Announcement']}\n"
            f"🔐 **Admin Debrief:** {reset_status['Admin_Debrief']}\n"
            f"🩸 **Elo Bleed DM:** {reset_status['Elo_Bleed_DM']}\n"
            f"🏰 **Final Wrap-Up:** {reset_status['Public_WrapUp']}\n"
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

def run_queue_processor_background():
    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        
        # Pick up pending and stuck answers
        c.execute("SELECT id, user_id, first_name, poll_id, chosen_option FROM answer_queue WHERE status IN ('pending', 'processing')")
        pending_answers = c.fetchall()

        if pending_answers:
            for row in pending_answers:
                c.execute("UPDATE answer_queue SET status = 'processing' WHERE id = %s", (row[0],))
            conn.commit()

            for row in pending_answers:
                # ✨ FIX: We now pass the unique queue ID (row[0]) to the processor
                process_answer(c, queue_id=row[0], user_id=row[1], first_name=row[2], poll_id=row[3], chosen_option=row[4])

            # ✨ FIX: Removed the batch DELETE command from here entirely.
            # If an answer fails, it stays in the queue indefinitely until it succeeds!
            
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
    conn = None
    try:
        recalculate_dynamic_scores()
        # ❌ REMOVED: bake_miniapp_cache() to prevent duplicate egress pulls. 
        # The separate cron job handles this now.
        
        conn = get_db()
        c = conn.cursor()
        c.execute("""
            INSERT INTO bot_settings (key, value) VALUES ('telegram_needs_update', '1') 
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

    dynamic_open_period = int(((current_ist + timedelta(days=6 - current_ist.weekday())).replace(hour=23, minute=59, second=59) - current_ist).total_seconds())

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
        c.execute("UPDATE users SET weekly_score = 0, daily_score = 0")
        c.execute("DELETE FROM precise_scores")

        c.execute("SELECT poll_id, poll_day FROM polls")
        active_polls = c.fetchall()
        poll_values = {}
        total_max_points = 0.0

        for poll in active_polls:
            p_id, p_day = poll[0], poll[1]
            c.execute("SELECT COUNT(*), SUM(is_correct) FROM user_answers WHERE poll_id = %s", (p_id,))
            result = c.fetchone()
            total_attempts = result[0]
            total_correct = result[1] if result[1] else 0

            if total_attempts == 0:
                total_max_points += 3.0
                continue

            accuracy = (total_correct / total_attempts) * 100
            if accuracy >= 86: pts, pen, q_elo = 1.0, -0.42, 800
            elif accuracy >= 72: pts, pen, q_elo = 2.0, -0.60, 1000
            elif accuracy >= 58: pts, pen, q_elo = 3.0, -0.75, 1200
            elif accuracy >= 44: pts, pen, q_elo = 4.0, -0.60, 1500
            else: pts, pen, q_elo = 5.0, -0.42, 1800

            poll_values[p_id] = {"pts": pts, "pen": pen, "day": p_day, "elo": q_elo}
            total_max_points += pts

        if total_max_points > 0: total_max_points += 0.24

        c.execute("""
            INSERT INTO bot_settings (key, value) VALUES ('live_max_points', %s) 
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (str(total_max_points),))

        # ⚡ OPTIMIZED: Only pull base Elo for users actively answering quizzes today
        c.execute("SELECT user_id, base_elo FROM users WHERE weekly_attempts > 0")
        base_elos = {row[0]: (row[1] if row[1] is not None else 1000) for row in c.fetchall()}

        c.execute("SELECT user_id, poll_id, is_correct, poll_day FROM user_answers")
        all_answers = c.fetchall()
        current_day_str = (datetime.utcnow() + timedelta(hours=5, minutes=30)).strftime('%a')
        user_scores = {}

        for ans in all_answers:
            u_id, p_id, is_correct, p_day = ans
            if p_id not in poll_values: continue

            val = poll_values[p_id]
            points_awarded = val["pts"] if is_correct else val["pen"]
            
            if u_id not in user_scores:
                # ✨ FIX 1: Added 'weekly_attempts' to the tracker
                user_scores[u_id] = {"weekly": 0, "daily": 0, "weekly_correct": 0, "weekly_attempts": 0, "expected_wins": 0.0, "actual_wins": 0, "precise": {}, "tier_bonus": 0.0, "played_today": False}

            user_scores[u_id]["weekly"] += points_awarded
            user_scores[u_id]["weekly_correct"] += int(is_correct)
            
            # ✨ FIX 2: Manually count the attempts based on the true user_answers table!
            user_scores[u_id]["weekly_attempts"] += 1 

            if p_day == current_day_str:
                user_scores[u_id]["daily"] += points_awarded
                user_scores[u_id]["played_today"] = True

            if p_day not in user_scores[u_id]["precise"]:
                user_scores[u_id]["precise"][p_day] = {"score": 0, "attempts": 0, "correct": 0}

            user_scores[u_id]["precise"][p_day]["score"] += points_awarded
            user_scores[u_id]["precise"][p_day]["attempts"] += 1
            user_scores[u_id]["precise"][p_day]["correct"] += int(is_correct)

            if is_correct: user_scores[u_id]["tier_bonus"] += (val["pts"] - 1.0) * 0.0002
            u_base = base_elos.get(u_id, 1000)
            user_scores[u_id]["expected_wins"] += 1 / (1 + 10 ** ((val["elo"] - u_base) / 400.0))
            user_scores[u_id]["actual_wins"] += int(is_correct)

        for u_id, totals in user_scores.items():
            u_base = base_elos.get(u_id, 1000)
            new_live_elo = max(500.0, u_base + 0.5 * (totals["actual_wins"] - totals["expected_wins"]))
            
            elo_fraction = max(0.0, min(1.0, (new_live_elo - 500) / 2000.0))
            total_sweetener = round(min(0.24, (elo_fraction * 0.12) + totals["tier_bonus"]), 2)

            final_weekly = totals["weekly"] + total_sweetener
            final_daily = totals["daily"] + total_sweetener if totals["played_today"] else 0

            # ✨ FIX 3: Force the database to update the broken weekly_attempts counter
            c.execute("UPDATE users SET weekly_score=%s, daily_score=%s, weekly_correct=%s, weekly_attempts=%s, live_elo=%s WHERE user_id=%s",
                      (final_weekly, final_daily, totals["weekly_correct"], totals["weekly_attempts"], new_live_elo, u_id))

            for day, day_data in totals["precise"].items():
                c.execute("""
                    INSERT INTO precise_scores (user_id, day_label, score, attempts, correct_answers) 
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (user_id, day_label) DO UPDATE SET 
                        score = EXCLUDED.score, attempts = EXCLUDED.attempts, correct_answers = EXCLUDED.correct_answers
                """, (u_id, day, day_data["score"], day_data["attempts"], day_data["correct"]))

        conn.commit()
    except Exception as e:
        print(f"🚨 Math Engine Error: {e}")
        if conn:
            conn.rollback() # ✨ FIX 4: Clear the deadlocks so the server doesn't crash
    finally:
        if conn:
            try:
                c.close()
            except:
                pass
            release_db(conn)

# ==========================================
# BACKGROUND WORKER: SUNDAY ANNOUNCEMENT RESTORED
# ==========================================
def run_sunday_announcement():
    text = "_There won't be any Today's Editorials today; Editorials will be available Monday through Saturday exclusively._"
    for attempt in range(10):
        try:
            res = http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "message_thread_id": 3, "text": text, "parse_mode": "Markdown"}, timeout=20)
            if res.status_code == 200: 
                notify_prathu("📢 **Sunday Announcement** posted successfully!")
                break
            elif res.status_code == 429: time.sleep(res.json().get("parameters", {}).get("retry_after", 5) + 1)
            else: time.sleep(2)
        except: time.sleep(3 + attempt * 2)

@app.route('/sunday_announcement/0508', methods=['GET', 'POST'])
def trigger_sunday_announcement():
    threading.Thread(target=run_sunday_announcement).start()
    return "Sunday announcement triggered in background!", 200

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

    dynamic_open_period = int(((current_ist_time + timedelta(days=6 - current_ist_time.weekday())).replace(hour=23, minute=59, second=59) - current_ist_time).total_seconds())
    if dynamic_open_period > 600538: dynamic_open_period = 600538
    elif dynamic_open_period < 5: dynamic_open_period = 5

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

# ==========================================
# BACKGROUND WORKER: SUNDAY REMINDERS RESTORED
# ==========================================
def run_sunday_reminder():
    current_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    date_str = f"[ {(current_ist - timedelta(days=6)).strftime('%d %B')} ➪ {(current_ist - timedelta(days=1)).strftime('%d %B')} ]"
    
    # Target threads and their specific drill names
    targets = [
        {"thread_id": 246, "drill": "Vocab Drill"},
        {"thread_id": 10123, "drill": "Topic Drill"}
    ]

    for target in targets:
        text = (
            f"⏳ ⟪ **THE HOUSE CUP COUNTDOWN** ⟫ ⏳\n"
            f"📅 `{date_str}`\n"
            f"🎯 **{target['drill']}**\n"
            f"🚨 **Last Chance!** 🚨\n"
            f"Today is the **absolute final day** to complete your weekly quizzes! The Great Hall hourglasses are locking soon.\n"
            f"Every point shifts the balance of power. Finish your magical trials before tonight's final tally! 🏆✨"
        )
        
        for attempt in range(10):
            try:
                res = http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "message_thread_id": target["thread_id"], "text": text, "parse_mode": "Markdown"}, timeout=20)
                if res.status_code == 200:
                    http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/pinChatMessage", json={"chat_id": CHAT_ID, "message_id": res.json()["result"]["message_id"], "disable_notification": False}, timeout=10)
                    notify_prathu("⏳ **Sunday Warning Reminder** posted successfully!")
                    break
            except: 
                time.sleep(3 + attempt * 2)
        
        time.sleep(2)

@app.route('/sunday_reminder/0508', methods=['GET', 'POST'])
def trigger_sunday_reminder():
    threading.Thread(target=run_sunday_reminder).start()
    return "Sunday reminder triggered!", 200

def run_sunday_final_reminder():
    current_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    total_seconds = int((current_ist.replace(hour=23, minute=59, second=59) - current_ist).total_seconds())
    time_str = f"{total_seconds // 3600} Hours and {(total_seconds % 3600) // 60} Minutes" if total_seconds > 0 else "0 Minutes"

    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM polls")
        total_quizzes = c.fetchone()[0]
        c.execute("SELECT u.weekly_score, u.weekly_attempts, (SELECT SUM(is_correct) FROM user_answers WHERE user_id = u.user_id) FROM users u WHERE u.weekly_attempts > 0")
        all_active_users = c.fetchall()
        
        target_average, sum_weights, sum_weighted = 0, 0, 0
        for score, attempts, correct in all_active_users:
            if attempts == 0: continue
            w = (attempts / (attempts + 10.0)) * ((correct or 0) / attempts)
            if score >= 0: sum_weighted += score * w; sum_weights += w
        if sum_weights > 0: target_average = int((sum_weighted / sum_weights) + 0.5)

        cleared = sum(1 for r in all_active_users if r[0] >= target_average)
        slackers = sum(1 for r in all_active_users if r[1] < total_quizzes)
        c.close()
        release_db(conn)
    except: return

    text = f"<blockquote>⏱ <b>{time_str} Remaining:</b> The weekly leaderboard officially locks tonight at midnight.\n\n📈 <b>Promotion Cut-off:</b> The current class cut-off score is <b>{target_average} pts</b>.\n\n📊 <b>Live Stats:</b>\n• <b>{cleared}</b> out of <b>{len(all_active_users)}</b> active students are currently in the Promotion Zone.\n• <b>{slackers}</b> out of <b>{len(all_active_users)}</b> active students have not yet completed all <b>{total_quizzes}</b> available quizzes this week.\n\n⚡️ If you are below the cut-off, complete your pending quizzes before midnight to secure your rank!</blockquote>"

    for attempt in range(10):
        try:
            res = http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "message_thread_id": 11, "text": text, "parse_mode": "HTML"}, timeout=20)
            if res.status_code == 200:
                http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/pinChatMessage", json={"chat_id": CHAT_ID, "message_id": res.json()["result"]["message_id"], "disable_notification": False}, timeout=10)
                notify_prathu("⏱️ **Sunday Final Midnight Reminder** posted successfully!")
                break
        except: time.sleep(3 + attempt * 2)

@app.route('/sunday_final_reminder/0508', methods=['GET', 'POST'])
def trigger_sunday_final_reminder():
    threading.Thread(target=run_sunday_final_reminder).start()
    return "Sunday final reminder triggered!", 200

# ==========================================
# BACKGROUND WORKER: EXAM COUNTDOWN RESTORED
# ==========================================
def run_countdown_and_commentary():
    fetch_and_update_exams_db()
    time.sleep(3)
    update_exam_countdown()
    time.sleep(5)
    generate_and_send_commentary()
    notify_prathu("📅 **Exam Countdown & AI Commentary** updated successfully!")

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

def update_exam_countdown():
    current_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    dynamic_exams = []
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT name, exam_date, status, is_exact_date, display_date FROM upcoming_exams")
        for row in c.fetchall():
            try: dynamic_exams.append({"name": row[0], "date": datetime.strptime(row[1], "%Y-%m-%d"), "status": row[2], "is_exact_date": bool(row[3]), "display_date": row[4]})
            except ValueError: continue
        c.close()
        release_db(conn)
    except: pass

    text = f"⏳ **UPCOMING EXAM COUNTDOWN** ⏳\n📅 **Today's Date:** {current_ist.strftime('%d %B %Y')}\n━━━━━━━━━━━━━━━━━━━━\n\n"
    active_exams_found = False
    for exam in sorted(dynamic_exams, key=lambda x: x['date']):
        delta = (exam['date'].date() - current_ist.date()).days
        if delta >= 0:
            # 🛑 FILTER: Skip placeholder exams that have no official date announced yet
            if exam.get('display_date', '').lower().strip() == 'to be announced':
                continue
                
            active_exams_found = True
            text += f"🎯 **{exam['name']}** `[{exam.get('status', 'Expected')}]`\n"
            if delta == 0: text += f"└ 🚨 **TODAY IS THE EXAM! Best of luck!** 🚨\n\n"
            elif exam.get('is_exact_date', True): text += f"└ 🗓 {exam.get('display_date', exam['date'].strftime('%d %b %Y'))} ➪ `{delta} Days Left`\n\n"
            else: text += f"└ 🗓 {exam.get('display_date', exam['date'].strftime('%d %b %Y'))} ➪ `~{delta} Days Left`\n\n"

    if not active_exams_found: text += "No upcoming exams currently scheduled. Keep practicing! 🪄\n\n"
    text += "━━━━━━━━━━━━━━━━━━━━\n*Keep grinding, future officers!* ✨"

    for attempt in range(10):
        try:
            res = http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText", json={"chat_id": CHAT_ID, "message_id": COUNTDOWN_MESSAGE_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)
            if res.status_code == 200: break
            elif res.status_code == 429: time.sleep(res.json().get("parameters", {}).get("retry_after", 3) + 1)
            else: break
        except: time.sleep(3 + attempt * 2)

def generate_and_send_commentary():
    current_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    milestones_hit = []
    
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT name, exam_date, is_exact_date, display_date FROM upcoming_exams ORDER BY exam_date ASC")
        rows = c.fetchall()

        # STEP 1: Collect ALL exams that hit a milestone today (both exact and tentative)
        for row in rows:
            try:
                exam_name = row[0]
                exam_date = datetime.strptime(row[1], "%Y-%m-%d").date()
                is_exact_date = bool(row[2])
                display_date = row[3]
                delta = (exam_date - current_ist.date()).days

                if display_date.lower().strip() == "to be announced":
                    continue

                if delta == 0 and is_exact_date:
                    milestones_hit.append({
                        "name": exam_name, "days": 0, "is_today": True, 
                        "is_exact": True, "display_date": display_date
                    })
                elif delta in [90, 60, 30, 15, 7, 1]:
                    milestones_hit.append({
                        "name": exam_name, "days": delta, "is_today": False, 
                        "is_exact": is_exact_date, "display_date": display_date
                    })
            except ValueError:
                continue
    except Exception as e:
        print(f"⚠️ Error fetching exam commentary target: {e}")
        try: c.close(); release_db(conn)
        except: pass
        return

    if not milestones_hit:
        try: c.close(); release_db(conn)
        except: pass
        return

    # STEP 2: Sort exams by urgency
    milestones_hit.sort(key=lambda x: x["days"])
    
    primary_exam = milestones_hit[0]
    secondary_exams = milestones_hit[1:]

    # STEP 3: Formulate Contextual Prompt
    if primary_exam["is_today"]:
        prompt = (
            f"Create a short Telegram exam-day wishing message following this EXACT 3-line structure:\n"
            f"Line 1: 🚨 {primary_exam['name']} ➪ TODAY IS THE EXAM!\n"
            f"Line 2: [1 short, encouraging sentence wishing candidates best of luck and advising them to stay calm and confident]\n"
            f"Line 3: Best of luck to all candidates! 🚀🏆\n"
            f"Rules: STRICTLY follow the 3-line format. No conversational filler. No hashtags. Keep it clean. ALWAYS use British English spelling."
        )
    elif not primary_exam["is_exact"]:
        prompt = (
            f"Create a short Telegram exam commentary message following this EXACT 3-line structure:\n"
            f"Line 1: 🚨 {primary_exam['name']} ➪ Expected: {primary_exam['display_date']}!\n"
            f"Line 2: [1 short, hype, action-oriented sentence reminding students that the exam timeframe is approaching fast]\n"
            f"Line 3: [1 short motivational sign-off with emojis]\n"
            f"Rules: STRICTLY follow the 3-line format. No conversational filler. No hashtags. Keep it clean. ALWAYS use British English spelling."
        )
    else:
        prompt = (
            f"Create a short Telegram exam commentary message following this EXACT 3-line structure:\n"
            f"Line 1: 🚨 {primary_exam['name']} ➪ {primary_exam['days']} Days Left!\n"
            f"Line 2: [1 short, hype, action-oriented sentence about studying/preparing]\n"
            f"Line 3: [1 short motivational sign-off with emojis]\n"
            f"Rules: STRICTLY follow the 3-line format. No conversational filler. No hashtags. Keep it clean. ALWAYS use British English spelling."
        )

    # STEP 4: Database-Backed Key Rotation
    ai_text = None
    try:
        c.execute("SELECT value FROM bot_settings WHERE key='current_key_index'")
        key_row = c.fetchone()
        db_key_index = int(key_row[0]) if key_row else 0
    except:
        db_key_index = 0

    for attempt in range(len(API_KEYS)):
        try:
            active_key = API_KEYS[db_key_index]
            temp_client = genai.Client(api_key=active_key)
            response = temp_client.models.generate_content(
                model='gemini-3.6-flash',
                contents=prompt
            )
            if response.text:
                ai_text = response.text.strip()
                break
        except Exception:
            db_key_index = (db_key_index + 1) % len(API_KEYS)
            try:
                c.execute("INSERT INTO bot_settings (key, value) VALUES ('current_key_index', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (str(db_key_index),))
                conn.commit()
            except: pass
            continue

    if not ai_text:
        try: c.close(); release_db(conn)
        except: pass
        return

    # STEP 5: Delete YESTERDAY'S messages (Both AI Commentary and Quick Insights)
    try:
        c.execute("SELECT value FROM bot_settings WHERE key='last_commentary_msg_id'")
        if last_msg := c.fetchone():
            http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteMessage", json={"chat_id": CHAT_ID, "message_id": int(last_msg[0])}, timeout=5)
            
        c.execute("SELECT value FROM bot_settings WHERE key='last_quick_insights_msg_id'")
        if last_insights_msg := c.fetchone():
            http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteMessage", json={"chat_id": CHAT_ID, "message_id": int(last_insights_msg[0])}, timeout=5)
    except: 
        pass

    # STEP 6: Send MESSAGE 1 (AI Commentary)
    for attempt in range(3):
        try:
            res_main = http_session.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", 
                json={
                    "chat_id": CHAT_ID, 
                    "message_thread_id": COUNTDOWN_THREAD_ID, 
                    "text": f"🤖 **Daily Exam Insights**\n\n{ai_text}", 
                    "parse_mode": "Markdown"
                }, 
                timeout=10
            )
            if res_main.json().get("ok"):
                new_msg_id = res_main.json()["result"]["message_id"]
                c.execute("INSERT INTO bot_settings (key, value) VALUES ('last_commentary_msg_id', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (str(new_msg_id),))
                conn.commit()
                break
            else: 
                time.sleep(2)
        except: 
            time.sleep(3)

    # STEP 7: Send MESSAGE 2 (Quick Insights) - ONLY if there are secondary exams
    if secondary_exams:
        quick_insights_text = "📌 **Quick Insights:**\n\n"
        for sec in secondary_exams:
            if sec["is_today"]:
                quick_insights_text += f"• 🚨 **{sec['name']}** ➪ TODAY IS THE EXAM!\n"
            elif not sec["is_exact"]:
                quick_insights_text += f"• **{sec['name']}** ➪ Expected: {sec['display_date']}\n"
            else:
                quick_insights_text += f"• **{sec['name']}** ➪ {sec['days']} Days Left\n"

        for attempt in range(3):
            try:
                res_sec = http_session.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", 
                    json={
                        "chat_id": CHAT_ID, 
                        "message_thread_id": COUNTDOWN_THREAD_ID, 
                        "text": quick_insights_text, 
                        "parse_mode": "Markdown"
                    }, 
                    timeout=10
                )
                if res_sec.json().get("ok"):
                    new_insights_id = res_sec.json()["result"]["message_id"]
                    c.execute("INSERT INTO bot_settings (key, value) VALUES ('last_quick_insights_msg_id', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (str(new_insights_id),))
                    conn.commit()
                    break
                else: 
                    time.sleep(2)
            except: 
                time.sleep(3)

    try: c.close(); release_db(conn)
    except: pass

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

                # ✨ NEW: Inject the "Mark as Read" button if it's the Editorials thread
                if target_thread_id == 3:
                    markup = {
                        "inline_keyboard": [[
                            {"text": "📖 Mark as Read • 0", "callback_data": f"read_{new_msg_id}"}
                        ]]
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
        if not RAM_CACHE["miniapp_snapshot"]:
            try:
                bake_miniapp_cache()
            except Exception as e:
                print(f"Error auto-baking cache for /rank: {e}")

        if not RAM_CACHE["miniapp_snapshot"]:
            return

        # Parse the JSON cache directly from memory
        cache_data = json.loads(RAM_CACHE["miniapp_snapshot"])
        leaderboard = cache_data.get('leaderboard', [])
        elo_ranking = cache_data.get('elo_ranking', [])
        total_quizzes = cache_data.get('total_quizzes', 0)

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

@app.route('/miniapp')
def serve_mini_app():
    return render_template('leaderboard.html')

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

            leaderboard_list.append({
                "rank": index + 1, "id": uid, "name": user[1], "score": u_score,
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

from flask import Response

@app.route('/api/leaderboard', methods=['GET'])
def get_mini_app_leaderboard():
    global RAM_CACHE
    user_id = request.args.get('user_id', default=0, type=int)
    
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
            # ✨ RESTORED: Send the full chart and history data so profiles work perfectly!
            custom_leaderboard.append(u)

    custom_elo = []
    for index, eu in enumerate(master_data["elo_ranking"]):
        if index < 50 or eu["id"] == user_id:
            custom_elo.append(eu)

    response_data = {
        "current_week": master_data["current_week"],
        "total_quizzes": master_data["total_quizzes"],
        "target_average": target_avg, 
        "total_active": master_data.get("total_active", len(master_data["leaderboard"])),
        "topper_history": master_data["topper_history"],
        "class_avg_history": master_data["class_avg_history"],
        "leaderboard": custom_leaderboard,
        "elo_ranking": custom_elo
    }

    res = Response(json.dumps(response_data), mimetype='application/json')
    res.headers["Cache-Control"] = "public, max-age=30"
    return res
    
@app.route('/cron/refresh_snapshot_0508', methods=['GET', 'POST'])
def cron_refresh_snapshot():
    # 🔒 SECURITY GATE
    if request.headers.get("X-Cron-Secret") != CRON_SECRET:
        return "Unauthorized", 401

    # Triggers the cache bake in the background instantly
    threading.Thread(target=bake_miniapp_cache).start()
    return "RAM Snapshot refresh triggered in background!", 200
    
def run_word_of_the_day():
    # ==========================================
    # PART 1: LIFETIME WORD OF THE DAY (Thread 2343)
    # ==========================================
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("SELECT word FROM lifetime_words")
        used_words = [row[0] for row in c.fetchall()]
    except Exception as e:
        print(f"DB Read Error (WOTD): {e}")
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
    successful_key_idx = 0 # ✨ Track which key does the heavy lifting
    
    for idx, key in enumerate(API_KEYS):
        try:
            temp_client = genai.Client(api_key=key)
            response = temp_client.models.generate_content(model='gemini-3.6-flash', contents=wotd_prompt, config=types.GenerateContentConfig(temperature=0.5))
            if response.text:
                wotd_text = response.text.strip()
                successful_key_idx = idx # Lock in the successful key
                break
        except: continue

    if wotd_text:
        for attempt in range(10):
            try:
                res = http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "message_thread_id": 2343, "text": wotd_text}, timeout=20)
                if res.status_code == 200:
                    try:
                        lines = [line.strip() for line in wotd_text.split('\n') if line.strip()]
                        extracted_word = lines[1].split(' ')[0].strip().lower()
                        conn = get_db()
                        c = conn.cursor()
                        c.execute("INSERT INTO lifetime_words (word) VALUES (%s) ON CONFLICT (word) DO NOTHING", (extracted_word,))
                        conn.commit()
                        c.close()
                        release_db(conn)
                    except: pass
                    notify_prathu("📖 **Word of the Day** generated successfully!")
                    break
                elif res.status_code == 429: time.sleep(res.json().get("parameters", {}).get("retry_after", 5) + 1)
                else: time.sleep(2)
            except: time.sleep(3 + attempt * 2)
    else:
        notify_prathu("🚨 **ERROR:** WOTD generation failed!")

    # ==========================================
    # PART 2: FOREIGN EXPRESSIONS (Thread 11028)
    # ==========================================
    time.sleep(5) 
    
    conn = get_db()
    c = conn.cursor()
    try:
        # Fetch the next 3 unused words sequentially
        c.execute("SELECT id, word FROM foreign_expressions WHERE is_used = FALSE ORDER BY id ASC LIMIT 3")
        foreign_batch = c.fetchall()
    except Exception as e:
        print(f"DB Read Error (Foreign Words): {e}")
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

📰 <Write one short human scenario or reaction in clear, natural English (approximately CEFR B1–B2). It MUST strictly reflect the specific political, economic, or social theme of the article. Do NOT use generic dictionary examples or unrelated business scenarios, and it should help students naturally remember the word.>

2️⃣ <Expression 2> (<Language of origin>)

🔊 <Simple English Pronunciation>

💡 <Short, simple meaning in English>.
(<Hindi meaning>)

📰 <Write one short human scenario or reaction in clear, natural English (approximately CEFR B1–B2). It MUST strictly reflect the specific political, economic, or social theme of the article. Do NOT use generic dictionary examples or unrelated business scenarios, and it should help students naturally remember the word.>

3️⃣ <Expression 3> (<Language of origin>)

🔊 <Simple English Pronunciation>

💡 <Short, simple meaning in English>.
(<Hindi meaning>)

📰 <Write one short human scenario or reaction in clear, natural English (approximately CEFR B1–B2). It MUST strictly reflect the specific political, economic, or social theme of the article. Do NOT use generic dictionary examples or unrelated business scenarios, and it should help students naturally remember the word.>"""

        foreign_text = None
        
        # ✨ LOAD BALANCER: Shift the array to start with the NEXT key in line
        shifted_keys = API_KEYS[successful_key_idx + 1:] + API_KEYS[:successful_key_idx + 1]
        
        for key in shifted_keys:
            try:
                temp_client = genai.Client(api_key=key)
                response = temp_client.models.generate_content(model='gemini-3.6-flash', contents=foreign_prompt, config=types.GenerateContentConfig(temperature=0.3))
                if response.text:
                    foreign_text = response.text.strip()
                    break
            except: continue
            
        if foreign_text:
            for attempt in range(10):
                try:
                    res = http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "message_thread_id": 11028, "text": foreign_text}, timeout=20)
                    if res.status_code == 200:
                        try:
                            conn = get_db()
                            c = conn.cursor()
                            c.execute("UPDATE foreign_expressions SET is_used = TRUE WHERE id IN %s", (tuple(words_ids),))
                            conn.commit()
                            c.close()
                            release_db(conn)
                        except: pass
                        notify_prathu("🌍 **Foreign Expressions** drop executed successfully!")
                        break
                    elif res.status_code == 429: time.sleep(res.json().get("parameters", {}).get("retry_after", 5) + 1)
                    else: time.sleep(2)
                except: time.sleep(3 + attempt * 2)
        else:
            notify_prathu("🚨 **ERROR:** Foreign Expressions AI generation failed!")
    elif len(foreign_batch) < 3:
        notify_prathu("🚨 **ALERT:** You are out of Foreign Expressions! The master list of 250 has been completed.")

@app.route('/word_of_the_day/0508', methods=['GET', 'POST'])
def trigger_word_of_the_day():
    threading.Thread(target=run_word_of_the_day).start()
    return "Word of the Day triggered!", 200
    
@app.route('/captcha')
def serve_captcha():
    return render_template('captcha.html')

def background_approve_user(user_id):
    # ✨ NEW: Automatically store the approved user in Supabase with today's timestamp!
    try:
        conn = get_db()
        c = conn.cursor()
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
    
    # 1. Approve the request safely with anti-spam retry logic
    for attempt in range(5):
        try:
            res = http_session.post(url, json=payload, timeout=10)
            if res.status_code == 200:
                break
            elif res.status_code == 429: # Telegram Rate Limit
                time.sleep(res.json().get("parameters", {}).get("retry_after", 3) + 1)
            else:
                break
        except:
            time.sleep(2)
            
    # 2. Send the Welcome DM
    try:
        http_session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={
            "chat_id": user_id,
            "text": "🎉 **Trial Complete!**\n\nYou have been approved. Welcome to the Great Hall of Ez Editorials! 🪄",
            "parse_mode": "Markdown"
        }, timeout=5)
    except:
        pass

@app.route('/api/approve_captcha', methods=['POST'])
def approve_captcha():
    data = request.get_json()
    user_id = data.get('user_id')
    
    if not user_id:
        return jsonify({"error": "No user ID provided"}), 400

    # Instantly pass the heavy lifting to a background thread
    threading.Thread(target=background_approve_user, args=(user_id,)).start()

    # Instantly tell the Mini App to close without waiting!
    return jsonify({"status": "success"}), 200

@app.route("/privacy")
def privacy():
    return render_template("privacy.html")
    
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
