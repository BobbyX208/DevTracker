import os
import hashlib
import secrets
from datetime import datetime, timedelta
from collections import defaultdict
from urllib.parse import urlencode

from flask import Flask, request, jsonify, redirect, session, url_for
from flask_cors import CORS
from dotenv import load_dotenv
import psycopg
from psycopg.rows import dict_row
import requests

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024

ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "chrome-extension://*").split(",")
CORS(app, origins=ALLOWED_ORIGINS)

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is required")

GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID")
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET")
GITHUB_REDIRECT_URI = os.environ.get("GITHUB_REDIRECT_URI", "http://localhost:5000/auth/github/callback")

LEGACY_API_KEY = os.environ.get("API_KEY", "")


# ─── Database Helpers ───────────────────────────────────────────────────────

def get_db_connection():
    """Get a database connection."""
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db():
    """Initialize database tables."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            migration_path = os.path.join(os.path.dirname(__file__), 'migrations', '001_init.sql')
            if os.path.exists(migration_path):
                with open(migration_path, 'r') as f:
                    cur.execute(f.read())
            conn.commit()


# ─── Auth Helpers ───────────────────────────────────────────────────────────

def get_user_from_api_key(api_key):
    """Get user from API key (supports both legacy and user-specific keys)."""
    if LEGACY_API_KEY and api_key == LEGACY_API_KEY:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO users (github_username, display_name) 
                    VALUES ('legacy', 'Legacy User')
                    ON CONFLICT (github_username) DO UPDATE SET github_username = EXCLUDED.github_username
                    RETURNING id
                """)
                user_id = cur.fetchone()['id']
                conn.commit()
                return user_id
    
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()
            cur.execute("""
                SELECT user_id FROM api_keys 
                WHERE key_hash = %s AND revoked_at IS NULL
            """, (api_key_hash,))
            result = cur.fetchone()
            
            if result:
                cur.execute("""
                    UPDATE api_keys SET last_used_at = CURRENT_TIMESTAMP 
                    WHERE key_hash = %s
                """, (api_key_hash,))
                conn.commit()
                return result['user_id']
    
    return None


def require_auth(f):
    """Decorator to require API key authentication."""
    def decorated_function(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        api_key = None
        
        if auth_header.startswith("Bearer "):
            api_key = auth_header[7:]
        else:
            api_key = auth_header
        
        if not api_key:
            return jsonify({"error": "API key required"}), 401
        
        user_id = get_user_from_api_key(api_key)
        if not user_id:
            return jsonify({"error": "Invalid API key"}), 401
        
        request.user_id = user_id
        return f(*args, **kwargs)
    
    decorated_function.__name__ = f.__name__
    return decorated_function


# ─── Routes ─────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "DevTracker API",
        "version": "2.0.0",
        "database": "PostgreSQL"
    })


@app.route("/devtracker/sessions", methods=["POST"])
@require_auth
def ingest_sessions():
    """Receive session data from the Chrome extension."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "No JSON body"}), 400

    incoming = data.get("sessions", [])
    source = data.get("source", "unknown")
    user_id = request.user_id

    if not isinstance(incoming, list):
        return jsonify({"error": "sessions must be an array"}), 400

    valid = [s for s in incoming if s.get("duration", 0) >= 5]

    if not valid:
        return jsonify({"ok": True, "saved": 0, "skipped": len(incoming)})

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            saved = 0
            for s in valid:
                session_id = s.get("id", f"imported_{datetime.utcnow().timestamp()}")
                
                cur.execute("SELECT id FROM sessions WHERE id = %s", (session_id,))
                if cur.fetchone():
                    continue
                
                cur.execute("""
                    INSERT INTO sessions (
                        id, user_id, date, start_time, end_time, duration,
                        file, language, project, editor, source, received_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    session_id,
                    user_id,
                    s.get("date", datetime.utcnow().strftime("%Y-%m-%d")),
                    s.get("startTime", 0),
                    s.get("endTime", 0),
                    int(s.get("duration", 0)),
                    s.get("file", "unknown")[:500],
                    s.get("language", "Unknown")[:100],
                    s.get("project", "unknown")[:500],
                    s.get("editor", "unknown"),
                    source,
                    datetime.utcnow()
                ))
                saved += 1
            
            conn.commit()
    
    update_daily_summary(user_id)
    
    return jsonify({
        "ok": True,
        "saved": saved,
        "skipped": len(incoming) - saved
    })


