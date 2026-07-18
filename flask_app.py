import threading
import os
import time
import json
import requests
from datetime import datetime, timedelta
from flask import Flask, request, render_template, jsonify
import psycopg2
from psycopg2 import pool
from google import genai
from google.genai import types

# --- DATABASE CONFIGURATION ---
# Paste your Supabase URL here. (On Render, we will use Environment Variables for safety later)
DB_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:FlUVu8dA8xy02woL@db.wuhoozvbufnwjsfpkojp.supabase.co:5432/postgres")

# Create a connection pool to handle thousands of rapid requests without crashing
db_pool = psycopg2.pool.SimpleConnectionPool(1, 20, DB_URL)

def get_db():
    """Fetches a high-speed connection from the cloud Postgres pool."""
    return db_pool.getconn()

def release_db(conn):
    """Safely releases the connection back to the pool."""
    db_pool.putconn(conn)

# --- AI & BOT CONFIGURATION ---
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
        INSERT INTO polls (poll_id, correct_index, poll_day) 
        VALUES (%s, %s, %s)
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
    phase_text = "Final Day! *(Locks at Midnight)*" if weekday_idx == 6 else f"Day {weekday_idx + 1} of 6 *(Competition Active)*"

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
            if attempts == 0: continue
            correct = exact_correct if exact_correct else 0
            accuracy = correct / attempts
            total_correct_global += correct
            total_attempts_global += attempts

            volume_weight = attempts / (attempts + 10.0)
            final_weight = volume_weight * accuracy
            if score < 0: final_weight = 0.0

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

    msg_text = f"🏰 **THE BATTLE FOR THE HOUSE CUP** 🏰\n📅 **{date_range}** | 👥 **{total_active} Active Students**\n⏳ **Phase:** {phase_text}\n━━━━━━━━━━━━━━━━━━━━\n\n"
    msg_text += f"📊 **THE COMMUNITY PULSE**\n➪ **Quizzes Dropped:** `{total_quizzes} Quizzes` (Max `{max_pts} pts`)\n➪ **Promotion Cut-off:** `{target_average} pts` (Class Average)\n➪ **Safe Zone:** `{safe_zone_count}` students above the cut-off\n➪ **Global Accuracy:** `{global_accuracy_pct}%` correct overall\n━━━━━━━━━━━━━━━━━━━━\n\n"
    msg_text += f"⚔️ **HOUSE STANDINGS**\n{dynamic_house_line}\n{lead_text}\n━━━━━━━━━━━━━━━━━━━━\n\n"
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
        "reply_markup": {"inline_keyboard": [[{"text": "🏆 See All Ranking", "url": "https://t.me/Ez_vocab_bot/leaderboard"}]]}
    }

    for attempt in range(10):
        try:
            res = requests.post(url, json=payload, timeout=10)
            if res.status_code == 200 or (res.status_code == 400 and "message is not modified" in res.text.lower()):
                break
            elif res.status_code == 429:
                time.sleep(res.json().get("parameters", {}).get("retry_after", 3) + 1)
            else:
                time.sleep(2)
        except requests.exceptions.RequestException:
            time.sleep(3 + attempt)

