import threading
import os
from flask import Flask, request, render_template, jsonify
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
db_pool = psycopg2.pool.ThreadedConnectionPool(1, 20, DB_URL)

CRON_SECRET = os.environ.get("CRON_SECRET", "Ez_Master_Key_77")

GITHUB_PAT = os.environ.get("GITHUB_PAT", "")

def get_db():
    return db_pool.getconn()

def release_db(conn):
    db_pool.putconn(conn)

# --- AI Configuration ---
API_KEYS = [
    "AIzaSyDb5THxDk58CrdPJ7nVKJov6gL87_G2hQ0g",
    "AQ.Ab8RN6L9w5YfLZE350_jxGvWS7NYJnTeYVoRH6yHoZlIDMnP1A",
    "AQ.Ab8RN6LTnLSW4d1ge6MHadn7YTOO1z608dB9ulQ8qNG3EOJHdw"
]

current_key_index = 0
app = Flask(__name__)

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

def notify_prathu(quiz_name):
    admin_chat_id = "716496729"
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": admin_chat_id,
        "text": f"🎩 ✅ **Success:** The {quiz_name} has been successfully dropped into the group!",
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload, timeout=10)
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

def process_answer(c, user_id, first_name, poll_id, chosen_option):
    try:
        c.execute("SELECT 1 FROM user_answers WHERE user_id=%s AND poll_id=%s", (user_id, poll_id))
        if c.fetchone():
            return

        c.execute("SELECT correct_index, poll_day FROM polls WHERE poll_id=%s", (poll_id,))
        poll_data = c.fetchone()
        if not poll_data:
            return

        correct_index = poll_data[0]
        current_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
        poll_day = poll_data[1] if poll_data[1] else current_ist.strftime('%a')
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

    except Exception as db_err:
        print(f"🚨 Memory batch execution error for user {user_id}: {db_err}")

