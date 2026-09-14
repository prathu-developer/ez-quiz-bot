import threading
import time
import requests
from datetime import datetime
from flask import Blueprint, request, jsonify
from flask_app import (
    get_db, release_db, CRON_SECRET, TELEGRAM_TOKEN, http_session,
    bake_miniapp_cache, dispatch_practice_sets, update_live_leaderboard,
    run_all_sunday_announcements, run_countdown_and_commentary,
    run_daily_reset_background, run_daily_vocab_and_quizzes,
    run_heavy_math_background, run_midnight_purge_background,
    run_mini_app_ingestion, run_queue_processor_background,
    run_quiz_unlock_announcement, run_weekly_reset_background,
    run_word_of_the_day
)

cron_bp = Blueprint('bot_cron', __name__)


@cron_bp.route('/cron/daily_purge_0508', methods=['GET', 'POST'])
def trigger_daily_purge():
    threading.Thread(target=run_midnight_purge_background).start()
    return "Midnight purge sequence initiated! Admin will receive a report.", 200


@cron_bp.route('/reset_daily/0508', methods=['GET', 'POST'])
def trigger_daily_reset():
    threading.Thread(target=run_daily_reset_background).start()
    return "Daily reset triggered!", 200


@cron_bp.route('/reset_weekly/0508', methods=['GET', 'POST'])
def trigger_weekly_reset():
    threading.Thread(target=run_weekly_reset_background).start()
    return "Weekly reset triggered!", 200


@cron_bp.route('/cron/process_leaderboard_0508', methods=['GET', 'POST'])
def cron_process_leaderboard():
    # 🔒 SECURITY GATE
    if request.headers.get("X-Cron-Secret") != CRON_SECRET:
        return "Unauthorized", 401
    
    # ✨ FIX 3: Instantly answer the cron request to prevent 30s timeouts
    threading.Thread(target=run_queue_processor_background).start()
    return "Queue Processor triggered in background!", 200


@cron_bp.route('/cron/heavy_math_0508', methods=['GET', 'POST'])
def cron_heavy_math():
    # 🔒 SECURITY GATE
    if request.headers.get("X-Cron-Secret") != CRON_SECRET:
        return "Unauthorized", 401

    # ✨ FIX: Instantly answer the cron request, then run the heavy math in the background
    threading.Thread(target=run_heavy_math_background).start()
    return "Math Engine triggered in background!", 200


@cron_bp.route('/cron/update_telegram_text_0508', methods=['GET', 'POST'])
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


@cron_bp.route('/cron/dispatcher_0508', methods=['GET', 'POST'])
def trigger_dispatcher():
    threading.Thread(target=dispatch_practice_sets).start()
    return "Dispatcher triggered!", 200


@cron_bp.route('/sunday_announcement/0508', methods=['GET', 'POST'])
def trigger_sunday_announcement():
    threading.Thread(target=run_all_sunday_announcements).start()
    return "All Sunday announcements triggered in background!", 200


@cron_bp.route('/daily_vocab/0508', methods=['GET', 'POST'])
def trigger_daily_vocab():
    threading.Thread(target=run_daily_vocab_and_quizzes).start()
    return "Daily Vocab triggered!", 200


@cron_bp.route('/update_countdown/0508', methods=['GET', 'POST'])
def trigger_countdown_update():
    threading.Thread(target=run_countdown_and_commentary).start()
    return "Countdown triggered!", 200


@cron_bp.route('/cron/quiz_announcement_0508', methods=['GET', 'POST'])
def trigger_quiz_announcement():
    threading.Thread(target=run_quiz_unlock_announcement).start()
    return "Quiz announcement triggered! Check Telegram.", 200


@cron_bp.route('/cron/refresh_snapshot_0508', methods=['GET', 'POST'])
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


@cron_bp.route('/word_of_the_day/0508', methods=['GET', 'POST'])
def trigger_word_of_the_day():
    threading.Thread(target=run_word_of_the_day).start()
    return "Word of the Day triggered!", 200


@cron_bp.route('/cron/ingest_miniapp_0508', methods=['GET', 'POST'])
def trigger_miniapp_ingestion():
    threading.Thread(target=run_mini_app_ingestion).start()
    return "Mini App Ingestion triggered! Check your Telegram DMs.", 200