def process_ai_query(chat_id, user_id, first_name, text, message_id, thread_id, replied_text=None):
    text_lower = text.lower()
    ADMIN_IDS = [716496729, 5103843488, 6251430317]
    is_admin = user_id in ADMIN_IDS
    is_explicitly_summoned = "lixie" in text_lower
    is_asking_rank = "rank" in text_lower or "score" in text_lower
    doubt_keywords = ["what", "how", "when", "why", "where", "can you", "explain", "meaning", "synonym", "antonym", "rank", "score", "cutoff", "exam", "quiz"]
    is_asking_doubt = "?" in text_lower or any(word in text_lower for word in doubt_keywords)

    if is_admin and not is_explicitly_summoned: return
    if replied_text and not is_explicitly_summoned: return
    if not is_admin and not is_explicitly_summoned and not is_asking_doubt: return

    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT value FROM bot_settings WHERE key='last_ai_reply_time'")
        last_time_row = c.fetchone()
        current_time = time.time()
        
        if last_time_row and current_time - float(last_time_row[0]) < 10:
            c.close()
            release_db(conn)
            return

        c.execute("""
            INSERT INTO bot_settings (key, value) VALUES ('last_ai_reply_time', %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (str(current_time),))
        conn.commit()
        
        c.execute("SELECT COUNT(*) FROM polls")
        total_quizzes_available = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM users WHERE weekly_attempts > 0")
        total_active_participants = c.fetchone()[0]
        
        exam_context = ""
        c.execute("SELECT name, exam_date, status, display_date FROM upcoming_exams")
        for row in c.fetchall():
            try:
                exam_date = datetime.strptime(row[1], "%Y-%m-%d")
                if exam_date.date() >= (datetime.utcnow() + timedelta(hours=5, minutes=30)).date():
                    exam_context += f"- {row[0]}: Scheduled for {row[3]} ({row[2]})\n"
            except ValueError:
                continue

        c.execute("SELECT value FROM bot_settings WHERE key='current_key_index'")
        key_row = c.fetchone()
        db_key_index = int(key_row[0]) if key_row else 0
        
    except Exception as e:
        print(f"⚠️ DB Error AI prep: {e}")
        return
    finally:
        try: c.close()
        except: pass
        release_db(conn)

    current_ist_time = datetime.utcnow() + timedelta(hours=5, minutes=30)
    current_day = current_ist_time.strftime('%A')
    phase_of_week = "Active Competition"
    reply_context = f"\n=== CONVERSATION HISTORY ===\n\"{replied_text}\"\n" if replied_text else ""

    system_prompt = f"""
    You are Lixie, the official Moderator+ AI for the "Ez Editorials" Telegram community.
    Talk like a real person.
    === CURRENT SYSTEM STATE ===
    - Today is: {current_day}
    - Current IST Time: {current_ist_time.strftime('%I:%M %p')}
    - Phase: {phase_of_week}
    - Active Participants: {total_active_participants}
    - Quizzes Dropped: {total_quizzes_available}
    === USER DATA ===
    - Name: {first_name}
    - Is Admin: {"True" if is_admin else "False"}
    === UPCOMING EXAMS ===
    {exam_context}
    {reply_context}
    === STRICT PROTOCOL ===
    1. DEFAULT ACTION IS SILENCE: Output IGNORE if members are just chatting.
    2. Ignore Admins unless explicitly summoned.
    3. Short, witty, British English spelling.
    4. RANK INQUIRIES: Tell them to open the "Live Weekly-Cup Leaderboard" thread and click the Mini App.
    """

    ai_reply = None
    for attempt in range(len(API_KEYS)):
        try:
            active_key = API_KEYS[db_key_index]
            temp_client = genai.Client(api_key=active_key)
            response = temp_client.models.generate_content(
                model='gemini-3.1-flash-lite', contents=text,
                config=types.GenerateContentConfig(system_instruction=system_prompt, temperature=0.4)
            )
            ai_reply = response.text.strip()
            break
        except Exception as e:
            error_str = str(e).lower()
            if "429" in error_str or "quota" in error_str:
                db_key_index = (db_key_index + 1) % len(API_KEYS)
                conn = get_db()
                c = conn.cursor()
                c.execute("INSERT INTO bot_settings (key, value) VALUES ('current_key_index', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (str(db_key_index),))
                conn.commit()
                c.close()
                release_db(conn)
                continue
            else:
                return

    if not ai_reply or ai_reply == "IGNORE" or ai_reply == '"IGNORE"': return

    send_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": ai_reply, "parse_mode": "Markdown", "reply_to_message_id": message_id}
    if thread_id: payload["message_thread_id"] = thread_id

    for attempt in range(10):
        try:
            res = requests.post(send_url, json=payload, timeout=15)
            if res.status_code == 200: break
            elif res.status_code == 429: time.sleep(res.json().get("parameters", {}).get("retry_after", 3) + 1)
            else: time.sleep(2)
        except:
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

        if 'text' in msg and chat_type in ['group', 'supergroup'] and not msg['text'].startswith('/'):
            if str(chat_id) == CHAT_ID and thread_id == 11:
                replied_text = msg['reply_to_message']['text'] if 'reply_to_message' in msg and 'text' in msg['reply_to_message'] else None
                threading.Thread(target=process_ai_query, kwargs={
                    "chat_id": chat_id, "user_id": msg['from']['id'], "first_name": msg['from']['first_name'],
                    "text": msg['text'], "message_id": msg['message_id'], "thread_id": thread_id, "replied_text": replied_text
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
        week_row = c.fetchone()
        if week_row:
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
                    rank = EXCLUDED.rank, score = EXCLUDED.score, attempts = EXCLUDED.attempts, correct = EXCLUDED.correct
            """, (uid, current_week_num, rank_index + 1, total_players_this_week, u_score, u_attempts, u_correct))
        conn.commit()
    except Exception as e: print(e)

    c.execute("INSERT INTO bot_settings (key, value) VALUES ('current_week', '14') ON CONFLICT DO NOTHING")
    c.execute("SELECT value FROM bot_settings WHERE key='current_week'")
    if week_row := c.fetchone():
        c.execute("UPDATE bot_settings SET value=%s WHERE key='current_week'", (str(int(week_row[0]) + 1),))

    # Weekly wipe
    c.execute("UPDATE users SET faction = NULL WHERE weekly_score < %s", (target_average,))
    c.execute("UPDATE users SET base_elo = live_elo, weekly_score = 0, weekly_attempts = 0, is_captain = 0")
    c.execute("DELETE FROM precise_scores")
    c.execute("DELETE FROM polls")
    c.execute("DELETE FROM user_answers")

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
        release_db(conn)
        return "Processed answers", 200
    except Exception as e:
        return f"Error: {e}", 500

