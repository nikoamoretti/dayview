from __future__ import annotations

import json
import os
import sqlite3

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(_DIR, "projects.db")

# ---------------------------------------------------------------------------
# Tag taxonomy — 6 categories for grouping projects
# ---------------------------------------------------------------------------
TAG_COLORS: dict[str, str] = {
    "Outbound":       "#F59E0B",  # amber
    "Product":        "#4A9EFF",  # blue
    "Intelligence":   "#8B5CF6",  # violet
    "Product Vision": "#EC4899",  # pink
    "GTM":            "#10B981",  # emerald
    "Internal":       "#64748B",  # slate
}

TAG_ORDER = list(TAG_COLORS.keys())

# project name (case-insensitive) → tag
_DEFAULT_TAGS: dict[str, str] = {
    "Q1 Outbound":                       "Outbound",
    "Market Consist Email Sequences":    "Outbound",
    "Chemicals Sequence":                "Outbound",
    "LinkedIn Outreach":                 "Outbound",
    "Mexico Expansion":                  "Outbound",
    "Objection handling doc":            "Outbound",
    "Objection handling doc. for cold calling": "Outbound",
    "Sales Dashboard":                   "Product",
    "Sales Dashboard Development":       "Product",
    "Outbound Dashboard":                "Product",
    "DayView":                           "Product",
    "Telegraph KB":                      "Product",
    "Rail Webcam":                       "Product",
    "Revenue Hub":                       "Product",
    "Lead Generation":                   "Intelligence",
    "Metabase analysis":                 "Intelligence",
    "Rail Spur Data Enrichment":         "Intelligence",
    "Demurrage PRD":                     "Product Vision",
    "Lease Health":                      "Product Vision",
    "Lease Health Prototype":            "Product Vision",
    "RFC Lease Management":              "Product Vision",
    "Channel Partnership":               "GTM",
    "Channel Partnership Development":   "GTM",
    "Demo Prep":                         "GTM",
    "Shipper GtM":                       "GTM",
    "Board meeting update":              "GTM",
    "CS survey":                         "Internal",
    "Customer Survey Analysis":          "Internal",
    "CS survey coding + hosting setup":  "Internal",
    "SEARS/SWARS prep":                  "Internal",
    "Ops Kickoff":                       "Internal",
    "Nico To Do":                        "Internal",
}


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------
def get_db() -> sqlite3.Connection:
    """Open a read/write connection to the projects database."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
def init_db() -> None:
    """Create tables if they don't exist. Safe to call on every startup."""
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL UNIQUE,
                description TEXT,
                status      TEXT NOT NULL DEFAULT 'active',
                source      TEXT,
                source_id   TEXT,
                tag         TEXT,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS project_entries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id  INTEGER NOT NULL REFERENCES projects(id),
                date        TEXT NOT NULL,
                achievements TEXT,
                in_progress  TEXT,
                blockers     TEXT,
                source       TEXT NOT NULL DEFAULT 'screenpipe',
                created_at   TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(project_id, date, source)
            );

            CREATE TABLE IF NOT EXISTS git_summaries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id  INTEGER NOT NULL,
                date        TEXT NOT NULL,
                summary     TEXT NOT NULL,
                commit_count INTEGER DEFAULT 0,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(project_id, date)
            );

            CREATE TABLE IF NOT EXISTS daily_stats_cache (
                date          TEXT PRIMARY KEY,
                total_minutes REAL DEFAULT 0,
                roles_json    TEXT,
                top_apps_json TEXT,
                top_urls_json TEXT,
                frame_count   INTEGER DEFAULT 0,
                created_at    TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS corrections (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                date                  TEXT NOT NULL,
                action                TEXT NOT NULL,
                original_project_id   INTEGER,
                original_text         TEXT,
                corrected_project_id  INTEGER,
                corrected_text        TEXT,
                source                TEXT,
                created_at            TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)

        # Migrate: add tag column if missing (existing DBs)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(projects)").fetchall()}
        if "tag" not in cols:
            conn.execute("ALTER TABLE projects ADD COLUMN tag TEXT")

        # Seed default tags for projects that don't have one yet
        _seed_default_tags(conn)


def _seed_default_tags(conn: sqlite3.Connection) -> None:
    """Set tags for known projects that have tag=NULL."""
    lookup = {k.lower(): v for k, v in _DEFAULT_TAGS.items()}
    rows = conn.execute("SELECT id, name FROM projects WHERE tag IS NULL").fetchall()
    for row in rows:
        tag = lookup.get(row["name"].lower())
        if tag:
            conn.execute("UPDATE projects SET tag = ? WHERE id = ?", (tag, row["id"]))


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------
def upsert_project(
    name: str,
    description: str | None = None,
    status: str = "active",
    source: str | None = None,
    source_id: str | None = None,
) -> int:
    """Insert or update a project by name (case-insensitive). Returns the project id.

    If a project with the given name already exists, only non-None keyword
    arguments overwrite the stored values.
    """
    with get_db() as conn:
        # Case-insensitive lookup: reuse existing name casing if found
        existing = conn.execute(
            "SELECT id, name FROM projects WHERE name = ? COLLATE NOCASE",
            (name,),
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE projects SET
                    description = COALESCE(?, description),
                    status      = COALESCE(?, status),
                    source      = COALESCE(?, source),
                    source_id   = COALESCE(?, source_id),
                    updated_at  = datetime('now')
                WHERE id = ?""",
                (description, status, source, source_id, existing["id"]),
            )
            return existing["id"]
        else:
            conn.execute(
                """INSERT INTO projects (name, description, status, source, source_id)
                VALUES (?, ?, ?, ?, ?)""",
                (name, description, status, source, source_id),
            )
            row = conn.execute(
                "SELECT id FROM projects WHERE name = ?", (name,)
            ).fetchone()
            return row["id"]


def get_all_projects(status: str | None = None) -> list[dict]:
    """Return all projects, optionally filtered by status.

    Args:
        status: One of 'active', 'paused', 'completed', or None for all.
    """
    with get_db() as conn:
        if status is not None:
            rows = conn.execute(
                "SELECT * FROM projects WHERE status = ? ORDER BY name",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM projects ORDER BY name"
            ).fetchall()
    return [dict(row) for row in rows]


def get_project(project_id: int) -> dict | None:
    """Return a single project by id, or None if not found."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
    return dict(row) if row else None


def update_project_status(project_id: int, status: str) -> None:
    """Set the status of a project and bump updated_at.

    Args:
        project_id: The project's id.
        status: One of 'active', 'paused', 'completed'.
    """
    with get_db() as conn:
        conn.execute(
            "UPDATE projects SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (status, project_id),
        )


def update_project_tag(project_id: int, tag: str | None) -> None:
    """Set or clear the tag for a project."""
    with get_db() as conn:
        conn.execute(
            "UPDATE projects SET tag = ?, updated_at = datetime('now') WHERE id = ?",
            (tag, project_id),
        )


def get_active_project_names() -> list[str]:
    """Return a flat list of active project names — lightweight, for LLM prompts."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT name FROM projects WHERE status = 'active' ORDER BY name"
        ).fetchall()
    return [row["name"] for row in rows]


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------
def add_entry(
    project_id: int,
    date: str,
    achievements: list[str] | None = None,
    in_progress: list[str] | None = None,
    blockers: list[str] | None = None,
    source: str = "screenpipe",
) -> int:
    """Insert or replace a progress entry for a project on a given date.

    The UNIQUE constraint is on (project_id, date, source), so re-running a
    source for the same day overwrites the previous entry rather than failing.

    Args:
        project_id:   Foreign key to projects.id.
        date:         Calendar date string in YYYY-MM-DD format.
        achievements: List of completed items.
        in_progress:  List of in-flight items.
        blockers:     List of blockers.
        source:       Origin of the data ('screenpipe', 'slack', 'manual').

    Returns:
        The id of the inserted or replaced row.
    """
    achievements_json = json.dumps(achievements or [])
    in_progress_json  = json.dumps(in_progress  or [])
    blockers_json     = json.dumps(blockers      or [])

    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO project_entries
                (project_id, date, achievements, in_progress, blockers, source)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, date, source) DO UPDATE SET
                achievements = excluded.achievements,
                in_progress  = excluded.in_progress,
                blockers     = excluded.blockers
            """,
            (project_id, date, achievements_json, in_progress_json, blockers_json, source),
        )
        row_id = cur.lastrowid or conn.execute(
            "SELECT id FROM project_entries WHERE project_id=? AND date=? AND source=?",
            (project_id, date, source),
        ).fetchone()["id"]
    return row_id


def _deserialize_entry(row: sqlite3.Row) -> dict:
    """Convert a project_entries row to a dict with parsed JSON fields."""
    entry = dict(row)
    for field in ("achievements", "in_progress", "blockers"):
        raw = entry.get(field)
        entry[field] = json.loads(raw) if raw else []
    return entry


def get_entries_for_project(project_id: int, limit: int = 30) -> list[dict]:
    """Return the most recent entries for a project, newest first.

    Args:
        project_id: The project's id.
        limit:      Maximum number of rows to return.
    """
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM project_entries
            WHERE project_id = ?
            ORDER BY date DESC, created_at DESC
            LIMIT ?
            """,
            (project_id, limit),
        ).fetchall()
    return [_deserialize_entry(row) for row in rows]


def get_entries_for_date(date_str: str) -> list[dict]:
    """Return all project entries for a specific date across all projects.

    Args:
        date_str: Date in YYYY-MM-DD format.
    """
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT pe.*, p.name AS project_name, p.tag AS project_tag
            FROM project_entries pe
            JOIN projects p ON p.id = pe.project_id
            WHERE pe.date = ?
            ORDER BY p.name
            """,
            (date_str,),
        ).fetchall()
    return [_deserialize_entry(row) for row in rows]


