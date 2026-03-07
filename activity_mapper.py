"""activity_mapper.py — Connect Screenpipe OCR frames to DayView projects.

Each OCR frame represents ~5 seconds of activity (Screenpipe default capture
rate).  Frames are matched against projects via keyword sets built from each
project's name and description, plus a small set of hand-coded domain aliases.
Aggregated results are upserted into the `project_activity` table.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

import projects_db
import screenpipe
from classifier import compute_role_minutes, ROLE_COLORS

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS project_activity (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   INTEGER NOT NULL REFERENCES projects(id),
    date         TEXT NOT NULL,
    minutes      REAL NOT NULL DEFAULT 0,
    app_breakdown TEXT,
    frame_count  INTEGER DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, date)
);
"""

# Seconds each Screenpipe frame represents.
_SECONDS_PER_FRAME = 5
_MINUTES_PER_FRAME = _SECONDS_PER_FRAME / 60.0


def init_activity_db() -> None:
    """Create project_activity table if it does not already exist."""
    with projects_db.get_db() as conn:
        conn.execute(_CREATE_TABLE)
        conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_project_activity_date
                ON project_activity(date);
            CREATE INDEX IF NOT EXISTS idx_project_activity_project_date
                ON project_activity(project_id, date);
        """)


# ---------------------------------------------------------------------------
# Keyword registry — hand-coded aliases for known project patterns
# ---------------------------------------------------------------------------
# Keys are lowercased substrings of project names; values are extra keywords
# to add to that project's match set.  These are HIGH-SIGNAL phrases that
# uniquely identify a project in OCR text.
_PROJECT_ALIASES: dict[str, set[str]] = {
    "q1 outbound":        {"hubspot", "apollo.io", "cold call", "cold calling", "dialing session", "dials today"},
    "mexico":             {"ferromex", "fxe", "mexico rail", "inmails"},
    "lease health":       {"lease health", "leasehealth", "demurrage calculator"},
    "rail webcam":        {"railcam", "webcam monitor", "yolo detection", "railcar detection"},
    "sales dashboard":    {"sales-dashboard", "dashboard_v2", "call_intel", "sales analytics"},
    "revenue hub":        {"railhub", "revenue hub", "telegraph seo"},
    "outbound dashboard": {"outbound dashboard", "cold-calling-stats"},
    "chemicals sequence": {"chemicals sequence", "chemical manufacturer"},
    "linkedin outreach":  {"linkedin.com/in/", "linkedin outreach", "linkedin-tracker"},
    "rail spur":          {"rail spur", "rail-network-scanner", "osm extraction"},
    "yard prd":           {"yard prd", "yard workplan", "rail yard"},
    "conference":         {"manifest", "iana", "conference registration"},
    "metabase":           {"metabase.com", "metabase analysis", "metabase query"},
}

# Stopwords to exclude from auto-generated keyword sets.
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "for", "nor", "not", "yet", "so",
    "in", "on", "at", "to", "of", "by", "up", "is", "it", "as", "if", "do",
    "be", "we", "he", "me", "my", "no", "vs", "its", "our", "all", "any",
    "can", "had", "has", "her", "him", "his", "how", "may", "new", "now",
    "old", "own", "say", "she", "too", "use", "way", "who", "did", "get",
    "got", "let", "out", "put", "run", "set", "try", "was", "are", "been",
    "from", "have", "into", "just", "like", "make", "many", "more", "most",
    "much", "must", "need", "only", "over", "some", "such", "than", "that",
    "them", "then", "this", "very", "what", "when", "will", "with", "also",
    "back", "been", "both", "come", "each", "even", "give", "here", "just",
    "keep", "last", "long", "look", "made", "next", "once", "part", "plan",
    "same", "take", "tell", "they", "were", "work", "year", "your",
    # Domain-generic words that appear in nearly every frame
    "app", "code", "data", "file", "help", "home", "info", "line", "list",
    "main", "menu", "name", "page", "save", "show", "tab", "text", "tool",
    "type", "user", "view", "web", "window", "open", "close", "edit",
    "search", "click", "button", "chrome", "slack", "linear",
    # Words too generic for project matching
    "project", "update", "status", "review", "prep", "planning", "setup",
    "development", "analysis", "strategy", "delivery", "preparation",
    "completing", "developing", "testing", "focusing", "explore", "implement",
    "documentation", "tracking", "comprehensive", "brainstorming", "outcomes",
    # Telegraph domain words that appear in almost every screen
    "sales", "rail", "railroad", "email", "calls", "calling", "sequence",
    "dashboard", "report", "meeting", "notes", "channel", "partner",
    "market", "prospect", "prospects", "leads", "pipeline", "deals",
    "board", "consist", "coding", "hosting", "survey",
})


def _build_keywords(project: dict) -> set[str]:
    """Return the keyword set for a project.

    Strategy:
    1. Extract the full lowercased name as a phrase keyword (high signal).
    2. Extract multi-word bigrams from the name (medium signal).
    3. Add only name words that are 5+ chars and not stopwords.
    4. Overlay any hand-coded aliases.
    5. Description words are EXCLUDED — too noisy for substring matching.
    """
    name = (project.get("name") or "").strip()
    name_lower = name.lower()

    keywords: set[str] = set()

    # Always include the full name as a phrase keyword
    keywords.add(name_lower)

    # Extract significant words from name only (not description)
    name_tokens = re.findall(r"[a-z0-9][\w\-]{1,}", name_lower)
    # Bigrams from the name — these are much more specific than single words
    for i in range(len(name_tokens) - 1):
        bigram = f"{name_tokens[i]} {name_tokens[i+1]}"
        keywords.add(bigram)

    # Single words from name only if 5+ chars and not a stopword
    for token in name_tokens:
        if len(token) >= 5 and token not in _STOPWORDS:
            keywords.add(token)

    # Apply hand-coded aliases
    for alias_key, extras in _PROJECT_ALIASES.items():
        if alias_key in name_lower:
            keywords.update(extras)

    return keywords


# ---------------------------------------------------------------------------
# Frame matching
# ---------------------------------------------------------------------------

# Minimum number of distinct keyword hits for a frame to be attributed.
_MIN_HITS = 1  # For phrase/bigram keywords, 1 hit is sufficient since they're specific


def _frame_matches(frame: dict, keywords: set[str]) -> bool:
    """Return True if the frame matches the project's keyword set.

    Uses a tiered approach:
    - Any multi-word keyword match → immediate True (high specificity)
    - Single-word keywords → require the word to appear as a distinct token
    """
    window = (frame.get("window_name") or frame.get("window_title") or "").lower()
    text = (frame.get("text") or "")[:500].lower()
    haystack = f"{window} {text}"

    for kw in keywords:
        if " " in kw:
            # Multi-word phrase: substring match is fine (high specificity)
            if kw in haystack:
                return True
        else:
            # Single word: require word boundary match to avoid partial hits
            # e.g., "sear" shouldn't match "search"
            if re.search(rf"\b{re.escape(kw)}\b", haystack):
                return True
    return False


# ---------------------------------------------------------------------------
# Core mapping
# ---------------------------------------------------------------------------
def map_activity_for_date(d: date) -> dict:
    """Match OCR frames to projects for *d* and upsert into project_activity.

    Returns a summary dict::

        {
            "date": "2026-02-25",
            "total_frames": 1234,
            "projects": [
                {"project_id": 1, "name": "Q1 Outbound", "minutes": 45.0,
                 "frame_count": 540, "app_breakdown": {"Chrome": 400, ...}},
                ...
            ]
        }
    """
    init_activity_db()

    # Fetch and deduplicate OCR frames
    raw_frames = screenpipe.get_ocr_frames(d)
    frames = screenpipe.deduplicate_ocr(raw_frames)

    # Load all projects and precompute keyword sets
    projects = projects_db.get_all_projects()
    project_keywords: list[tuple[dict, set[str]]] = [
        (p, _build_keywords(p)) for p in projects
    ]

    # Per-project accumulators: {project_id: {"frame_count": int, "apps": {app: count}}}
    accumulators: dict[int, dict] = {}
    for p, _ in project_keywords:
        accumulators[p["id"]] = {"frame_count": 0, "apps": {}}

    # Match each frame against every project
    for frame in frames:
        app = frame.get("app_name") or "Unknown"
        for p, keywords in project_keywords:
            if _frame_matches(frame, keywords):
                acc = accumulators[p["id"]]
                acc["frame_count"] += 1
                acc["apps"][app] = acc["apps"].get(app, 0) + 1

    # Upsert results and build return value
    date_str = d.isoformat()
    summary_projects: list[dict] = []

    with projects_db.get_db() as conn:
        for p, _ in project_keywords:
            pid = p["id"]
            acc = accumulators[pid]
            frame_count = acc["frame_count"]
            if frame_count == 0:
                # Still upsert zeros so stale detection is accurate
                minutes = 0.0
                app_breakdown_json = "{}"
            else:
                minutes = round(frame_count * _MINUTES_PER_FRAME, 2)
                app_breakdown_json = json.dumps(acc["apps"])

            conn.execute(
                """
                INSERT INTO project_activity (project_id, date, minutes, app_breakdown, frame_count)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id, date) DO UPDATE SET
                    minutes       = excluded.minutes,
                    app_breakdown = excluded.app_breakdown,
                    frame_count   = excluded.frame_count
                """,
                (pid, date_str, minutes, app_breakdown_json, frame_count),
            )

            if frame_count > 0:
                summary_projects.append({
                    "project_id": pid,
                    "name": p["name"],
                    "minutes": minutes,
                    "frame_count": frame_count,
                    "app_breakdown": acc["apps"],
                })

    summary_projects.sort(key=lambda x: x["minutes"], reverse=True)

    return {
        "date": date_str,
        "total_frames": len(frames),
        "projects": summary_projects,
    }


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------
def get_project_activity(project_id: int, days: int = 30) -> list[dict]:
    """Return daily activity rows for *project_id* over the last *days* days.

    Results are ordered oldest-first so they are chart-friendly.
    Rows with zero frame_count are included so the caller sees gaps.
    """
    init_activity_db()
    cutoff = (datetime.utcnow().date() - timedelta(days=days)).isoformat()
    with projects_db.get_db() as conn:
        rows = conn.execute(
            """
            SELECT date, minutes, frame_count, app_breakdown
            FROM project_activity
            WHERE project_id = ? AND date >= ?
            ORDER BY date ASC
            """,
            (project_id, cutoff),
        ).fetchall()

    result: list[dict] = []
    for row in rows:
        entry = dict(row)
        raw = entry.get("app_breakdown") or "{}"
        try:
            entry["app_breakdown"] = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            entry["app_breakdown"] = {}
        result.append(entry)
    return result


def get_total_screen_minutes(d: date) -> float:
    """Return the total unique screen minutes for a date (deduped frames * 5s)."""
    raw = screenpipe.get_ocr_frames(d)
    deduped = screenpipe.deduplicate_ocr(raw)
    return round(len(deduped) * _MINUTES_PER_FRAME, 1)


def has_activity_for_date(date_str: str) -> bool:
    """Return True if the date already has cached project_activity rows."""
    init_activity_db()
    with projects_db.get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM project_activity WHERE date = ? LIMIT 1",
            (date_str,),
        ).fetchone()
    return row is not None


def get_activity_for_date(date_str: str) -> list[dict]:
    """Return all project activity for *date_str* (YYYY-MM-DD), sorted by minutes desc.

    Joins with the projects table to include the project name.
    Zero-minute rows are excluded — only projects that matched frames appear.
    """
    init_activity_db()
    with projects_db.get_db() as conn:
        rows = conn.execute(
            """
            SELECT pa.project_id, p.name AS project_name,
                   pa.minutes, pa.frame_count, pa.app_breakdown
            FROM project_activity pa
            JOIN projects p ON p.id = pa.project_id
            WHERE pa.date = ? AND pa.frame_count > 0
            ORDER BY pa.minutes DESC
            """,
            (date_str,),
        ).fetchall()

    result: list[dict] = []
    for row in rows:
        entry = dict(row)
        raw = entry.get("app_breakdown") or "{}"
        try:
            entry["app_breakdown"] = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            entry["app_breakdown"] = {}
        result.append(entry)
    return result


def get_stale_projects(days_threshold: int = 5) -> list[dict]:
    """Return active projects with no activity in the last *days_threshold* days.

    A project is considered active if its `status` column is 'active'.
    Activity is checked in both `project_entries` (manual/LLM entries) and
    `project_activity` (OCR-based frame matches).  A project is *not* stale
    if it has a recent row in either table.
    """
    init_activity_db()
    cutoff = (datetime.utcnow().date() - timedelta(days=days_threshold)).isoformat()

    with projects_db.get_db() as conn:
        rows = conn.execute(
            """
            SELECT p.id, p.name, p.description, p.status,
                   MAX(pe.date)  AS last_entry_date,
                   MAX(pa.date)  AS last_activity_date
            FROM projects p
            LEFT JOIN project_entries  pe ON pe.project_id = p.id AND pe.date  >= ?
            LEFT JOIN project_activity pa ON pa.project_id = p.id AND pa.date  >= ?
                                          AND pa.frame_count > 0
            WHERE p.status = 'active'
            GROUP BY p.id
            HAVING last_entry_date IS NULL AND last_activity_date IS NULL
            ORDER BY p.name
            """,
            (cutoff, cutoff),
        ).fetchall()

    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Daily stats cache — avoids re-querying Screenpipe for the Activity tab
# ---------------------------------------------------------------------------

def get_or_compute_daily_stats(d: date) -> dict | None:
    """Return cached daily stats, or compute and cache them.

    Returns dict with keys: total_minutes, roles, top_apps, top_urls, frame_count.
    Returns None if no Screenpipe data for the day.
    """
    date_str = d.isoformat()

    # Check cache first
    with projects_db.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM daily_stats_cache WHERE date = ?", (date_str,)
        ).fetchone()

    if row:
        return {
            "total_minutes": row["total_minutes"],
            "roles": json.loads(row["roles_json"]) if row["roles_json"] else [],
            "top_apps": json.loads(row["top_apps_json"]) if row["top_apps_json"] else [],
            "top_urls": json.loads(row["top_urls_json"]) if row["top_urls_json"] else [],
            "frame_count": row["frame_count"],
        }

    # Compute from Screenpipe
    try:
        frames = screenpipe.get_ocr_frames(d)
        deduped = screenpipe.deduplicate_ocr(frames)
    except Exception:
        return None

    if not deduped:
        return None

    roles = compute_role_minutes(deduped)
    screen_min = round(len(deduped) * _MINUTES_PER_FRAME, 1)

    # Aggregate apps
    app_counts: dict[str, int] = {}
    url_counts: dict[str, int] = {}
    for frame in deduped:
        app = frame.get("app_name") or "Unknown"
        app_counts[app] = app_counts.get(app, 0) + 1
        url = frame.get("browser_url") or ""
        if url:
            try:
                host = urlparse(url).hostname or ""
            except Exception:
                host = ""
            if host:
                url_counts[host] = url_counts.get(host, 0) + 1

    top_apps = [
        {"app": a, "minutes": round(c * _MINUTES_PER_FRAME, 1)}
        for a, c in sorted(app_counts.items(), key=lambda x: x[1], reverse=True)[:15]
    ]
    top_urls = [
        {"domain": d_name, "minutes": round(c * _MINUTES_PER_FRAME, 1)}
        for d_name, c in sorted(url_counts.items(), key=lambda x: x[1], reverse=True)[:15]
    ]

    result = {
        "total_minutes": screen_min,
        "roles": roles,
        "top_apps": top_apps,
        "top_urls": top_urls,
        "frame_count": len(deduped),
    }

    # Cache it (don't cache today — it's still accumulating)
    if d < date.today():
        with projects_db.get_db() as conn:
            conn.execute(
                """INSERT INTO daily_stats_cache (date, total_minutes, roles_json,
                    top_apps_json, top_urls_json, frame_count)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    total_minutes = excluded.total_minutes,
                    roles_json = excluded.roles_json,
                    top_apps_json = excluded.top_apps_json,
                    top_urls_json = excluded.top_urls_json,
                    frame_count = excluded.frame_count""",
                (date_str, screen_min, json.dumps(roles),
                 json.dumps(top_apps), json.dumps(top_urls), len(deduped)),
            )

    return result
