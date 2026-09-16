import os
import json
import datetime
from flask import Blueprint, request, jsonify, Response
from flask_app import get_db, release_db, get_verified_user, redis_client

magazine_bp = Blueprint('api_magazine', __name__)

def get_current_week_num() -> int:
    """
    Resolves the current season/week number from the database `bot_settings` table.
    Crucial: Must follow bot_settings.current_week (lockstep with Weekly Cup resets),
    NOT standard datetime.isocalendar().
    """
    # 1. Check RAM_CACHE first if populated (0ms latency)
    try:
        from flask_app import RAM_CACHE
        if RAM_CACHE.get("master_data") and RAM_CACHE["master_data"].get("current_week"):
            return int(RAM_CACHE["master_data"]["current_week"])
    except Exception:
        pass

    # 2. Query Supabase / Postgres bot_settings table
    conn = None
    try:
        conn = get_db()
        if conn:
            with conn.cursor() as c:
                c.execute("SELECT value FROM bot_settings WHERE key = 'current_week'")
                row = c.fetchone()
                if row and row[0]:
                    return int(row[0])
    except Exception as e:
        print(f"⚠️ Warning: Could not fetch current_week from bot_settings: {e}")
    finally:
        if conn:
            release_db(conn)

    # 3. Safe fallback if DB is temporarily unreachable
    return datetime.datetime.now().isocalendar()[1]


def get_current_week_key() -> str:
    """Generates the Redis key for the active week's magazine cache."""
    return f"magazine:week:{get_current_week_num()}"


# ==============================================================================
# 1. INGEST ENDPOINT (PUSH FROM MAGAZINE PIPELINE)
# ==============================================================================
@magazine_bp.route('/api/magazine/ingest', methods=['POST'])
def ingest_magazine_edition():
    """
    Secured ingest endpoint called by the GitHub Actions / magazine generator pipeline.
    Authenticates via X-Magazine-Secret header before touching Redis or DB.
    Merges the day's editorial articles into the current week's Redis store with a 9-day TTL.
    """
    ingest_secret = os.environ.get("MAGAZINE_INGEST_SECRET")
    request_secret = request.headers.get("X-Magazine-Secret")

    if not ingest_secret or request_secret != ingest_secret:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({"error": "Invalid payload: JSON body required"}), 400

    date_str = data.get("date")
    articles = data.get("articles")

    if not date_str or not isinstance(articles, list):
        return jsonify({"error": "Invalid payload: 'date' string (YYYY-MM-DD) and 'articles' array required"}), 400

    week_key = get_current_week_key()
    week_data = {}

    if redis_client:
        try:
            raw = redis_client.get(week_key)
            if raw:
                if isinstance(raw, bytes):
                    raw = raw.decode('utf-8')
                week_data = json.loads(raw)
        except Exception as e:
            print(f"⚠️ Redis read error in ingest_magazine: {e}")
            week_data = {}

        # Merge day's articles into week container
        week_data[date_str] = articles

        # Optional Telegram Message ID Sync (Sprint 3.5)
        telegram_msg_id = data.get("telegram_message_id")
        if telegram_msg_id:
            if "_meta" not in week_data or not isinstance(week_data["_meta"], dict):
                week_data["_meta"] = {}
            if date_str not in week_data["_meta"] or not isinstance(week_data["_meta"][date_str], dict):
                week_data["_meta"][date_str] = {}
            week_data["_meta"][date_str]["telegram_message_id"] = int(telegram_msg_id)

        try:
            # 9-day TTL (777,600 seconds) ensures the full week remains available through Sunday wrap-up
            redis_client.set(week_key, json.dumps(week_data), ex=9 * 86400)
        except Exception as e:
            print(f"⚠️ Redis write error in ingest_magazine: {e}")
            return jsonify({"error": f"Failed to persist magazine to Redis: {e}"}), 500
    else:
        print("⚠️ Warning: redis_client not configured. Magazine edition not stored in Redis.")

    return jsonify({
        "status": "ok",
        "date": date_str,
        "week_key": week_key,
        "articles_count": len(articles)
    }), 200


# ==============================================================================
# 2. WEEK ENDPOINT (EDGE-CACHEABLE CONTENT CONSUMED BY APP)
# ==============================================================================
@magazine_bp.route('/api/magazine/week', methods=['GET'])
def get_magazine_week():
    """
    Publicly edge-cacheable endpoint returning all days of the current week.
    Cache-Control: public, max-age=1800 (30 minutes) prevents redundant egress.
    Filters out internal `_meta` key so only date keys are returned to students.
    """
    week_num = get_current_week_num()
    week_key = f"magazine:week:{week_num}"
    week_data = {}

    if redis_client:
        try:
            raw = redis_client.get(week_key)
            if raw:
                if isinstance(raw, bytes):
                    raw = raw.decode('utf-8')
                week_data = json.loads(raw)
        except Exception as e:
            print(f"⚠️ Redis read error in get_magazine_week: {e}")

    # Exclude internal metadata (_meta) from public response
    clean_week_data = {k: v for k, v in week_data.items() if k != "_meta"}

    response_payload = {
        "week": clean_week_data,
        "current_week": week_num
    }

    res = Response(json.dumps(response_payload), mimetype='application/json', status=200)
    res.headers["Cache-Control"] = "public, max-age=1800"
    return res