@app.route('/cron/heavy_math_0508', methods=['GET', 'POST'])
def cron_heavy_math():
    try:
        recalculate_dynamic_scores()
        bake_miniapp_cache()
        
        conn = get_db()
        c = conn.cursor()
        c.execute("INSERT INTO bot_settings (key, value) VALUES ('telegram_needs_update', '1') ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
        conn.commit()
        c.close()
        release_db(conn)
        
        return "Math Engine completed", 200
    except Exception as e:
        return f"Error: {e}", 500

@app.route('/cron/update_telegram_text_0508', methods=['GET', 'POST'])
def cron_update_telegram_text():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT value FROM bot_settings WHERE key='telegram_needs_update'")
        row = c.fetchone()
        
        if row and row[0] == '1':
            c.execute("UPDATE bot_settings SET value='0' WHERE key='telegram_needs_update'")
            conn.commit()
            c.close()
            release_db(conn)
            update_live_leaderboard()
            return "Telegram banner updated!", 200
        else:
            c.close()
            release_db(conn)
            return "No update needed.", 200
    except Exception as e:
        return f"Error: {e}", 500

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

        c.execute("INSERT INTO bot_settings (key, value) VALUES ('live_max_points', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (str(total_max_points),))

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

def relay_message(message_id, target_thread_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/copyMessage"
    payload = {"chat_id": CHAT_ID, "from_chat_id": SOURCE_CHAT_ID, "message_id": message_id, "message_thread_id": target_thread_id}

    for attempt in range(5):
        try:
            res = requests.post(url, json=payload, timeout=10)
            if res.status_code == 200:
                try:
                    target_msg_id = res.json()["result"]["message_id"]
                    conn = get_db()
                    c = conn.cursor()
                    c.execute("""
                        INSERT INTO message_links (source_msg_id, target_msg_id) VALUES (%s, %s)
                        ON CONFLICT (source_msg_id) DO UPDATE SET target_msg_id = EXCLUDED.target_msg_id
                    """, (message_id, target_msg_id))
                    conn.commit()
                    c.close()
                    release_db(conn)
                except Exception as e:
                    pass
                return
        except: time.sleep(3)

def sync_message_edit(msg, target_msg_id):
    pass # Unchanged media processing logic...

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
    elo_leaderboard = []
    for rank_idx, eu in enumerate(all_elo_users):
        last_up = eu[3] if eu[3] else 0
        elo_leaderboard.append({
            "rank": rank_idx + 1, "id": eu[0], "name": eu[1],
            "elo": round(eu[2] if eu[2] is not None else 1000, 1),
            "is_active": True if last_up >= two_days_ago else False
        })

    c.execute("SELECT user_id, week_num, rank, total_members, score, attempts, correct FROM weekly_rank_history ORDER BY week_num DESC")
    rank_hist_dict = {}
    for r in c.fetchall():
        uid = r[0]
        if uid not in rank_hist_dict: rank_hist_dict[uid] = []
        rank_hist_dict[uid].append({"week": r[1], "rank": r[2], "total": r[3], "score": r[4], "attempts": r[5], "correct": r[6]})

    c.execute("SELECT user_id, day_label, score, attempts, correct_answers FROM precise_scores")
    precise_scores_dict = {}
    for r in c.fetchall():
        uid = r[0]
        if uid not in precise_scores_dict: precise_scores_dict[uid] = []
        precise_scores_dict[uid].append(r)

    def get_exact_history_fast(uid):
        merged = {}
        for row in precise_scores_dict.get(uid, []): merged[row[1]] = {"score": row[2], "attempts": row[3], "correct": row[4]}
        return merged

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
        u_rank_history = rank_hist_dict.get(uid, [])
        weight = u_attempts ** 0.5
        sum_weights += weight

        user_hist = get_exact_history_fast(uid)
        sorted_user_hist = sorted(user_hist.items(), key=lambda x: day_order.get(x[0], 99))

        for day, stats in user_hist.items():
            weighted_daily_sums[day] = weighted_daily_sums.get(day, 0) + (stats["score"] * weight)

        if index == 0: topper_history_dict = {k: v["score"] for k, v in user_hist.items()}
        accuracy = round((u_correct / u_attempts) * 100) if u_attempts > 0 else 0

        leaderboard_list.append({
            "rank": index + 1, "id": uid, "name": user[1], "score": u_score,
            "elo": round(user[8] if user[8] is not None else 1000, 1), 
            "last_updated": user[9] if user[9] else 0,
            "house": str(user[3]), "is_captain": user[4], "attempts": u_attempts, "league": user[6] if user[6] else 0,
            "rank_history": u_rank_history,
            "history": {
                "labels": [k for k, v in sorted_user_hist], "scores": [v["score"] for k, v in sorted_user_hist],
                "daily_correct": [v["correct"] for k, v in sorted_user_hist], "daily_attempts": [v["attempts"] for k, v in sorted_user_hist],
                "accuracy": accuracy, "correct": u_correct, "wrong": max(0, u_attempts - u_correct)
            }
        })

    class_avg_history_dict = {day: round(weighted_daily_sums[day] / sum_weights) if sum_weights > 0 else 0 for day in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]}

    json_string = json.dumps({
        "current_week": current_week_val, "leaderboard": leaderboard_list,
        "topper_history": topper_history_dict, "class_avg_history": class_avg_history_dict, "elo_ranking": elo_leaderboard
    })
    
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

# --- RENDER PORT BINDING ---
if __name__ == '__main__':
    # Render requires web apps to bind to 0.0.0.0 and dynamically grab the PORT environment variable
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
