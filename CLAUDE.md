# DayView — Cloud Build Plan

## What This Is
Work intelligence dashboard powered by Screenpipe. Captures screen activity + audio, classifies work by project/role, generates AI daily summaries.

## Current State
- Flask app running locally at localhost:5051
- Reads from local Screenpipe SQLite DB (~/.screenpipe/db.sqlite)
- Summarizes via Gemini 2.0 Flash
- Stores projects/activities in local SQLite (projects.db)
- Frontend: single-page app with 4 tabs (Daily Journal, Shipped Feed, Project Radar, Activity Map)
- No auth, no cloud, single-user only

## Goal
Deploy as a cloud app on Fly.io. Anyone can sign up, install a lightweight local agent, and get AI-generated daily summaries at a shareable URL.

## Architecture

### Cloud (Fly.io)
- Flask app with Postgres (replaces SQLite)
- User auth (email/password + API key for agent)
- Ingest API: receives pre-deduplicated Screenpipe frames from local agent
- AI summarizer runs server-side (Gemini 2.0 Flash)
- Dashboard serves from cloud DB, not local Screenpipe

### Local Agent
- Standalone Python script (~/.dayview/dayview-agent.py)
- Reads ~/.screenpipe/db.sqlite (read-only)
- Deduplicates OCR frames (0.85 similarity threshold → ~10x compression)
- POSTs compressed payload to cloud API every 30 minutes
- Tracks last-synced timestamp to avoid re-uploading
- Runs via launchd (macOS) or cron

### Install Flow
1. User visits dayview URL, signs up → gets API key
2. Runs: `curl -sSL https://dayview-url/install.sh | bash`
3. Script checks Screenpipe is installed, creates ~/.dayview/config.json with API key
4. Agent starts syncing. Dashboard shows data within 30 minutes.

## Required Secrets (Fly.io)
- DATABASE_URL (auto-set by `fly postgres attach`)
- GEMINI_API_KEY (for summarizer)
- SECRET_KEY (Flask sessions)

## Implementation Order

### Phase 1: Core Server
1. **db.py** — Postgres connection pool (psycopg). `get_db()` returns connection.
2. **auth.py** — User model, signup/login, `require_auth` decorator, API key validation.
3. **projects_db.py** — REFACTOR: sqlite3 → psycopg, add user_id to all functions.
4. **summarizer.py** — REFACTOR: file cache → DB table (summaries_cache), context from user record.
5. **activity_mapper.py** — REFACTOR: accept frames as input (not call screenpipe), add user_id.

### Phase 2: API + Routes
6. **ingest_routes.py** — NEW: `POST /api/ingest/activity` receives frames from agent.
7. **daily_routes.py** — REFACTOR: read from Postgres, not Screenpipe.
8. **dashboard_routes.py** — Minor: add user_id filtering.
9. **project_routes.py** — Minor: add user_id, remove local sync calls.
10. **app.py** — Init Postgres pool, register auth + ingest routes, add /health.

### Phase 3: Local Agent
11. **dayview-agent/dayview_agent.py** — Reads Screenpipe DB, deduplicates, POSTs to cloud.
12. **dayview-agent/screenpipe_reader.py** — Extracted query + dedup logic from screenpipe.py.
13. **dayview-agent/install.sh** — Creates config, downloads agent, sets up launchd/cron.

### Phase 4: Deploy
14. **fly.toml** — App config, region lax.
15. **Dockerfile** — Python + gunicorn.
16. **Postgres** — `fly postgres create --name dayview-db`
17. Deploy + verify.

## Postgres Schema

```sql
CREATE TABLE users (
    id            SERIAL PRIMARY KEY,
    email         TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    api_key       TEXT UNIQUE NOT NULL DEFAULT gen_random_uuid()::text,
    timezone      TEXT NOT NULL DEFAULT 'America/Los_Angeles',
    context_md    TEXT DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE projects (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    name        TEXT NOT NULL,
    description TEXT,
    status      TEXT NOT NULL DEFAULT 'active',
    source      TEXT,
    source_id   TEXT,
    tag         TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(user_id, name)
);

CREATE TABLE project_entries (
    id           SERIAL PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    project_id   INTEGER NOT NULL REFERENCES projects(id),
    date         DATE NOT NULL,
    achievements JSONB DEFAULT '[]',
    in_progress  JSONB DEFAULT '[]',
    blockers     JSONB DEFAULT '[]',
    source       TEXT NOT NULL DEFAULT 'screenpipe',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(project_id, date, source)
);

CREATE TABLE project_activity (
    id            SERIAL PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    project_id    INTEGER NOT NULL REFERENCES projects(id),
    date          DATE NOT NULL,
    minutes       REAL NOT NULL DEFAULT 0,
    app_breakdown JSONB DEFAULT '{}',
    frame_count   INTEGER DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(project_id, date)
);

CREATE TABLE daily_stats_cache (
    id            SERIAL PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    date          DATE NOT NULL,
    total_minutes REAL DEFAULT 0,
    roles_json    JSONB,
    top_apps_json JSONB,
    top_urls_json JSONB,
    frame_count   INTEGER DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(user_id, date)
);

CREATE TABLE summaries_cache (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    date        DATE NOT NULL,
    content     JSONB NOT NULL,
    input_chars INTEGER,
    truncated   BOOLEAN DEFAULT false,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(user_id, date)
);

CREATE TABLE corrections (
    id                    SERIAL PRIMARY KEY,
    user_id               INTEGER NOT NULL REFERENCES users(id),
    date                  DATE NOT NULL,
    action                TEXT NOT NULL,
    original_project_id   INTEGER,
    original_text         TEXT,
    corrected_project_id  INTEGER,
    corrected_text        TEXT,
    source                TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE git_summaries (
    id           SERIAL PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    project_id   INTEGER NOT NULL REFERENCES projects(id),
    date         DATE NOT NULL,
    summary      TEXT NOT NULL,
    commit_count INTEGER DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(project_id, date)
);
```

## What Gets Dropped in v1
- project_sync.py (Google Docs, Slack, Linear) — too coupled to local creds
- repo_scanner.py (local git) — could become agent feature later
- screenpipe.py on server — agent handles all Screenpipe interaction

## What Stays As-Is
- templates/index.html
- static/dayview.js (2251 lines, all API paths unchanged)
- static/dayview.css
- classifier.py (pure logic)
- meetings.py (pure logic)

## Key Numbers
- ~8000 raw OCR frames/day → ~1000 after dedup → ~500KB-1MB JSON payload
- Gemini 2.0 Flash summarizer: 80K char context limit
- Dedup threshold: 0.85 similarity (SequenceMatcher)
- Work hours: 7AM-7PM Pacific

## Existing Files
```
app.py                 # Flask bootstrap
screenpipe.py          # Local Screenpipe queries + dedup
summarizer.py          # Gemini AI summarizer
activity_mapper.py     # Frame → project matching
projects_db.py         # SQLite project storage
classifier.py          # Role classification (pure logic)
meetings.py            # Meeting detection (pure logic)
daily_routes.py        # /api/day/* endpoints
dashboard_routes.py    # /api/dashboard/* endpoints
project_routes.py      # /api/projects/* endpoints
route_helpers.py       # Shared route utilities
project_sync.py        # External integrations
repo_scanner.py        # Local git scanning
context.md             # User context for summarizer
templates/index.html   # SPA shell
static/dayview.js      # Frontend (2251 lines)
static/dayview.css     # Styles
```