def get_project_timeline(project_id: int) -> list[dict]:
    """Return all entries for a project ordered oldest first — for timeline view.

    Args:
        project_id: The project's id.
    """
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM project_entries
            WHERE project_id = ?
            ORDER BY date ASC, created_at ASC
            """,
            (project_id,),
        ).fetchall()
    return [_deserialize_entry(row) for row in rows]


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------
def add_correction(
    date: str,
    action: str,
    original_project_id: int | None = None,
    original_text: str | None = None,
    corrected_project_id: int | None = None,
    corrected_text: str | None = None,
    source: str | None = None,
) -> int:
    """Record a user correction for LLM learning."""
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO corrections
               (date, action, original_project_id, original_text,
                corrected_project_id, corrected_text, source)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (date, action, original_project_id, original_text,
             corrected_project_id, corrected_text, source),
        )
        return cur.lastrowid


def get_recent_corrections(limit: int = 50) -> list[dict]:
    """Fetch recent corrections, newest first."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT c.*, p1.name AS orig_project_name, p2.name AS corr_project_name
               FROM corrections c
               LEFT JOIN projects p1 ON p1.id = c.original_project_id
               LEFT JOIN projects p2 ON p2.id = c.corrected_project_id
               ORDER BY c.created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Item-level CRUD (operates on JSON arrays inside project_entries)