@app.route("/devtracker/sessions", methods=["GET"])
@require_auth
def get_sessions():
    """Return all sessions for the authenticated user."""
    user_id = request.user_id
    days = request.args.get("days", type=int)
    language = request.args.get("language")
    project = request.args.get("project")
    limit = request.args.get("limit", 100, type=int)
    offset = request.args.get("offset", 0, type=int)
    
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            query = "SELECT * FROM sessions WHERE user_id = %s"
            params = [user_id]
            
            if days:
                cutoff = (datetime.utcnow() - timedelta(days=days)).timestamp() * 1000
                query += " AND start_time >= %s"
                params.append(cutoff)
            
            if language:
                query += " AND LOWER(language) = LOWER(%s)"
                params.append(language)
            
            if project:
                query += " AND LOWER(project) LIKE LOWER(%s)"
                params.append(f"%{project}%")
            
            query += " ORDER BY start_time DESC LIMIT %s OFFSET %s"
            params.extend([limit, offset])
            
            cur.execute(query, params)
            sessions = cur.fetchall()
            
            count_query = query.replace("SELECT *", "SELECT COUNT(*) as count").split(" LIMIT")[0]
            cur.execute(count_query, params[:-2])
            total = cur.fetchone()['count']
    
    return jsonify({
        "sessions": sessions,
        "count": len(sessions),
        "total": total,
        "limit": limit,
        "offset": offset
    })


@app.route("/devtracker/analytics", methods=["GET"])
@require_auth
def get_analytics():
    """Return pre-computed analytics."""
    user_id = request.user_id
    days = request.args.get("days", 7, type=int)
    
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cutoff_date = (datetime.utcnow().date() - timedelta(days=days-1))
            
            cur.execute("""
                SELECT 
                    SUM(total_seconds) as total_seconds,
                    SUM(session_count) as session_count,
                    jsonb_object_agg(date, total_seconds) as by_day
                FROM daily_summaries 
                WHERE user_id = %s AND date >= %s
            """, (user_id, cutoff_date))
            
            summary = cur.fetchone()
            
            if summary and summary['total_seconds']:
                cutoff_ts = (datetime.utcnow() - timedelta(days=days)).timestamp() * 1000
                
                cur.execute("""
                    SELECT 
                        language,
                        project,
                        editor,
                        SUM(duration) as total_duration
                    FROM sessions
                    WHERE user_id = %s AND start_time >= %s
                    GROUP BY language, project, editor
                """, (user_id, cutoff_ts))
                
                rows = cur.fetchall()
                
                by_language = defaultdict(int)
                by_project = defaultdict(int)
                by_editor = defaultdict(int)
                
                for row in rows:
                    if row['language'] and row['language'] != 'Unknown':
                        by_language[row['language']] += row['total_duration']
                    if row['project']:
                        by_project[row['project']] += row['total_duration']
                    if row['editor']:
                        by_editor[row['editor']] += row['total_duration']
                
                return jsonify({
                    "days": days,
                    "totalSeconds": summary['total_seconds'],
                    "sessionCount": summary['session_count'],
                    "byLanguage": dict(sorted(by_language.items(), key=lambda x: -x[1])),
                    "byProject": dict(sorted(by_project.items(), key=lambda x: -x[1])),
                    "byDay": summary['by_day'] if summary['by_day'] else {},
                    "byEditor": dict(by_editor),
                })
    
    return calculate_analytics_from_sessions(user_id, days)


def calculate_analytics_from_sessions(user_id, days):
    """Calculate analytics directly from sessions (slower fallback)."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).timestamp() * 1000
    
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM sessions 
                WHERE user_id = %s AND start_time >= %s
            """, (user_id, cutoff))
            sessions = cur.fetchall()
    
    total_seconds = sum(s['duration'] for s in sessions)
    
    by_language = defaultdict(int)
    by_project = defaultdict(int)
    by_day = defaultdict(int)
    by_editor = defaultdict(int)
    
    for s in sessions:
        if s['language'] and s['language'] != 'Unknown':
            by_language[s['language']] += s['duration']
        if s['project']:
            by_project[s['project']] += s['duration']
        if s['editor']:
            by_editor[s['editor']] += s['duration']
        day = s['date'].isoformat() if hasattr(s['date'], 'isoformat') else str(s['date'])
        by_day[day] += s['duration']
    
    return jsonify({
        "days": days,
        "totalSeconds": total_seconds,
        "sessionCount": len(sessions),
        "byLanguage": dict(sorted(by_language.items(), key=lambda x: -x[1])),
        "byProject": dict(sorted(by_project.items(), key=lambda x: -x[1])),
        "byDay": dict(by_day),
        "byEditor": dict(by_editor),
    })


