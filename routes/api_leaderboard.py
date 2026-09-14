import json
from flask import Blueprint, request, jsonify, Response
from flask_app import (
    get_db, release_db, get_verified_user,
    CACHE_LOCK, RAM_CACHE, bake_miniapp_cache, get_telegram_avatar_url
)

leaderboard_bp = Blueprint('api_leaderboard', __name__)


@leaderboard_bp.route('/api/leaderboard', methods=['GET'])
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


@leaderboard_bp.route('/api/elo', methods=['GET'])
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


@leaderboard_bp.route('/api/weekly-results', methods=['GET'])
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