# ---------------------------------------------------------------------------
def _get_entry_row(conn: sqlite3.Connection, entry_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM project_entries WHERE id = ?", (entry_id,)
    ).fetchone()
    if not row:
        raise ValueError(f"Entry {entry_id} not found")
    return row


def _parse_field(row: sqlite3.Row, field: str) -> list:
    raw = row[field]
    return json.loads(raw) if raw else []


def _save_field(conn: sqlite3.Connection, entry_id: int, field: str, items: list) -> None:
    if field not in ("achievements", "in_progress", "blockers"):
        raise ValueError(f"Invalid field: {field}")
    conn.execute(
        f"UPDATE project_entries SET {field} = ? WHERE id = ?",
        (json.dumps(items), entry_id),
    )


def _cleanup_empty_entry(conn: sqlite3.Connection, entry_id: int) -> bool:
    """Delete entry if all arrays are empty. Returns True if deleted."""
    row = conn.execute(
        "SELECT achievements, in_progress, blockers FROM project_entries WHERE id = ?",
        (entry_id,),
    ).fetchone()
    if not row:
        return True
    for f in ("achievements", "in_progress", "blockers"):
        items = json.loads(row[f]) if row[f] else []
        if items:
            return False
    conn.execute("DELETE FROM project_entries WHERE id = ?", (entry_id,))
    return True