def update_daily_summary(user_id):
    """Update daily summary for a user."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO daily_summaries (user_id, date, total_seconds, session_count, languages, projects, editors)
                SELECT 
                    user_id,
                    date,
                    SUM(duration) as total_seconds,
                    COUNT(*) as session_count,
                    jsonb_object_agg(language, lang_duration) as languages,
                    jsonb_object_agg(project, proj_duration) as projects,
                    jsonb_object_agg(editor, editor_duration) as editors
                FROM (
                    SELECT 
                        user_id,
                        date,
                        duration,
                        language,
                        SUM(duration) OVER (PARTITION BY user_id, date, language) as lang_duration,
                        project,
                        SUM(duration) OVER (PARTITION BY user_id, date, project) as proj_duration,
                        editor,
                        SUM(duration) OVER (PARTITION BY user_id, date, editor) as editor_duration
                    FROM sessions
                    WHERE user_id = %s
                ) t
                GROUP BY user_id, date
                ON CONFLICT (user_id, date) DO UPDATE SET
                    total_seconds = EXCLUDED.total_seconds,
                    session_count = EXCLUDED.session_count,
                    languages = EXCLUDED.languages,
                    projects = EXCLUDED.projects,
                    editors = EXCLUDED.editors
            """, (user_id,))
            conn.commit()


# ─── GitHub Integration ──────────────────────────────────────────────────────

@app.route("/auth/github", methods=["GET"])
def github_login():
    """Initiate GitHub OAuth flow."""
    params = {
        "client_id": GITHUB_CLIENT_ID,
        "redirect_uri": GITHUB_REDIRECT_URI,
        "scope": "read:user user:email",
        "state": secrets.token_hex(16)
    }
    session['oauth_state'] = params['state']
    auth_url = f"https://github.com/login/oauth/authorize?{urlencode(params)}"
    return redirect(auth_url)


