# DevTracker

> Real-time coding activity tracker for web-based editors. Tracks your sessions on **vscode.dev** and **GitHub Codespaces** — no IDE plugin needed.

Built by **BOB.** 🟠

---

## What it tracks

- Active file & language (detected from DOM + tab title)
- Project / GitHub repo
- Session duration with idle detection
- Editor (VS Code Web vs Codespaces)

## Architecture

```
Chrome Extension
├── content.js      → injected into vscode.dev + github.dev
│                     detects file/language via DOM observation
├── background.js   → state machine, session storage, sync queue
├── popup.html/js   → quick stats popup
├── dashboard.html  → full analytics view (Chart.js)
└── settings.html   → API endpoint config

Flask API (optional)
└── app.py          → receives sessions, stores JSON, serves analytics
```

---

## Setup

### 1. Load the Chrome Extension

1. Go to `chrome://extensions`
2. Enable **Developer mode** (top right)
3. Click **Load unpacked**
4. Select the `extension/` folder

### 2. Start the Flask API (optional — for sync)

```bash
cd dashboard
pip install flask flask-cors

# Optional: set API key
export DEVTRACKER_API_KEY=your_secret_key
export PORT=5000

python app.py
```

API will be available at `http://localhost:5000`

### 3. Configure the Extension

1. Click the DevTracker extension icon
2. Click **⚙ Settings**
3. Set your API endpoint: `http://yourserver.com/devtracker/sessions`
4. Set your API key (if configured)
5. Enable sync
6. Click **Test Connection** → should show `Connection OK (200)`

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Health check |
| `POST` | `/devtracker/sessions` | Ingest sessions from extension |
| `GET` | `/devtracker/sessions` | Get all sessions |
| `GET` | `/devtracker/analytics?days=7` | Pre-computed analytics |
| `DELETE` | `/devtracker/sessions` | Clear all data |

### Query params for GET /sessions
- `?days=7` — filter to last N days
- `?language=Python` — filter by language
- `?project=myrepo` — filter by project name

---

## Session Data Format

```json
{
  "id": "session_1712592000000",
  "date": "2026-04-08",
  "startTime": 1712592000000,
  "endTime": 1712595600000,
  "duration": 3600,
  "file": "app.py",
  "language": "Python",
  "project": "BobbyX208/simcoin-v3",
  "editor": "github_codespaces",
  "synced": true
}
```

---

## Deploy the API

### Railway / Render

```bash
# Add environment variables:
DEVTRACKER_API_KEY=your_key
PORT=5000

# Start command:
python app.py
```

### Docker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["python", "app.py"]
```

```bash
# requirements.txt
flask
flask-cors
```

---

## Swap JSON file for PostgreSQL (prod)

Replace `load_sessions()` / `save_sessions()` in `app.py` with:

```python
import psycopg2

def get_conn():
    return psycopg2.connect(os.environ["DATABASE_URL"])
```

Schema:
```sql
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    date DATE,
    start_time BIGINT,
    end_time BIGINT,
    duration INT,
    file TEXT,
    language TEXT,
    project TEXT,
    editor TEXT,
    received_at TIMESTAMP DEFAULT NOW()
);
```

---

## Roadmap

- [ ] PostgreSQL backend
- [ ] Weekly email digest (Simora City Gazette style)
- [ ] GitHub streak integration
- [ ] Team/multi-user support
- [ ] Wakapi-compatible API (drop-in replacement)

---

*DevTracker — by BOB. 🟠*
