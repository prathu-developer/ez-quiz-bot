import os
import json
import time
import hmac
import hashlib
import secrets
import threading
from datetime import datetime
from flask import Blueprint, request, jsonify
from flask_app import (
    get_db, release_db, get_verified_user, redis_client,
    TELEGRAM_TOKEN, background_approve_user, CHAT_ID, http_session
)

auth_bp = Blueprint('api_auth', __name__)


@auth_bp.route('/api/approve_captcha', methods=['POST'])
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


@auth_bp.route('/api/admin/approve_pending_joins', methods=['GET', 'POST'])
def admin_approve_pending_joins():
    """
    Sweeps all currently pending join requests in bot_settings and approves them.
    Instantly clears any backlog of students whose captcha had loading issues.
    """
    conn = None
    approved_count = 0
    failed_count = 0
    approved_users = []
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT key, value FROM bot_settings WHERE key LIKE 'join_req_%'")
        rows = c.fetchall()
        for key, val in rows:
            u_id = key.replace("join_req_", "").strip()
            try:
                user_int = int(u_id)
                res = http_session.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/approveChatJoinRequest",
                    json={"chat_id": CHAT_ID, "user_id": user_int},
                    timeout=5
                )
                if res.status_code in [200, 400]:
                    approved_count += 1
                    approved_users.append(user_int)
                    c.execute("DELETE FROM bot_settings WHERE key = %s", (key,))
                    # Also register in users table
                    c.execute("""
                        INSERT INTO users (user_id, first_name, joined_at)
                        VALUES (%s, 'New Student', NOW())
                        ON CONFLICT (user_id) DO NOTHING
                    """, (user_int,))
                else:
                    failed_count += 1
            except Exception:
                failed_count += 1
        conn.commit()
        c.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            release_db(conn)

    return jsonify({
        "status": "success",
        "approved_count": approved_count,
        "failed_count": failed_count,
        "approved_users": approved_users
    }), 200


@auth_bp.route("/api/system-status", methods=["GET"])
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


@auth_bp.route('/api/auth/telegram-widget', methods=['POST'])
def auth_telegram_widget():
    """
    Validates the official Telegram Web Login Widget data from ezeditorials.pages.dev
    """
    data = request.get_json() or {}
    received_hash = data.get('hash')
    if not received_hash:
        return jsonify({"error": "Missing signature"}), 400

    # Build verification string according to Telegram specs
    check_dict = {k: v for k, v in data.items() if k != 'hash'}
    data_check_string = "\n".join(f"{k}={check_dict[k]}" for k in sorted(check_dict.keys()))

    # Secret key for widget is SHA256 of bot token (not HMAC like TMA)
    secret_key = hashlib.sha256(TELEGRAM_TOKEN.encode()).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        return jsonify({"error": "Invalid Telegram signature"}), 403

    # Check that the request was made within the last 24 hours
    auth_date = int(data.get('auth_date', 0))
    if time.time() - auth_date > 86400:
        return jsonify({"error": "Session expired"}), 401

    user_id = int(data['id'])
    first_name = data.get('first_name', 'Student')

    # Ensure student profile exists in PostgreSQL
    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("""
            INSERT INTO users (user_id, first_name, last_updated)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET 
                first_name = EXCLUDED.first_name,
                last_updated = EXCLUDED.last_updated
        """, (user_id, first_name, time.time()))
        conn.commit()
    except Exception as e:
        print(f"Error updating user on login: {e}")
    finally:
        if conn: release_db(conn)

    # Generate 30-day session token in Upstash Redis
    session_token = secrets.token_hex(32)
    user_payload = {
        "id": user_id,
        "first_name": first_name,
        "username": data.get("username", ""),
        "photo_url": data.get("photo_url", "")
    }

    if redis_client:
        redis_client.set(f"session:{session_token}", json.dumps(user_payload), ex=30 * 86400)

    return jsonify({
        "token": session_token,
        "user": user_payload
    }), 200


@auth_bp.route('/api/auth/request-code', methods=['POST'])
def request_login_code():
    """
    Generates a 5-minute deep-link token for the Android app and mobile web.
    """
    auth_code = secrets.token_hex(6)  # e.g., 'a3f81e7b9c12'
    
    if redis_client:
        redis_client.set(f"auth_code:{auth_code}", "pending", ex=300)

    return jsonify({
        "code": auth_code,
        "bot_username": "Ez_vocab_bot"  # Your bot username
    }), 200


@auth_bp.route('/api/user/delete-account', methods=['POST'])
def delete_account():
    user = get_verified_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    user_id = user.get('id') or user.get('user_id')
    if not user_id:
        return jsonify({"error": "Invalid user identification"}), 400

    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM quiz_responses WHERE attempt_id IN (SELECT id FROM quiz_attempts WHERE user_id = %s)", (user_id,))
        c.execute("DELETE FROM quiz_attempts WHERE user_id = %s", (user_id,))
        c.execute("DELETE FROM user_answers WHERE user_id = %s", (user_id,))
        c.execute("DELETE FROM read_receipts WHERE user_id = %s", (user_id,))
        c.execute("DELETE FROM daily_history WHERE user_id = %s", (user_id,))
        c.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
        conn.commit()
        c.close()

        if redis_client:
            # Purge all Redis sessions for this user
            try:
                for key in redis_client.scan_iter("session:*"):
                    val = redis_client.get(key)
                    if val and str(user_id) in str(val):
                        redis_client.delete(key)
            except Exception as e:
                print(f"⚠️ Redis purge error on delete account: {e}")

        return jsonify({"success": True}), 200
    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            release_db(conn)


@auth_bp.route('/api/auth/verify-code', methods=['POST'])
def verify_login_code():
    """
    Android App / Mobile Web polls this endpoint while student taps 'Start' in Telegram.
    """
    data = request.get_json() or {}
    code = data.get('code', '')

    if not code or not redis_client:
        return jsonify({"status": "pending"}), 200

    stored_data = redis_client.get(f"auth_code:{code}")
    if not stored_data:
        return jsonify({"error": "Code expired or invalid"}), 400

    stored_str = stored_data.decode('utf-8') if isinstance(stored_data, bytes) else str(stored_data)

    if stored_str == "pending":
        return jsonify({"status": "pending"}), 200

    # If code was verified by the bot, stored_data contains student info
    user_payload = json.loads(stored_str)

    # Mint a long-lived 30-day session token
    session_token = secrets.token_hex(32)
    redis_client.set(f"session:{session_token}", json.dumps(user_payload), ex=30 * 86400)
    
    # Delete one-time code to prevent reuse
    redis_client.delete(f"auth_code:{code}")

    return jsonify({
        "status": "authenticated",
        "token": session_token,
        "user": user_payload
    }), 200