@app.route("/auth/github/callback", methods=["GET"])
def github_callback():
    """Handle GitHub OAuth callback."""
    code = request.args.get("code")
    state = request.args.get("state")
    
    if state != session.get('oauth_state'):
        return jsonify({"error": "Invalid state"}), 400
    
    token_response = requests.post(
        "https://github.com/login/oauth/access_token",
        data={
            "client_id": GITHUB_CLIENT_ID,
            "client_secret": GITHUB_CLIENT_SECRET,
            "code": code,
            "redirect_uri": GITHUB_REDIRECT_URI
        },
        headers={"Accept": "application/json"}
    )
    
    if token_response.status_code != 200:
        return jsonify({"error": "Failed to get access token"}), 400
    
    access_token = token_response.json().get("access_token")
    
    user_response = requests.get(
        "https://api.github.com/user",
        headers={"Authorization": f"Bearer {access_token}"}
    )
    
    if user_response.status_code != 200:
        return jsonify({"error": "Failed to get user info"}), 400
    
    user_data = user_response.json()
    
    email_response = requests.get(
        "https://api.github.com/user/emails",
        headers={"Authorization": f"Bearer {access_token}"}
    )
    
    primary_email = None
    if email_response.status_code == 200:
        emails = email_response.json()
        primary = next((e for e in emails if e.get("primary")), None)
        if primary:
            primary_email = primary["email"]
    
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (
                    github_id, github_username, github_email, 
                    github_access_token, github_avatar_url, display_name
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (github_id) DO UPDATE SET
                    github_username = EXCLUDED.github_username,
                    github_email = EXCLUDED.github_email,
                    github_access_token = EXCLUDED.github_access_token,
                    github_avatar_url = EXCLUDED.github_avatar_url,
                    last_sync_at = CURRENT_TIMESTAMP
                RETURNING id
            """, (
                user_data["id"],
                user_data["login"],
                primary_email,
                access_token,
                user_data.get("avatar_url"),
                user_data.get("name") or user_data["login"]
            ))
            
            user_id = cur.fetchone()['id']
            
            api_key = secrets.token_urlsafe(32)
            api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()
            
            cur.execute("""
                INSERT INTO api_keys (user_id, key_hash, name)
                VALUES (%s, %s, %s)
            """, (user_id, api_key_hash, "Default API Key"))
            
            conn.commit()
    
    return jsonify({
        "success": True,
        "api_key": api_key,
        "message": "Add this API key to your DevTracker extension settings",
        "github_username": user_data["login"]
    })


@app.route("/auth/me", methods=["GET"])
@require_auth
def get_current_user():
    """Get current authenticated user info."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, github_username, display_name, github_avatar_url, created_at
                FROM users WHERE id = %s
            """, (request.user_id,))
            user = cur.fetchone()
    
    if not user:
        return jsonify({"error": "User not found"}), 404
    
    return jsonify(user)


@app.route("/profile/<username>", methods=["GET"])
def get_public_profile(username):
    """Get public profile data for GitHub-style display."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, github_username, display_name, github_avatar_url, created_at
                FROM users WHERE github_username = %s
            """, (username,))
            user = cur.fetchone()
            
            if not user:
                return jsonify({"error": "User not found"}), 404
            
            cur.execute("""
                SELECT 
                    COALESCE(SUM(total_seconds), 0) as total_seconds,
                    COALESCE(SUM(session_count), 0) as total_sessions
                FROM daily_summaries 
                WHERE user_id = %s AND date >= CURRENT_DATE - INTERVAL '7 days'
            """, (user['id'],))
            week_stats = cur.fetchone()
            
            cur.execute("""
                SELECT 
                    language,
                    SUM(duration) as total_duration
                FROM sessions
                WHERE user_id = %s AND start_time >= EXTRACT(EPOCH FROM (NOW() - INTERVAL '30 days')) * 1000
                GROUP BY language
                ORDER BY total_duration DESC
                LIMIT 5
            """, (user['id'],))
            top_languages = cur.fetchall()
    
    return jsonify({
        "username": user['github_username'],
        "display_name": user['display_name'],
        "avatar_url": user['github_avatar_url'],
        "member_since": user['created_at'],
        "stats": {
            "week_seconds": week_stats['total_seconds'],
            "week_sessions": week_stats['total_sessions'],
            "top_languages": top_languages
        }
    })


# ─── API Key Management ──────────────────────────────────────────────────────

@app.route("/api-keys", methods=["GET"])
@require_auth
def list_api_keys():
    """List API keys for the authenticated user."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, last_used_at, created_at
                FROM api_keys 
                WHERE user_id = %s AND revoked_at IS NULL
                ORDER BY created_at DESC
            """, (request.user_id,))
            keys = cur.fetchall()
    
    return jsonify({"api_keys": keys})


@app.route("/api-keys", methods=["POST"])
@require_auth
def create_api_key():
    """Create a new API key."""
    data = request.get_json() or {}
    name = data.get("name", "API Key")
    
    api_key = secrets.token_urlsafe(32)
    api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO api_keys (user_id, key_hash, name)
                VALUES (%s, %s, %s)
                RETURNING id
            """, (request.user_id, api_key_hash, name))
            conn.commit()
    
    return jsonify({
        "api_key": api_key,
        "name": name,
        "message": "Save this key - it won't be shown again!"
    })


@app.route("/api-keys/<int:key_id>", methods=["DELETE"])
@require_auth
def revoke_api_key(key_id):
    """Revoke an API key."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE api_keys 
                SET revoked_at = CURRENT_TIMESTAMP
                WHERE id = %s AND user_id = %s
            """, (key_id, request.user_id))
            conn.commit()
    
    return jsonify({"ok": True})


# ─── Badge Generation (WakaTime-style) ────────────────────────────────────────

