"""
DevTracker API — Flask Backend
Receives session data from the Chrome extension and stores it.

Install:
    pip install flask flask-cors

Run:
    python app.py

Deploy to any platform (Railway, Render, VPS, etc.)
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import json
import os
from datetime import datetime, timedelta
from collections import defaultdict

app = Flask(__name__)
CORS(app, origins=["chrome-extension://*", "https://vscode.dev", "https://*.github.dev"])

# ── Config ────────────────────────────────────────────────────────────────────

API_KEY = os.environ.get("DEVTRACKER_API_KEY", "")  # Set this in env for auth
DATA_FILE = os.environ.get("DATA_FILE", "sessions.json")  # Simple JSON file store
# For prod, swap this with a real DB (PostgreSQL, MongoDB, etc.)


# ── Storage (simple JSON file — swap with DB in prod) ─────────────────────────

def load_sessions():
    if not os.path.exists(DATA_FILE):
        return []
    with open(DATA_FILE, "r") as f:
        return json.load(f)


def save_sessions(sessions):
    with open(DATA_FILE, "w") as f:
        json.dump(sessions, f, indent=2)


# ── Middleware ────────────────────────────────────────────────────────────────

def check_auth():
    """Optional API key auth. Skip if no key configured."""
    if not API_KEY:
        return True
    auth = request.headers.get("Authorization", "")
    return auth == f"Bearer {API_KEY}" or auth == API_KEY


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "DevTracker API", "version": "1.0.0"})


@app.route("/devtracker/sessions", methods=["POST"])
def ingest_sessions():
    """Receive session data from the Chrome extension."""
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    if not data:
        return jsonify({"error": "No JSON body"}), 400

    incoming = data.get("sessions", [])
    source = data.get("source", "unknown")

    if not isinstance(incoming, list):
        return jsonify({"error": "sessions must be an array"}), 400

    # Filter out empty/test calls
    valid = [s for s in incoming if s.get("duration", 0) >= 5]

    if not valid:
        return jsonify({"ok": True, "saved": 0, "skipped": len(incoming)})

    # Load existing, deduplicate by session id
    sessions = load_sessions()
    existing_ids = {s["id"] for s in sessions}

    new_sessions = []
    for s in valid:
        if s.get("id") in existing_ids:
            continue
        # Sanitize and normalize
        session = {
            "id": s.get("id", f"imported_{datetime.now().timestamp()}"),
            "date": s.get("date", datetime.now().strftime("%Y-%m-%d")),
            "startTime": s.get("startTime", 0),
            "endTime": s.get("endTime", 0),
            "duration": int(s.get("duration", 0)),
            "file": s.get("file", "unknown")[:200],
            "language": s.get("language", "Unknown")[:50],
            "project": s.get("project", "unknown")[:200],
            "editor": s.get("editor", "unknown"),
            "source": source,
            "receivedAt": datetime.now().isoformat(),
        }
        new_sessions.append(session)
        existing_ids.add(session["id"])

    sessions.extend(new_sessions)

    # Prune older than 180 days
    cutoff_ts = (datetime.now() - timedelta(days=180)).timestamp() * 1000
    sessions = [s for s in sessions if s.get("startTime", 0) > cutoff_ts]

    save_sessions(sessions)

    return jsonify({
        "ok": True,
        "saved": len(new_sessions),
        "skipped": len(incoming) - len(new_sessions),
        "total": len(sessions),
    })


@app.route("/devtracker/sessions", methods=["GET"])
def get_sessions():
    """Return all sessions (optionally filtered)."""
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    sessions = load_sessions()

    # Optional query filters
    days = request.args.get("days", type=int)
    language = request.args.get("language")
    project = request.args.get("project")

    if days:
        cutoff = (datetime.now() - timedelta(days=days)).timestamp() * 1000
        sessions = [s for s in sessions if s.get("startTime", 0) > cutoff]

    if language:
        sessions = [s for s in sessions if s.get("language", "").lower() == language.lower()]

    if project:
        sessions = [s for s in sessions if project.lower() in (s.get("project") or "").lower()]

    return jsonify({"sessions": sessions, "count": len(sessions)})


@app.route("/devtracker/analytics", methods=["GET"])
def get_analytics():
    """Return pre-computed analytics."""
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    days = request.args.get("days", 7, type=int)
    sessions = load_sessions()

    cutoff = (datetime.now() - timedelta(days=days)).timestamp() * 1000
    recent = [s for s in sessions if s.get("startTime", 0) > cutoff]

    total_seconds = sum(s.get("duration", 0) for s in recent)

    by_language = defaultdict(int)
    by_project = defaultdict(int)
    by_day = defaultdict(int)
    by_editor = defaultdict(int)

    for s in recent:
        lang = s.get("language", "Unknown")
        if lang and lang != "Unknown":
            by_language[lang] += s.get("duration", 0)

        proj = s.get("project") or "unknown"
        by_project[proj] += s.get("duration", 0)

        day = s.get("date") or datetime.fromtimestamp(s.get("startTime", 0) / 1000).strftime("%Y-%m-%d")
        by_day[day] += s.get("duration", 0)

        editor = s.get("editor", "unknown")
        by_editor[editor] += s.get("duration", 0)

    return jsonify({
        "days": days,
        "totalSeconds": total_seconds,
        "sessionCount": len(recent),
        "byLanguage": dict(sorted(by_language.items(), key=lambda x: -x[1])),
        "byProject": dict(sorted(by_project.items(), key=lambda x: -x[1])),
        "byDay": dict(by_day),
        "byEditor": dict(by_editor),
    })


@app.route("/devtracker/sessions", methods=["DELETE"])
def clear_sessions():
    """Clear all sessions (dev use — protect this in prod)."""
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    save_sessions([])
    return jsonify({"ok": True, "cleared": True})


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    print(f"DevTracker API running on http://0.0.0.0:{port}")
    if API_KEY:
        print("Auth: API key required")
    else:
        print("Auth: NONE (set DEVTRACKER_API_KEY env var to secure)")
    app.run(host="0.0.0.0", port=port, debug=debug)
