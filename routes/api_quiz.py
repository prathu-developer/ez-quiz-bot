import json
import time
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify
from flask_app import (
    get_db, release_db, get_verified_user, redis_client,
    ADD_DB_KEY, acquire_submission_lock, maintenance_block
)

quiz_bp = Blueprint('api_quiz', __name__)


@quiz_bp.route('/add_poll', methods=['POST'])
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


@quiz_bp.route('/api/quiz/today', methods=['GET'])
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


@quiz_bp.route('/api/quiz/start', methods=['POST'])
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


@quiz_bp.route('/api/quiz/submit', methods=['POST'])
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
            time_spent = int(r.get('time_spent', 0)) # 🟢 Extract question time
            
            if q_id not in correct_map:
                continue
                
            is_correct = False
            if s_idx is not None:
                if s_idx == correct_map[q_id]:
                    is_correct = True
                    total_score += 1.0
                else:
                    total_score -= 0.25
                    
            # 🟢 Add time_spent as the 5th parameter
            responses_to_insert.append((attempt_id, q_id, s_idx, is_correct, time_spent))
            
            if s_idx is not None:
                poll_inserts.append((str(q_id), correct_map[q_id], current_day_str))
                user_answer_inserts.append((user_id, str(q_id), int(is_correct), current_day_str, s_idx))
            
        # 3. Batch Inserts
        if responses_to_insert:
            c.executemany("""
                INSERT INTO quiz_responses (attempt_id, question_id, selected_index, is_correct, time_spent)
                VALUES (%s, %s, %s, %s, %s)
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


@quiz_bp.route('/api/quiz/result/<int:attempt_id>', methods=['GET'])
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
        
        # 2. Get Community Stats & True Question Averages
        c.execute("""
            SELECT question_id, 
                   COUNT(selected_index) as total_attempts, 
                   SUM(CASE WHEN is_correct THEN 1 ELSE 0 END) as total_correct,
                   ROUND(AVG(time_spent) FILTER (WHERE time_spent > 0)) as avg_time
            FROM quiz_responses
            WHERE question_id IN (SELECT id FROM quiz_questions WHERE quiz_set_id = %s)
            GROUP BY question_id
        """, (quiz_set_id,))
        q_stats = {
            row[0]: {
                "attempts": row[1], 
                "correct": row[2], 
                "avg_time": int(row[3]) if row[3] is not None else 15
            } 
            for row in c.fetchall()
        }

        # 3. Fetch Sectional Summary, Explanations, and User's Recorded Time
        c.execute("""
            SELECT q.id, q.question_text, q.options, q.correct_index, q.explanation, 
                   r.selected_index, r.is_correct, COALESCE(r.time_spent, 0)
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
            q_id, text, options, c_idx, exp, s_idx, is_corr, u_time_spent = row
            
            if s_idx is None:
                unattempted_count += 1
            elif is_corr:
                correct_count += 1
            else:
                wrong_count += 1
                
            g_att = q_stats.get(q_id, {}).get("attempts", 0)
            g_cor = q_stats.get(q_id, {}).get("correct", 0)
            g_acc = round((g_cor / g_att) * 100) if g_att > 0 else 0
            g_avg = q_stats.get(q_id, {}).get("avg_time", 15)
                
            question_details.append({
                "question_id": q_id,
                "text": text,
                "options": options,
                "correct_index": c_idx,
                "explanation": exp,
                "user_selected_index": s_idx,
                "is_correct": is_corr,
                "global_accuracy": g_acc,
                "global_avg_time": g_avg,          # 🟢 Specific question average
                "user_time_spent": u_time_spent     # 🟢 Student's actual time spent
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