@app.route("/badge/<username>", methods=["GET"])
def get_badge(username):
    """Generate a WakaTime-style badge for GitHub README."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COALESCE(SUM(total_seconds), 0) as total_seconds
                FROM daily_summaries ds
                JOIN users u ON u.id = ds.user_id
                WHERE u.github_username = %s AND ds.date >= CURRENT_DATE - INTERVAL '7 days'
            """, (username,))
            result = cur.fetchone()
    
    hours = (result['total_seconds'] or 0) // 3600
    
    color = "orange" if hours > 10 else "blue" if hours > 5 else "lightgrey"
    badge_text = f"{hours}h this week"
    
    svg = f"""
    <svg xmlns="http://www.w3.org/2000/svg" width="150" height="20">
        <linearGradient id="b" x2="0" y2="100%">
            <stop offset="0" stop-color="#bbb" stop-opacity=".1"/>
            <stop offset="1" stop-opacity=".1"/>
        </linearGradient>
        <mask id="a">
            <rect width="150" height="20" rx="3" fill="#fff"/>
        </mask>
        <g mask="url(#a)">
            <path fill="#555" d="M0 0h80v20H0z"/>
            <path fill="{color}" d="M80 0h70v20H80z"/>
            <path fill="url(#b)" d="M0 0h150v20H0z"/>
        </g>
        <g fill="#fff" text-anchor="middle" font-family="DejaVu Sans,Verdana,Geneva,sans-serif" font-size="11">
            <text x="40" y="15" fill="#010101" fill-opacity=".3">DevTracker</text>
            <text x="40" y="14">DevTracker</text>
            <text x="115" y="15" fill="#010101" fill-opacity=".3">{badge_text}</text>
            <text x="115" y="14">{badge_text}</text>
        </g>
    </svg>
    """
    
    return svg, 200, {'Content-Type': 'image/svg+xml'}


# ─── WakaTime Compatibility Layer ─────────────────────────────────────────────

@app.route("/api/v1/users/current/stats", methods=["GET"])
@require_auth
def wakatime_stats():
    """WakaTime-compatible stats endpoint."""
    user_id = request.user_id
    range_param = request.args.get("range", "last_7_days")
    
    range_map = {
        "last_7_days": 7,
        "last_30_days": 30,
        "last_6_months": 180,
        "last_year": 365,
        "all_time": 3650
    }
    days = range_map.get(range_param, 7)
    
    cutoff = (datetime.utcnow() - timedelta(days=days)).timestamp() * 1000
    
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 
                    language,
                    SUM(duration) as total_seconds,
                    COUNT(*) as session_count
                FROM sessions
                WHERE user_id = %s AND start_time >= %s
                GROUP BY language
                ORDER BY total_seconds DESC
            """, (user_id, cutoff))
            languages = cur.fetchall()
            
            cur.execute("""
                SELECT 
                    editor,
                    SUM(duration) as total_seconds
                FROM sessions
                WHERE user_id = %s AND start_time >= %s
                GROUP BY editor
                ORDER BY total_seconds DESC
            """, (user_id, cutoff))
            editors = cur.fetchall()
            
            cur.execute("""
                SELECT 
                    SUM(duration) as total_seconds,
                    COUNT(*) as session_count
                FROM sessions
                WHERE user_id = %s AND start_time >= %s
            """, (user_id, cutoff))
            totals = cur.fetchone()
    
    total_seconds = totals['total_seconds'] or 0
    
    return jsonify({
        "data": {
            "total_seconds": total_seconds,
            "total_seconds_including_other_language": total_seconds,
            "human_readable_total": f"{total_seconds // 3600}h {(total_seconds % 3600) // 60}m",
            "daily_average": total_seconds // days if days > 0 else 0,
            "languages": [
                {
                    "name": lang['language'] or "Unknown",
                    "total_seconds": lang['total_seconds'],
                    "percent": round(lang['total_seconds'] / total_seconds * 100, 2) if total_seconds > 0 else 0,
                    "text": f"{lang['total_seconds'] // 3600}h {(lang['total_seconds'] % 3600) // 60}m"
                }
                for lang in languages[:10]
            ],
            "editors": [
                {
                    "name": ed['editor'] or "Unknown",
                    "total_seconds": ed['total_seconds'],
                    "percent": round(ed['total_seconds'] / total_seconds * 100, 2) if total_seconds > 0 else 0
                }
                for ed in editors
            ],
            "range": range_param,
            "is_up_to_date": True
        }
    })


@app.route("/api/v1/users/current", methods=["GET"])
@require_auth
def wakatime_user():
    """WakaTime-compatible user endpoint."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT display_name, github_username, created_at
                FROM users WHERE id = %s
            """, (request.user_id,))
            user = cur.fetchone()
    
    return jsonify({
        "data": {
            "username": user['github_username'] or "devtracker_user",
            "display_name": user['display_name'] or "DevTracker User",
            "created_at": user['created_at'].isoformat() if user['created_at'] else datetime.utcnow().isoformat()
        }
    })


# ─── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    
    print(f"🚀 DevTracker API running on http://0.0.0.0:{port}")
    print(f"📊 Using PostgreSQL database")
    if GITHUB_CLIENT_ID:
        print(f"🔐 GitHub OAuth enabled")
    
    app.run(host="0.0.0.0", port=port, debug=debug)