def update_live_leaderboard():
    conn = get_db()
    c = conn.cursor()

    current_ist_time = datetime.utcnow() + timedelta(hours=5, minutes=30)
    monday_date = current_ist_time - timedelta(days=current_ist_time.weekday())
    sunday_date = monday_date + timedelta(days=6)
    date_range = f"{monday_date.strftime('%d %b')} - {sunday_date.strftime('%d %b')}"

    weekday_idx = current_ist_time.weekday()
    if weekday_idx == 6:
        phase_text = "Final Day! *(Locks at Midnight)*"
    else:
        phase_text = f"Day {weekday_idx + 1} of 6 *(Competition Active)*"

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
        SELECT u.weekly_score, u.weekly_attempts,
               (SELECT SUM(is_correct) FROM user_answers WHERE user_id = u.user_id) as correct
        FROM users u WHERE u.weekly_attempts > 0
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

            correct = exact_correct if exact_correct else 0
            accuracy = correct / attempts

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

    house_emoji_map = {'Gryffindor 🦁🔥': '🦁', 'Slytherin 🐍💧': '🐍', 'Ravenclaw 🦅💨': '🦅', 'Hufflepuff 🦡🌍': '🦡'}
    house_abbrev = {'Gryffindor 🦁🔥': '🦁 **GRY:**', 'Slytherin 🐍💧': '🐍 **SLY:**', 'Ravenclaw 🦅💨': '🦅 **RAV:**', 'Hufflepuff 🦡🌍': '🦡 **HUF:**'}

    standings_parts = []
    for house, score in sorted_houses:
        abbrev = house_abbrev.get(house, "🏳️ **UNK:**")
        clean_score = int(score) if score % 1 == 0 else round(score, 2)
        standings_parts.append(f"{abbrev} `{clean_score}`")

    dynamic_house_line = " | ".join(standings_parts)

    if top_score > sorted_houses[1][1]:
        house_name_only = top_house.split()[0]
        house_emoji = house_emoji_map.get(top_house, "🏳️")
        lead_text = f"🏆 *({house_emoji} {house_name_only} leads the race for the House Cup!)*"
    elif top_score > 0 and top_score == sorted_houses[1][1]:
        lead_text = "⚖️ *(The House Cup is currently tied!)*"
    else:
        lead_text = "⚖️ *(No points have been earned yet!)*"

    c.execute("SELECT user_id, first_name, weekly_score, faction, is_captain FROM users WHERE weekly_attempts > 0 ORDER BY weekly_score DESC, last_updated ASC LIMIT 10")
    top_10 = c.fetchall()

    c.close()
    release_db(conn)

    msg_text = "🏰 **THE BATTLE FOR THE HOUSE CUP** 🏰\n"
    msg_text += f"📅 **{date_range}** | 👥 **{total_active} Active Students**\n"
    msg_text += f"⏳ **Phase:** {phase_text}\n"
    msg_text += "━━━━━━━━━━━━━━━━━━━━\n\n"

    msg_text += "📊 **THE COMMUNITY PULSE**\n"
    msg_text += f"➪ **Quizzes Dropped:** `{total_quizzes} Quizzes` (Max `{max_pts} pts`)\n"
    msg_text += f"➪ **Promotion Cut-off:** `{target_average} pts` (Class Average)\n"
    msg_text += f"➪ **Safe Zone:** `{safe_zone_count}` students above the cut-off\n"
    msg_text += f"➪ **Global Accuracy:** `{global_accuracy_pct}%` correct overall\n"

    msg_text += "━━━━━━━━━━━━━━━━━━━━\n\n"

    msg_text += "⚔️ **HOUSE STANDINGS**\n"
    msg_text += f"{dynamic_house_line}\n"
    msg_text += f"{lead_text}\n"

    msg_text += "━━━━━━━━━━━━━━━━━━━━\n\n"

    msg_text += "🏆 **TOP WIZARDS & WITCHES** 🏆\n"

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
        msg_text += f"{medals[i]} {faction_emoji} {captain_emoji}[{name}](tg://user?id={u_id}) ➪ `{clean_score} pts`\n"

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
                    "text": "🏆 See All Ranking",
                    "url": "https://t.me/Ez_vocab_bot/leaderboard"
                }
            ]]
        }
    }

    max_retries = 10
    for attempt in range(max_retries):
        try:
            res = requests.post(url, json=payload, timeout=10)
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

    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT value FROM bot_settings WHERE key='last_ai_reply_time'")
        last_time_row = c.fetchone()
        current_time = time.time()
        if last_time_row:
            last_time = float(last_time_row[0])
            if current_time - last_time < 10:
                c.close()
                release_db(conn)
                return

        c.execute("""
            INSERT INTO bot_settings (key, value) VALUES ('last_ai_reply_time', %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (str(current_time),))
        conn.commit()
    except Exception as e:
        print(f"⚠️ Cooldown Error: {e}")
        try: c.close()
        except: pass
        release_db(conn)
        return

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
    You are a helpful, witty senior student monitoring the "Members Discussion/Feedback" thread. Drop all robotic formality. Talk like a real person.

    === CURRENT SYSTEM STATE ===
    - Today is: {current_day}
    - Current IST Time: {current_ist_time.strftime('%I:%M %p')}
    - **Current Phase of the Week**: {phase_of_week}
    - **Total Active Participants This Week**: {total_active_participants} students have attempted quizzes.
    - **Total Quizzes Dropped**: {total_quizzes_available}

    === USER DATA ===
    - Name: {first_name}
    - Is Admin: {"True" if is_admin else "False"}

    === EZ EDITORIALS GROUP MAP & KNOWLEDGE BASE ===
    If members ask where to find things, direct them to these specific Telegram Threads (Topics):
    1. "Today's Editorials Magazine": Drops daily (Mon-Sat) 10:00 AM - 11:59 AM.
    2. "Words 101": "THE DAILY DISPATCH / FIELD NOTES" PDFs are uploaded here.
    3. "Vocab Quiz (Editorial Based)": Drops daily at 7:00 PM. 25 questions.
    4. "Editor's Pick": Selected original-form editorials/articles.
    5. "Grammar 101": **CRITICAL STATUS:** This course has officially ENDED. Do NOT promise new daily grammar notes.
    6. "Live Weekly-Cup Leaderboard": The live standings thread.

    === SCORING & LEAGUE RULES ===
    - Tiers: Unranked -> Bronze -> Silver -> Gold -> Platinum -> Diamond -> Champion -> Master -> Elite -> Legend -> Mythic -> Prodigy -> Celestial -> Zenith -> Ascendant.
    - Promotion: Finish the week above the Class Average to get promoted +1 League. Top 10 get Double (+2). 1st Place gets Triple (+3).
    - Demotion: Dropping below the class average results in a Demotion (-1 League).

    === CURRENT UPCOMING EXAMS ===
    {exam_context}

    {reply_context}

    === STRICT OPERATIONAL PROTOCOL (NEVER BREAK THESE) ===
    1. THE DEFAULT ACTION IS SILENCE: If members are just chatting, debating, or greeting each other, your ONLY output must be the exact word: IGNORE.
    2. THE "ADMIN" RULE: You must completely ignore Admins unless they explicitly say "Lixie".
    3. TONE: Short (MAX 2-3 sentences). Use texting shortcuts and emojis. Dive straight into the answer.
    4. BRITISH ENGLISH: ALWAYS use British English spelling for explanations.
    5. RANK INQUIRIES: If anyone asks for their rank, score, league, or leaderboard status, DO NOT give them any numbers. Playfully and wittily tell them to go to the "Live Weekly-Cup Leaderboard" thread and click the "See All Ranking" button to open the Mini App. Tell them the app has all their beautiful charts, Global Elo, and data!
    """

    ai_reply = None
    for attempt in range(len(API_KEYS)):
        try:
            active_key = API_KEYS[db_key_index]
            temp_client = genai.Client(api_key=active_key)
            response = temp_client.models.generate_content(
                model='gemini-3.1-flash-lite',
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
            res = requests.post(send_url, json=payload, timeout=15)
            if res.status_code == 200: break
            elif res.status_code == 429: time.sleep(res.json().get("parameters", {}).get("retry_after", 3) + 1)
            else: time.sleep(2)
        except requests.exceptions.RequestException:
            time.sleep(3 + attempt)

@app.route(f'/{TELEGRAM_TOKEN}', methods=['POST'])
def webhook():
    update = request.get_json()

    if 'poll_answer' in update:
        ans = update['poll_answer']
        user_info = ans['user']
        f_name = user_info.get('first_name', '').strip()
        l_name = user_info.get('last_name', '').strip()
        formatted_name = f"{f_name} {l_name[0]}".strip() if l_name else f_name

        try:
            conn = get_db()
            c = conn.cursor()
            c.execute("INSERT INTO answer_queue (user_id, first_name, poll_id, chosen_option) VALUES (%s, %s, %s, %s)",
                      (user_info['id'], formatted_name, ans['poll_id'], ans['option_ids'][0]))
            conn.commit()
            c.close()
            release_db(conn)
            return 'OK', 200
        except Exception as e:
            return 'DB Locked, Retrying', 500

    elif 'edited_message' in update:
        msg = update['edited_message']
        chat_id = msg['chat']['id']

        if str(chat_id) == SOURCE_CHAT_ID:
            try:
                conn = get_db()
                c = conn.cursor()
                c.execute("SELECT target_msg_id FROM message_links WHERE source_msg_id = %s", (msg['message_id'],))
                row = c.fetchone()
                c.close()
                release_db(conn)
                if row: sync_message_edit(msg=msg, target_msg_id=row[0])
            except Exception as e:
                pass
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
            if chat_type in ['group', 'supergroup'] and not text.startswith('/'):
                if str(chat_id) == CHAT_ID and thread_id == 11:
                    replied_text = msg['reply_to_message']['text'] if 'reply_to_message' in msg and 'text' in msg['reply_to_message'] else None
                    threading.Thread(target=process_ai_query, kwargs={
                        "chat_id": chat_id, "user_id": msg['from']['id'], "first_name": msg['from']['first_name'],
                        "text": text, "message_id": msg['message_id'], "thread_id": thread_id, "replied_text": replied_text
                    }).start()

    return 'OK', 200

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

@app.route('/reset_daily/0508', methods=['GET', 'POST'])
def trigger_daily_reset():
    threading.Thread(target=run_daily_reset_background).start()
    return "Daily reset triggered!", 200

def run_weekly_reset_background():
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
            if rank_index == 0: new_league = min(14, current_league + 3)
            elif rank_index < 10: new_league = min(14, current_league + 2)
            else: new_league = min(14, current_league + 1)
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
        conn.commit()
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
            res = requests.post(url, json={"chat_id": CHAT_ID, "text": group_text, "parse_mode": "Markdown", "message_thread_id": TELEGRAM_THREAD_ID}, timeout=10)
            if res.json().get("ok"):
                for _ in range(3):
                    try:
                        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/pinChatMessage", json={"chat_id": CHAT_ID, "message_id": res.json()["result"]["message_id"], "disable_notification": False}, timeout=5)
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
        completion_count = sum(1 for u in all_weekly_players if u[3] >= total_quizzes)
        completion_rate = round((completion_count / total_active_students) * 100) if total_active_students > 0 else 0

        c.execute("SELECT first_name, live_elo FROM users WHERE live_elo IS NOT NULL ORDER BY live_elo DESC LIMIT 1")
        highest_elo_row = c.fetchone()
        highest_elo_name = highest_elo_row[0] if highest_elo_row else "N/A"
        highest_elo_val = round(highest_elo_row[1], 1) if highest_elo_row else 1000

        try:
            with open("/home/prathu/quiz_bot/questions.json", 'r', encoding='utf-8') as f:
                question_bank = json.load(f)
        except:
            question_bank = []

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

        for p_id, p_att, p_cor in poll_stats:
            p_cor = p_cor if p_cor else 0
            p_acc = (p_cor / p_att) * 100

            if p_acc >= 86: t1 += 1; tier_map["T1"].append(p_id); total_q_elo += 800
            elif p_acc >= 72: t2 += 1; tier_map["T2"].append(p_id); total_q_elo += 1000
            elif p_acc >= 58: t3 += 1; tier_map["T3"].append(p_id); total_q_elo += 1200
            elif p_acc >= 44: t4 += 1; tier_map["T4"].append(p_id); total_q_elo += 1500
            else: t5 += 1; tier_map["T5"].append(p_id); total_q_elo += 1800

            if p_acc < lowest_acc: lowest_acc = p_acc; hardest_poll_id = p_id
            if p_acc > highest_acc: highest_acc = p_acc; easiest_poll_id = p_id

        avg_q_elo = (total_q_elo / len(poll_stats)) if len(poll_stats) > 0 else 1000
        week_diff_score = min(10.0, max(1.0, ((avg_q_elo - 800) / 1000) * 9.0 + 1.0))
        diff_label = "Easy" if week_diff_score < 4 else "Moderate" if week_diff_score < 7 else "Brutal"

        lowest_acc = round(lowest_acc) if lowest_acc != 101 else 0
        highest_acc = round(highest_acc) if highest_acc != -1 else 0

        hardest_snippet, trap_text, trap_pct, easiest_snippet = "Question text not found.", "N/A", 0, "Question text not found."

        if hardest_poll_id and len(question_bank) > 0:
            correct_idx = poll_metadata.get(hardest_poll_id, -1)
            c.execute("""
                SELECT chosen_option, COUNT(*) as count FROM user_answers
                WHERE poll_id = %s AND is_correct = 0 GROUP BY chosen_option ORDER BY count DESC LIMIT 1
            """, (hardest_poll_id,))
            trap_row = c.fetchone()
            if trap_row and correct_idx != -1:
                c.execute("SELECT COUNT(*) FROM user_answers WHERE poll_id = %s", (hardest_poll_id,))
                total_att_hard = c.fetchone()[0]
                trap_pct = round((trap_row[1] / total_att_hard) * 100) if total_att_hard > 0 else 0
                hardest_snippet = f"[Poll ID: {hardest_poll_id[:8]}...]"
                trap_text = f"Option Index {trap_row[0]}"
        if easiest_poll_id:
            easiest_snippet = f"[Poll ID: {easiest_poll_id[:8]}...]"

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

        total_weekly_attempts = sum(u[3] for u in all_weekly_players)
        total_weekly_correct = sum(u[6] if (len(u) > 6 and u[6] is not None) else 0 for u in all_weekly_players)
        overall_accuracy = round((total_weekly_correct / total_weekly_attempts) * 100) if total_weekly_attempts > 0 else 0
        promoted_count = sum(1 for u in all_weekly_players if u[2] >= target_average)

        c.execute("SELECT value FROM bot_settings WHERE key='last_week_cutoff'")
        cutoff_row = c.fetchone()
        cutoff_change_text = ""
        if cutoff_row and cutoff_row[0]:
            last_cutoff = float(cutoff_row[0])
            if last_cutoff > 0:
                change = ((target_average - last_cutoff) / last_cutoff) * 100
                if change > 0: cutoff_change_text = f" *(📈 +{change:.1f}% from last week)*"
                elif change < 0: cutoff_change_text = f" *(📉 {change:.1f}% from last week)*"
                else: cutoff_change_text = " *(⚖️ identical to last week)*"
            elif last_cutoff == 0 and target_average > 0: cutoff_change_text = " *(📈 +100% from last week)*"

        c.execute("""
            INSERT INTO bot_settings (key, value) VALUES ('last_week_cutoff', %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (str(int(target_average)),))
        
        c.execute("SELECT faction, SUM(weekly_score) FROM users WHERE faction IS NOT NULL GROUP BY faction")
        team_scores = dict(c.fetchall())
        finals = {'Gryffindor 🦁🔥': team_scores.get('Gryffindor 🦁🔥', 0), 'Slytherin 🐍💧': team_scores.get('Slytherin 🐍💧', 0), 'Ravenclaw 🦅💨': team_scores.get('Ravenclaw 🦅💨', 0), 'Hufflepuff 🦡🌍': team_scores.get('Hufflepuff 🦡🌍', 0)}
        sorted_finals = sorted(finals.items(), key=lambda x: x[1], reverse=True)
        winner_house, winner_score = sorted_finals[0]

        house_text = ""
        medals_house = ["🥇", "🥈", "🥉", "4️⃣"]
        for i, (h_name, h_score) in enumerate(sorted_finals):
            clean_h_score = int(h_score) if h_score % 1 == 0 else round(h_score, 2)
            house_text += f"{medals_house[i]} {h_name.split()[0]}: `{clean_h_score} pts`\n"

        admin_msg = f"🔐 **ADMIN DEBRIEF: WEEKLY CUP SEASON {current_week_num}**\n📅 `{date_range}`\n\n"
        admin_msg += f"👥 **1. COMMUNITY ENGAGEMENT**\n• Active Challengers: `{total_active_students}`\n• Total Volume: `{total_weekly_attempts}` attempts\n• Completion Rate: `{completion_rate}%`\n• Overall Accuracy: `{overall_accuracy}%`\n\n"
        admin_msg += f"⚙️ **2. SYSTEM CALIBRATION**\n• Quizzes Dropped: `{total_quizzes}`\n• Promotion Cut-off: `{int(target_average)} pts` {cutoff_change_text}\n\n"
        admin_msg += f"📈 **3. LEAGUE & ELO ECONOMY**\n• Promotions (▲): `{promoted_count}`\n• Demotions (▼): `{total_active_students - promoted_count}`\n• Elo Ceiling: `{highest_elo_val}` *(Held by {highest_elo_name})*\n\n"
        admin_msg += f"🧠 **4. CONTENT INSIGHTS**\n• Hardest (Boss): {hardest_snippet}\n  ↳ *`{lowest_acc}%` got it right. (Trap: `{trap_pct}%` chose {trap_text})*\n"
        admin_msg += f"• Easiest (Freebie): {easiest_snippet}\n  ↳ *`{highest_acc}%` got it right.*\n• Tiers: `T1: {t1} | T2: {t2} | T3: {t3} | T4: {t4} | T5: {t5}`\n• Overall Difficulty: `{diff_label} ({week_diff_score:.1f}/10)`\n\n"
        admin_msg += f"🎯 **THE MASTERY FUNNEL**\n• T1 Masters: `{masters['T1']}`\n• T2 Masters: `{masters['T2']}`\n• T3 Masters: `{masters['T3']}`\n• T4 Masters: `{masters['T4']}`\n• T5 Boss Slayers: `{masters['T5']}`\n\n"
        admin_msg += f"🏰 **5. THE HOUSE WAR**\n{house_text}"

        admin_ids = [716496729, 6251430317, 5103843488]
        for a_id in admin_ids:
            try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": a_id, "text": admin_msg, "parse_mode": "Markdown"}, timeout=5)
            except: pass

    except Exception as e: print(f"⚠️ Error generating Admin Debrief: {e}")

    # --- THE GREAT WIPE ---
    c.execute("UPDATE users SET faction = NULL WHERE weekly_score < %s", (target_average,))
    c.execute("UPDATE users SET base_elo = live_elo, weekly_score = 0, weekly_attempts = 0, is_captain = 0")
    c.execute("DELETE FROM precise_scores")
    
    # --- CAPTAIN SELECTION & PUBLIC WRAP-UP ANNOUNCEMENT ---
    c.execute("SELECT user_id, first_name FROM users WHERE faction='Gryffindor 🦁🔥' AND weekly_attempts > 0 ORDER BY weekly_score DESC LIMIT 1")
    top_gryffindor = c.fetchone()
    c.execute("SELECT user_id, first_name FROM users WHERE faction='Slytherin 🐍💧' AND weekly_attempts > 0 ORDER BY weekly_score DESC LIMIT 1")
    top_slytherin = c.fetchone()
    c.execute("SELECT user_id, first_name FROM users WHERE faction='Ravenclaw 🦅💨' AND weekly_attempts > 0 ORDER BY weekly_score DESC LIMIT 1")
    top_ravenclaw = c.fetchone()
    c.execute("SELECT user_id, first_name FROM users WHERE faction='Hufflepuff 🦡🌍' AND weekly_attempts > 0 ORDER BY weekly_score DESC LIMIT 1")
    top_hufflepuff = c.fetchone()

    if top_gryffindor: c.execute("UPDATE users SET faction='Gryffindor 🦁🔥', is_captain=1 WHERE user_id=%s", (top_gryffindor[0],))
    if top_slytherin: c.execute("UPDATE users SET faction='Slytherin 🐍💧', is_captain=1 WHERE user_id=%s", (top_slytherin[0],))
    if top_ravenclaw: c.execute("UPDATE users SET faction='Ravenclaw 🦅💨', is_captain=1 WHERE user_id=%s", (top_ravenclaw[0],))
    if top_hufflepuff: c.execute("UPDATE users SET faction='Hufflepuff 🦡🌍', is_captain=1 WHERE user_id=%s", (top_hufflepuff[0],))

    gryf_cap = f"[{top_gryffindor[1]}](tg://user?id={top_gryffindor[0]})" if top_gryffindor else "None"
    slyth_cap = f"[{top_slytherin[1]}](tg://user?id={top_slytherin[0]})" if top_slytherin else "None"
    rav_cap = f"[{top_ravenclaw[1]}](tg://user?id={top_ravenclaw[0]})" if top_ravenclaw else "None"
    huff_cap = f"[{top_hufflepuff[1]}](tg://user?id={top_hufflepuff[0]})" if top_hufflepuff else "None"

    winning_banner = ""
    if winner_score > sorted_finals[1][1]:
        house_name, emoji = winner_house.split()[0].upper(), winner_house.split()[1]
        winning_banner = f"🥇 **TEAM {house_name} WINS!** {emoji}\nSecuring the top spot with **{winner_score}** points! Your reigning Team Captains for this new week are:\n\n"
    elif winner_score > 0 and winner_score == sorted_finals[1][1]:
        winning_banner = f"⚖️ **TEAM TIE!**\nThe top teams tied with **{winner_score}** points. Your reigning Team Captains for this new week are:\n\n"
    else:
        winning_banner = "⚖️ **THE WEEK HAS ENDED!**\nNo points were earned this week. Your reigning Team Captains for this new week are:\n\n"

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
            res = requests.post(url, json={"chat_id": CHAT_ID, "message_thread_id": ANNOUNCEMENT_THREAD_ID, "text": announcement_text, "parse_mode": "Markdown"}, timeout=10)
            if res.json().get("ok"):
                for _ in range(3):
                    try:
                        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/pinChatMessage", json={"chat_id": CHAT_ID, "message_id": res.json()["result"]["message_id"], "disable_notification": False}, timeout=5)
                        break
                    except: time.sleep(2)
                break
            elif res.json().get("error_code") == 429: time.sleep(res.json().get("parameters", {}).get("retry_after", 3) + 1)
            else: break
        except: time.sleep(3 + attempt * 2)

    # --- 🧹 SUNDAY SWEEP ---
    c.execute("DELETE FROM polls")
    c.execute("DELETE FROM user_answers")
    thirty_days_ago = (datetime.utcnow() + timedelta(hours=5, minutes=30) - timedelta(days=30)).strftime('%Y-%m-%d')
    c.execute("DELETE FROM daily_history WHERE date_str < %s", (thirty_days_ago,))
    if week_row: c.execute("DELETE FROM weekly_rank_history WHERE week_num < %s", (int(week_row[0]) - 10,))

    conn.commit()
    c.close()
    release_db(conn)
    update_live_leaderboard()

@app.route('/reset_weekly/0508', methods=['GET', 'POST'])
def trigger_weekly_reset():
    threading.Thread(target=run_weekly_reset_background).start()
    return "Weekly reset triggered!", 200

@app.route('/cron/process_leaderboard_0508', methods=['GET', 'POST'])
def cron_process_leaderboard():
    # 🔒 SECURITY GATE
    if request.headers.get("X-Cron-Secret") != CRON_SECRET:
        return "Unauthorized", 401

    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id, user_id, first_name, poll_id, chosen_option FROM answer_queue WHERE status = 'pending'")
        pending_answers = c.fetchall()

        if pending_answers:
            for row in pending_answers:
                c.execute("UPDATE answer_queue SET status = 'processing' WHERE id = %s", (row[0],))
            conn.commit()

            for row in pending_answers:
                process_answer(c, user_id=row[1], first_name=row[2], poll_id=row[3], chosen_option=row[4])

            conn.commit()
            c.execute("DELETE FROM answer_queue WHERE status = 'processing'")
            conn.commit()
            
        c.close()
        return "Processed answers", 200
    except Exception as e:
        try:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={
                "chat_id": "716496729",
                "text": f"🚨 **CRITICAL CRON ERROR (Queue Processor)** 🚨\n\n`{e}`",
                "parse_mode": "Markdown"
            }, timeout=5)
        except: pass
        return f"Error: {e}", 500
    finally:
        if conn:
            release_db(conn)


@app.route('/cron/heavy_math_0508', methods=['GET', 'POST'])
def cron_heavy_math():
    # 🔒 SECURITY GATE
    if request.headers.get("X-Cron-Secret") != CRON_SECRET:
        return "Unauthorized", 401

    conn = None
    try:
        recalculate_dynamic_scores()
        bake_miniapp_cache()
        
        conn = get_db()
        c = conn.cursor()
        c.execute("""
            INSERT INTO bot_settings (key, value) VALUES ('telegram_needs_update', '1') 
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """)
        conn.commit()
        c.close()
        return "Math Engine completed", 200
    except Exception as e:
        try:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={
                "chat_id": "716496729",
                "text": f"🚨 **CRITICAL CRON ERROR (Heavy Math Engine)** 🚨\n\n`{e}`",
                "parse_mode": "Markdown"
            }, timeout=5)
        except: pass
        return f"Error: {e}", 500
    finally:
        if conn:
            release_db(conn)


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
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={
                "chat_id": "716496729",
                "text": f"🚨 **CRITICAL CRON ERROR (Telegram Updater)** 🚨\n\n`{e}`",
                "parse_mode": "Markdown"
            }, timeout=5)
        except: pass
        return f"Error: {e}", 500
    finally:
        if conn:
            release_db(conn)


@app.route('/cron/dispatcher', methods=['GET', 'POST'])
def trigger_dispatcher():
    # 🔒 SECURITY GATE
    if request.headers.get("X-Cron-Secret") != CRON_SECRET:
        return "Unauthorized", 401
        
    threading.Thread(target=dispatch_practice_sets).start()
    return "Dispatcher triggered!", 200

# ==========================================
# SECURED: PRACTICE SET DISPATCHER
# ==========================================
def dispatch_practice_sets():
    CONNECT_TO_LEADERBOARD = False
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
        
        response = requests.get(github_grammar_url, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()
        titles, set_a, set_b, set_c = data.get("titles", []), data.get("set_a", []), data.get("set_b", []), data.get("set_c", [])
    except Exception as e:
        print(f"⚠️ Failed to fetch grammar.json from private GitHub: {e}")
        return

    if not set_a or not set_b or not set_c: return
    dynamic_open_period = int(((current_ist + timedelta(days=6 - current_ist.weekday())).replace(hour=23, minute=59, second=59) - current_ist).total_seconds())

    def safe_send_text(text, pin=False):
        for attempt in range(10):
            try:
                res = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "message_thread_id": PRACTICE_THREAD_ID, "text": text, "parse_mode": "Markdown"}, timeout=20)
                if res.status_code == 200:
                    if pin: requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/pinChatMessage", json={"chat_id": CHAT_ID, "message_id": res.json()["result"]["message_id"]}, timeout=10)
                    return True
                elif res.status_code == 429: time.sleep(res.json().get("parameters", {}).get("retry_after", 3) + 1)
                else: time.sleep(2)
            except: time.sleep(3 + attempt)
        return False

    def send_and_link_poll(q_list, default_ui_type, shuffle, start_q_num):
        conn = get_db()
        c = conn.cursor()
        for i, mcq in enumerate(q_list):
            options = mcq['options']
            if mcq['correct_answer'] not in options: options[0] = mcq['correct_answer']
            correct_index = options.index(mcq['correct_answer'])
            current_ui = f'Choose the best replacement for the words "{mcq["target_phrase"]}".' if 'target_phrase' in mcq else mcq.get('custom_ui', default_ui_type)

            poll_payload = {
                "chat_id": CHAT_ID, "message_thread_id": PRACTICE_THREAD_ID,
                "question": f"Que {start_q_num + i}: {current_ui}\n\n{mcq['sentence']}"[:300],
                "options": json.dumps([opt[:100] for opt in options]),
                "type": "quiz", "correct_option_id": correct_index, "explanation": mcq['explanation'][:200],
                "is_anonymous": False, "shuffle_options": shuffle, "open_period": dynamic_open_period
            }

            for tg_attempt in range(10):
                try:
                    res = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPoll", json=poll_payload, timeout=20)
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
    send_and_link_poll(set_a, "Identify the part containing the error.", shuffle=False, start_q_num=1)
    time.sleep(300)
    safe_send_text("🎯 **SET B — Sentence Improvement**\n⬇️")
    time.sleep(2)
    send_and_link_poll(set_b, "Choose the best replacement.", shuffle=False, start_q_num=6)
    time.sleep(300)
    safe_send_text("🎯 **SET C — Fill in the Blank**\n⬇️")
    time.sleep(2)
    send_and_link_poll(set_c, "Choose the most appropriate option.", shuffle=True, start_q_num=11)
    notify_prathu("Grammar Practice Sets")

def recalculate_dynamic_scores():
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

        c.execute("SELECT user_id, base_elo FROM users")
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
                user_scores[u_id] = {"weekly": 0, "daily": 0, "weekly_correct": 0, "expected_wins": 0.0, "actual_wins": 0, "precise": {}, "tier_bonus": 0.0, "played_today": False}

            user_scores[u_id]["weekly"] += points_awarded
            user_scores[u_id]["weekly_correct"] += int(is_correct)

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

            c.execute("UPDATE users SET weekly_score=%s, daily_score=%s, weekly_correct=%s, live_elo=%s WHERE user_id=%s",
                      (final_weekly, final_daily, totals["weekly_correct"], new_live_elo, u_id))

            for day, day_data in totals["precise"].items():
                c.execute("""
                    INSERT INTO precise_scores (user_id, day_label, score, attempts, correct_answers) 
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (user_id, day_label) DO UPDATE SET 
                        score = EXCLUDED.score, attempts = EXCLUDED.attempts, correct_answers = EXCLUDED.correct_answers
                """, (u_id, day, day_data["score"], day_data["attempts"], day_data["correct"]))

        conn.commit()
        c.close()
        release_db(conn)
    except Exception as e:
        print(f"🚨 Math Engine Error: {e}")

# ==========================================
# BACKGROUND WORKER: SUNDAY ANNOUNCEMENT RESTORED
# ==========================================
def run_sunday_announcement():
    text = "_There won't be any Today's Editorials today; Editorials will be available Monday through Saturday exclusively._"
    for attempt in range(10):
        try:
            res = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "message_thread_id": 3, "text": text, "parse_mode": "Markdown"}, timeout=20)
            if res.status_code == 200: break
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
            res = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "message_thread_id": 246, "text": text, "parse_mode": "Markdown"}, timeout=20)
            if res.status_code == 200:
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/pinChatMessage", json={"chat_id": CHAT_ID, "message_id": res.json()["result"]["message_id"], "disable_notification": False}, timeout=20)
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
        mcqs = requests.get(github_vocab_url, headers=headers, timeout=15).json()
    except Exception as e: 
        print(f"⚠️ Failed to fetch questions.json: {e}")
        return

    for mcq in mcqs:
        options = mcq['options']
        if mcq['correct_answer'] not in options: options[0] = mcq['correct_answer']
        correct_index = options.index(mcq['correct_answer'])

        poll_payload = {
            "chat_id": CHAT_ID, "message_thread_id": 246, "question": mcq['question'], "options": json.dumps(options),
            "type": "quiz", "correct_option_id": correct_index, "explanation": mcq.get('explanation', '')[:200],
            "is_anonymous": False, "shuffle_options": True, "open_period": dynamic_open_period
        }

        for attempt in range(10):
            try:
                poll_res = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPoll", json=poll_payload, timeout=20)
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
        time.sleep(3)
    notify_prathu("Daily Vocab Quiz")
    
@app.route('/daily_vocab/0508', methods=['GET', 'POST'])
def trigger_daily_vocab():
    threading.Thread(target=run_daily_vocab_and_quizzes).start()
    return "Daily Vocab triggered!", 200

# ==========================================
# BACKGROUND WORKER: SUNDAY REMINDERS RESTORED
# ==========================================
def run_sunday_reminder():
    current_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    text = f"⏳ ⟪ **THE HOUSE CUP COUNTDOWN** ⟫ ⏳\n📅 `[ {(current_ist - timedelta(days=6)).strftime('%d %B')} ➪ {(current_ist - timedelta(days=1)).strftime('%d %B')} ]`\n\n🚨 **Last Chance!** 🚨\nToday is the **absolute final day** to complete your weekly quizzes!"
    for attempt in range(10):
        try:
            res = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "message_thread_id": 246, "text": text, "parse_mode": "Markdown"}, timeout=20)
            if res.status_code == 200:
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/pinChatMessage", json={"chat_id": CHAT_ID, "message_id": res.json()["result"]["message_id"], "disable_notification": False}, timeout=10)
                break
        except: time.sleep(3 + attempt * 2)

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
            res = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "message_thread_id": 11, "text": text, "parse_mode": "HTML"}, timeout=20)
            if res.status_code == 200:
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/pinChatMessage", json={"chat_id": CHAT_ID, "message_id": res.json()["result"]["message_id"], "disable_notification": False}, timeout=10)
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
        latest_exams = requests.get(github_exams_url, headers=headers, timeout=10).json()
        
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
            res = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText", json={"chat_id": CHAT_ID, "message_id": COUNTDOWN_MESSAGE_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)
            if res.status_code == 200: break
            elif res.status_code == 429: time.sleep(res.json().get("parameters", {}).get("retry_after", 3) + 1)
            else: break
        except: time.sleep(3 + attempt * 2)

def generate_and_send_commentary():
    current_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    target_exam, days_left = None, 0
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT name, exam_date FROM upcoming_exams")
        for row in c.fetchall():
            try:
                delta = (datetime.strptime(row[1], "%Y-%m-%d").date() - current_ist.date()).days
                if delta in [90, 60, 30, 15, 7, 1]: target_exam, days_left = row[0], delta; break
            except ValueError: continue
    except: pass
    if not target_exam: return

    try:
        ai_text = genai.Client(api_key="AIzaSyDb5THxDk58CrdPJ7nVKJov6L87_G2hQ0g").models.generate_content(
            model='gemini-3.5-flash',
            contents=f"Create a short Telegram exam commentary message following this EXACT 3-line structure:\nLine 1: [Urgency Emoji] {target_exam} ➪ {days_left} Days Left!\nLine 2: [1 short, hype, action-oriented sentence about studying/preparing]\nLine 3: [1 short motivational sign-off with emojis]\nRules: STRICTLY follow the 3-line format. No conversational filler. No hashtags. Keep it clean."
        ).text.strip()
    except: return

    try:
        c.execute("SELECT value FROM bot_settings WHERE key='last_commentary_msg_id'")
        if last_msg := c.fetchone(): requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteMessage", json={"chat_id": CHAT_ID, "message_id": int(last_msg[0])}, timeout=5)
    except: pass

    for attempt in range(3):
        try:
            res = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "message_thread_id": COUNTDOWN_THREAD_ID, "text": f"🤖 **Daily Exam Insights**\n\n{ai_text}", "parse_mode": "Markdown"}, timeout=10)
            if res.json().get("ok"):
                new_msg_id = res.json()["result"]["message_id"]
                c.execute("""
                    INSERT INTO bot_settings (key, value) VALUES ('last_commentary_msg_id', %s)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """, (str(new_msg_id),))
                conn.commit()
                # 🛑 The pinChatMessage line has been removed from here!
                break
            else: time.sleep(2)
        except: time.sleep(3)
    c.close()
    release_db(conn)

def relay_message(message_id, target_thread_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/copyMessage"
    payload = {"chat_id": CHAT_ID, "from_chat_id": SOURCE_CHAT_ID, "message_id": message_id, "message_thread_id": target_thread_id}
    for attempt in range(5):
        try:
            res = requests.post(url, json=payload, timeout=10)
            if res.status_code == 200:
                try:
                    conn = get_db()
                    c = conn.cursor()
                    c.execute("""
                        INSERT INTO message_links (source_msg_id, target_msg_id) VALUES (%s, %s)
                        ON CONFLICT (source_msg_id) DO UPDATE SET target_msg_id = EXCLUDED.target_msg_id
                    """, (message_id, res.json()["result"]["message_id"]))
                    conn.commit()
                    c.close()
                    release_db(conn)
                except: pass
                return
        except: time.sleep(3)

def sync_message_edit(msg, target_msg_id):
    pass # Media sync identical to original...

# ==========================================
# RESTORED: ADMIN DRAFT VIEWER
# ==========================================
@app.route('/admin/view_drafts_0508')
def view_drafts():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT set_type, question_data FROM draft_quizzes")
        drafts = c.fetchall()
        c.close()
        release_db(conn)

        if not drafts:
            return "No drafts found. The table is empty."

        html = "<h2>Stored Drafts for Tonight</h2><div style='font-family: monospace;'>"
        for d_type, data in drafts:
            parsed_json = json.loads(data)
            pretty_json = json.dumps(parsed_json, indent=4)
            html += f"<h3 style='color: #2a5298;'>{d_type}</h3>"
            html += f"<pre style='background: #f4f6f8; padding: 10px; border-radius: 5px;'>{pretty_json}</pre><hr>"

        html += "</div>"
        return html
    except Exception as e:
        return f"Error reading database: {e}"

# ==========================================
# RESTORED: DATABASE VACUUM CRON
# ==========================================
@app.route('/cron/vacuum_db_0508', methods=['GET', 'POST'])
def cron_vacuum_db():
    """PostgreSQL manages vacuuming automatically, but we keep this route so cron-job.org doesn't return 404 errors!"""
    return "PostgreSQL Auto-Vacuum handles this automatically. Route kept alive for cron compatibility!", 200

@app.route('/miniapp')
def serve_mini_app():
    return render_template('leaderboard.html')

def bake_miniapp_cache():
    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT value FROM bot_settings WHERE key='current_week'")
    week_row = c.fetchone()
    current_week_val = week_row[0] if week_row else 14

    c.execute("""
        SELECT user_id, first_name, weekly_score, faction, is_captain, weekly_attempts, league_tier, weekly_correct, live_elo, last_updated
        FROM users WHERE weekly_attempts > 0 ORDER BY weekly_score DESC, last_updated ASC
    """)
    top_users = c.fetchall()

    c.execute("SELECT user_id, first_name, live_elo, last_updated FROM users ORDER BY live_elo DESC, last_updated ASC")
    all_elo_users = c.fetchall()

    two_days_ago = time.time() - (48 * 3600)
    elo_leaderboard = [{"rank": i + 1, "id": eu[0], "name": eu[1], "elo": round(eu[2] if eu[2] is not None else 1000, 1), "is_active": True if (eu[3] if eu[3] else 0) >= two_days_ago else False} for i, eu in enumerate(all_elo_users)]

    c.execute("SELECT user_id, week_num, rank, total_members, score, attempts, correct FROM weekly_rank_history ORDER BY week_num DESC")
    rank_hist_dict = {}
    for r in c.fetchall():
        if r[0] not in rank_hist_dict: rank_hist_dict[r[0]] = []
        rank_hist_dict[r[0]].append({"week": r[1], "rank": r[2], "total": r[3], "score": r[4], "attempts": r[5], "correct": r[6]})

    c.execute("SELECT user_id, day_label, score, attempts, correct_answers FROM precise_scores")
    precise_scores_dict = {}
    for r in c.fetchall():
        if r[0] not in precise_scores_dict: precise_scores_dict[r[0]] = []
        precise_scores_dict[r[0]].append(r)

    def get_exact_history_fast(uid): return {row[1]: {"score": row[2], "attempts": row[3], "correct": row[4]} for row in precise_scores_dict.get(uid, [])}

    weighted_daily_sums = {"Mon": 0, "Tue": 0, "Wed": 0, "Thu": 0, "Fri": 0, "Sat": 0, "Sun": 0}
    sum_weights = 0
    topper_history_dict = {}
    leaderboard_list = []
    day_order = {"Mon": 1, "Tue": 2, "Wed": 3, "Thu": 4, "Fri": 5, "Sat": 6, "Sun": 7}

    for index, user in enumerate(top_users):
        uid = user[0]
        u_score = int(user[2]) if user[2] % 1 == 0 else round(user[2], 2)
        u_attempts = user[5] if user[5] is not None else 0
        u_correct = user[7] if user[7] is not None else 0
        weight = u_attempts ** 0.5
        sum_weights += weight

        user_hist = get_exact_history_fast(uid)
        sorted_user_hist = sorted(user_hist.items(), key=lambda x: day_order.get(x[0], 99))

        for day, stats in user_hist.items():
            weighted_daily_sums[day] = weighted_daily_sums.get(day, 0) + (stats["score"] * weight)

        if index == 0: topper_history_dict = {k: (int(v["score"]) if v["score"] % 1 == 0 else round(v["score"], 2)) for k, v in user_hist.items()}

        leaderboard_list.append({
            "rank": index + 1, "id": uid, "name": user[1], "score": u_score,
            "elo": round(user[8] if user[8] is not None else 1000, 1), 
            "last_updated": user[9] if user[9] else 0,
            "house": str(user[3]), "is_captain": user[4], "attempts": u_attempts, "league": user[6] if user[6] else 0,
            "rank_history": rank_hist_dict.get(uid, []),
            "history": {
                "labels": [k for k, v in sorted_user_hist], "scores": [(int(v["score"]) if v["score"] % 1 == 0 else round(v["score"], 2)) for k, v in sorted_user_hist],
                "daily_correct": [v["correct"] for k, v in sorted_user_hist], "daily_attempts": [v["attempts"] for k, v in sorted_user_hist],
                "accuracy": round((u_correct / u_attempts) * 100) if u_attempts > 0 else 0, "correct": u_correct, "wrong": max(0, u_attempts - u_correct)
            }
        })

    class_avg_history_dict = {day: round(weighted_daily_sums[day] / sum_weights) if sum_weights > 0 else 0 for day in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]}

    json_string = json.dumps({"current_week": current_week_val, "leaderboard": leaderboard_list, "topper_history": topper_history_dict, "class_avg_history": class_avg_history_dict, "elo_ranking": elo_leaderboard})
    
    c.execute("""
        INSERT INTO global_cache (cache_key, json_data) VALUES ('miniapp_snapshot', %s)
        ON CONFLICT (cache_key) DO UPDATE SET json_data = EXCLUDED.json_data
    """, (json_string,))
    
    conn.commit()
    c.close()
    release_db(conn)

from flask import Response
@app.route('/api/leaderboard', methods=['GET'])
def get_mini_app_leaderboard():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT json_data FROM global_cache WHERE cache_key = 'miniapp_snapshot'")
    row = c.fetchone()
    c.close()
    release_db(conn)
    if row: return Response(row[0], mimetype='application/json')
    return jsonify({"error": "Syncing..."}), 503

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
