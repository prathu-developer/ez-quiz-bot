import json
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify
from flask_app import (
    get_db, release_db, get_verified_user
)

profile_bp = Blueprint('api_profile', __name__)


@profile_bp.route('/api/profile/me', methods=['GET'])
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


@profile_bp.route('/api/profile/update-target', methods=['POST'])
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


@profile_bp.route('/api/progress/me', methods=['GET'])
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


@profile_bp.route('/api/upcoming-exams', methods=['GET'])
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


@profile_bp.route('/api/digest/today', methods=['GET'])
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


@profile_bp.route('/api/digest/mark-read', methods=['POST'])
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


@profile_bp.route('/api/word-of-day', methods=['GET'])
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


@profile_bp.route('/api/foreign-expressions', methods=['GET'])
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