def update_entry_item(entry_id: int, field: str, item_index: int, new_text: str) -> None:
    """Edit one item within a JSON array field."""
    if field not in ("achievements", "in_progress", "blockers"):
        raise ValueError(f"Invalid field: {field}")
    with get_db() as conn:
        row = _get_entry_row(conn, entry_id)
        items = _parse_field(row, field)
        if item_index < 0 or item_index >= len(items):
            raise ValueError(f"Index {item_index} out of range for {field}")
        items[item_index] = new_text
        _save_field(conn, entry_id, field, items)


def delete_entry_item(entry_id: int, field: str, item_index: int) -> None:
    """Remove one item from a JSON array. Cleans up empty entries."""
    if field not in ("achievements", "in_progress", "blockers"):
        raise ValueError(f"Invalid field: {field}")
    with get_db() as conn:
        row = _get_entry_row(conn, entry_id)
        items = _parse_field(row, field)
        if item_index < 0 or item_index >= len(items):
            raise ValueError(f"Index {item_index} out of range for {field}")
        items.pop(item_index)
        _save_field(conn, entry_id, field, items)
        _cleanup_empty_entry(conn, entry_id)


def move_entry_item(
    src_entry_id: int,
    src_field: str,
    item_index: int,
    target_project_id: int,
    date: str,
) -> None:
    """Move an item from one project's entry to another project."""
    if src_field not in ("achievements", "in_progress", "blockers"):
        raise ValueError(f"Invalid field: {src_field}")
    with get_db() as conn:
        # Read and remove from source
        row = _get_entry_row(conn, src_entry_id)
        items = _parse_field(row, src_field)
        if item_index < 0 or item_index >= len(items):
            raise ValueError(f"Index {item_index} out of range")
        text = items.pop(item_index)
        _save_field(conn, src_entry_id, src_field, items)
        _cleanup_empty_entry(conn, src_entry_id)

        # Upsert into target project's entry for this date
        target_row = conn.execute(
            """SELECT id, achievements, in_progress, blockers
               FROM project_entries
               WHERE project_id = ? AND date = ? AND source = 'screenpipe'""",
            (target_project_id, date),
        ).fetchone()

        if target_row:
            target_items = json.loads(target_row[src_field]) if target_row[src_field] else []
            target_items.append(text)
            _save_field(conn, target_row["id"], src_field, target_items)
        else:
            data = {"achievements": [], "in_progress": [], "blockers": []}
            data[src_field] = [text]
            conn.execute(
                """INSERT INTO project_entries
                   (project_id, date, achievements, in_progress, blockers, source)
                   VALUES (?, ?, ?, ?, ?, 'screenpipe')""",
                (target_project_id, date,
                 json.dumps(data["achievements"]),
                 json.dumps(data["in_progress"]),
                 json.dumps(data["blockers"])),
            )


# ---------------------------------------------------------------------------
# Project management
# ---------------------------------------------------------------------------
def rename_project(project_id: int, new_name: str) -> None:
    """Rename a project. Raises ValueError if the name is taken."""
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM projects WHERE name = ? COLLATE NOCASE AND id != ?",
            (new_name, project_id),
        ).fetchone()
        if existing:
            raise ValueError(f"Project name '{new_name}' already exists")
        conn.execute(
            "UPDATE projects SET name = ?, updated_at = datetime('now') WHERE id = ?",
            (new_name, project_id),
        )


def delete_project(project_id: int) -> None:
    """Delete a project and all its entries."""
    with get_db() as conn:
        conn.execute("DELETE FROM project_entries WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM git_summaries WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


def create_project(name: str, tag: str | None = None, description: str | None = None) -> int:
    """Create a new project. Returns the new project id."""
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM projects WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()
        if existing:
            raise ValueError(f"Project '{name}' already exists")
        cur = conn.execute(
            """INSERT INTO projects (name, tag, description, status)
               VALUES (?, ?, ?, 'active')""",
            (name, tag, description),
        )
        return cur.lastrowid