# ==============================================================================
# 3. DAY ENDPOINT (EDGE-CACHEABLE CONTENT FOR SPECIFIC DATE)
# ==============================================================================
@magazine_bp.route('/api/magazine/day/<date_str>', methods=['GET'])
def get_magazine_day(date_str):
    """
    Returns the articles for a specific day from the active week.
    Cache-Control: public, max-age=1800.
    """
    week_num = get_current_week_num()
    week_key = f"magazine:week:{week_num}"
    articles = []

    if redis_client:
        try:
            raw = redis_client.get(week_key)
            if raw:
                if isinstance(raw, bytes):
                    raw = raw.decode('utf-8')
                week_data = json.loads(raw)
                articles = week_data.get(date_str, [])
        except Exception as e:
            print(f"⚠️ Redis read error in get_magazine_day: {e}")

    response_payload = {
        "date": date_str,
        "articles": articles,
        "current_week": week_num
    }

    res = Response(json.dumps(response_payload), mimetype='application/json', status=200)
    res.headers["Cache-Control"] = "public, max-age=1800"
    return res


# ==============================================================================
# 4. USER READ STATUS ENDPOINT
# ==============================================================================
@magazine_bp.route('/api/magazine/read-status', methods=['GET'])
def get_magazine_read_status():
    """
    Returns the list of magazine dates the logged-in student has marked read this week.
    Allows the UI to display completion checkmarks on day pills.
    """
    verified_user = get_verified_user()
    if not verified_user:
        return jsonify({"read_dates": []}), 200

    user_id = int(verified_user.get('id'))
    conn = None
    read_dates = []

    try:
        conn = get_db()
        if conn:
            with conn.cursor() as c:
                c.execute("""
                    SELECT content_type FROM content_read_status
                    WHERE user_id = %s AND content_type LIKE 'magazine_%%'
                    ORDER BY content_date DESC LIMIT 30
                """, (user_id,))
                for row in c.fetchall():
                    c_type = row[0]
                    # Format: magazine_YYYY-MM-DD
                    if c_type.startswith("magazine_"):
                        read_dates.append(c_type.replace("magazine_", ""))
    except Exception as e:
        print(f"⚠️ Error fetching magazine read status: {e}")
    finally:
        if conn:
            release_db(conn)

    return jsonify({"read_dates": read_dates}), 200


# ==============================================================================
# 5. TELEGRAM READ RECEIPT SYNC ENDPOINT (SPRINT 3.5)
# ==============================================================================
@magazine_bp.route('/api/magazine/sync-read-receipt', methods=['POST'])
def sync_read_receipt():
    """
    Syncs an in-app read action with the Telegram group's '✅ Marked as Read • N' button.
    Reuses process_read_receipt() from flask_app.py for seamless cross-platform identity
    and double-tap protection.
    """
    verified_user = get_verified_user()
    if not verified_user:
        return jsonify({"error": "Unauthorized"}), 401

    user_id_raw = verified_user.get('id')
    if not user_id_raw:
        return jsonify({"skipped": "user has no telegram user_id"}), 200

    try:
        user_id = int(user_id_raw)
    except (ValueError, TypeError):
        return jsonify({"skipped": "invalid user_id format"}), 200

    data = request.get_json(silent=True) or {}
    date_str = data.get("date")
    if not date_str:
        return jsonify({"error": "Missing date"}), 400

    week_key = get_current_week_key()
    week_data = {}
    if redis_client:
        try:
            raw = redis_client.get(week_key)
            if raw:
                if isinstance(raw, bytes):
                    raw = raw.decode('utf-8')
                week_data = json.loads(raw)
        except Exception as e:
            print(f"⚠️ Redis read error in sync_read_receipt: {e}")

    message_id = week_data.get("_meta", {}).get(date_str, {}).get("telegram_message_id")
    if not message_id:
        return jsonify({"skipped": "no linked Telegram message for this date"}), 200

    first_name = verified_user.get('first_name', 'Student')

    try:
        from flask_app import process_read_receipt
        process_read_receipt(
            cb_id=None,
            user_id=user_id,
            first_name=first_name,
            message_id=int(message_id)
        )
        return jsonify({"success": True, "message_id": int(message_id)}), 200
    except Exception as e:
        print(f"⚠️ Error in sync_read_receipt calling process_read_receipt: {e}")
        return jsonify({"error": str(e)}), 500

