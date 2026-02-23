from __future__ import annotations

import os
import sqlite3
import requests
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from difflib import SequenceMatcher

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SCREENPIPE_URL = "http://localhost:3030"
DB_PATH = os.path.expanduser("~/.screenpipe/db.sqlite")
PACIFIC = ZoneInfo("America/Los_Angeles")

# Skip a frame if the OCR text is >= this similar to the previous frame
# from the same (app, window) — the main compression knob.
SIMILARITY_THRESHOLD = 0.85

IGNORE_APPS = {
    "loginwindow", "Spotlight", "SystemUIServer", "ScreenSaverEngine",
    "Notification Center", "screenpipe",
}
IGNORE_WINDOW_KEYWORDS = ["password", "keychain", "1password", "bitwarden"]
IGNORE_SPEAKERS = {"nika", "steve"}
WORK_HOUR_START = 7   # 7 AM PT
WORK_HOUR_END = 19    # 7 PM PT


# ---------------------------------------------------------------------------
# DB connection
# ---------------------------------------------------------------------------
def get_db() -> sqlite3.Connection:
    # Open read-only via URI so we never accidentally write to Screenpipe's DB.
    uri = f"file:{DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------
def get_date_bounds(d: date) -> tuple[str, str]:
    """Return ISO UTC timestamps for the start and end of a Pacific calendar day."""
    # Construct midnight PT then convert to UTC-aware ISO for DB queries.
    start_pt = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=PACIFIC)
    end_pt = start_pt + timedelta(days=1)
    return start_pt.isoformat(), end_pt.isoformat()


# ---------------------------------------------------------------------------
# SQLite queries
# ---------------------------------------------------------------------------
def list_days_with_data() -> list[str]:
    # Fetch raw timestamps and group by Pacific date in Python
    # (SQLite's date() operates in UTC, which misattributes evening PT activity)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT date(timestamp) FROM frames ORDER BY 1 DESC"
        ).fetchall()
    # Quick approach: query UTC dates, but also include surrounding days.
    # For a more accurate list, we convert representative timestamps per UTC date.
    days: set[str] = set()
    for row in rows:
        utc_date_str = row[0]
        if utc_date_str:
            days.add(utc_date_str)
            # Also include the previous day (evening PT activity shows as next UTC day)
            try:
                d = datetime.strptime(utc_date_str, "%Y-%m-%d").date()
                prev = d - timedelta(days=1)
                days.add(prev.isoformat())
            except ValueError:
                pass
    # Filter to only days that actually have frames when queried with Pacific bounds
    confirmed = []
    for day_str in sorted(days, reverse=True):
        try:
            d = datetime.strptime(day_str, "%Y-%m-%d").date()
            start, end = get_date_bounds(d)
            with get_db() as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM frames WHERE timestamp >= ? AND timestamp < ? LIMIT 1",
                    (start, end),
                ).fetchone()[0]
            if count > 0:
                confirmed.append(day_str)
        except (ValueError, Exception):
            continue
    return confirmed


def get_ocr_frames(d: date) -> list[dict]:
    start, end = get_date_bounds(d)
    sql = """
        SELECT
            f.timestamp,
            f.app_name,
            f.window_name,
            f.browser_url,
            f.focused,
            f.device_name,
            o.text
        FROM frames f
        JOIN ocr_text o ON o.frame_id = f.id
        WHERE f.timestamp >= ? AND f.timestamp < ?
          AND o.text_length > 20
        ORDER BY f.timestamp
    """
    with get_db() as conn:
        rows = conn.execute(sql, (start, end)).fetchall()
    return [dict(row) for row in rows]


def get_audio_transcripts(d: date) -> list[dict]:
    start, end = get_date_bounds(d)
    sql = """
        SELECT
            a.timestamp,
            a.transcription,
            a.device,
            a.is_input_device,
            s.name AS speaker_name
        FROM audio_transcriptions a
        LEFT JOIN speakers s ON s.id = a.speaker_id
        WHERE a.timestamp >= ? AND a.timestamp < ?
        ORDER BY a.timestamp
    """
    with get_db() as conn:
        rows = conn.execute(sql, (start, end)).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Filtering / deduplication
# ---------------------------------------------------------------------------
def should_ignore(app_name: str, window_name: str) -> bool:
    if app_name in IGNORE_APPS:
        return True
    wn_lower = (window_name or "").lower()
    return any(kw in wn_lower for kw in IGNORE_WINDOW_KEYWORDS)


def text_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a[:500], b[:500]).ratio()


def deduplicate_ocr(frames: list[dict]) -> list[dict]:
    """
    Remove near-duplicate consecutive OCR frames per (app, window).
    Frames are expected to already have keys: timestamp, app_name, window_name, text.
    Input is sorted by timestamp (get_ocr_frames guarantees this).
    """
    last_text: dict[tuple[str, str], str] = {}
    unique: list[dict] = []

    for frame in frames:
        app = frame.get("app_name") or "Unknown"
        window = frame.get("window_name") or ""
        text = (frame.get("text") or "").strip()

        if should_ignore(app, window):
            continue
        if len(text) < 20:
            continue

        key = (app, window)
        if text_similarity(last_text.get(key, ""), text) < SIMILARITY_THRESHOLD:
            unique.append(frame)
            last_text[key] = text

    return unique


# ---------------------------------------------------------------------------
# Timeline builders
# ---------------------------------------------------------------------------
def build_timeline(ocr_frames: list[dict]) -> list[dict]:
    """
    Group consecutive OCR frames by app into sessions for the UI.
    Returns structured dicts — NOT a text format.
    """
    sessions: list[dict] = []
    current_app: str | None = None
    current_windows: list[str] = []
    session_start: str = ""
    session_end: str = ""
    session_texts: list[str] = []
    frame_count: int = 0

    def _flush() -> None:
        if current_app and session_texts:
            sessions.append({
                "app": current_app,
                "windows": list(dict.fromkeys(current_windows)),  # dedupe, preserve order
                "start": session_start,
                "end": session_end,
                "samples": session_texts[-5:],
                "frame_count": frame_count,
            })

    for frame in ocr_frames:
        app = frame.get("app_name") or "Unknown"
        window = frame.get("window_name") or ""
        ts = frame.get("timestamp") or ""
        text = frame.get("text") or ""

        if app != current_app:
            _flush()
            current_app = app
            current_windows = [window]
            session_start = ts
            session_end = ts
            session_texts = [text[:500]]
            frame_count = 1
        else:
            current_windows.append(window)
            session_end = ts
            session_texts.append(text[:500])
            frame_count += 1

    _flush()
    return sessions


def build_activity_text(ocr_frames: list[dict], audio: list[dict]) -> str:
    """
    Build the compressed text timeline used for LLM summarization.
    Mirrors daily-update's build_activity_timeline() but operates on
    the flat dicts returned by get_ocr_frames / get_audio_transcripts.
    """
    lines: list[str] = []

    # --- Screen activity ---
    sessions = build_timeline(ocr_frames)
    lines.append("=== SCREEN ACTIVITY ===\n")

    for s in sessions:
        # Timestamps are UTC ISO strings from the DB; trim to HH:MM for display.
        start_short = s["start"][11:16] if len(s["start"]) > 16 else s["start"]
        end_short = s["end"][11:16] if len(s["end"]) > 16 else s["end"]
        windows_str = ", ".join(s["windows"][:8])
        lines.append(f"[{start_short}–{end_short}] {s['app']} — {windows_str}")
        for sample in s["samples"]:
            clean = " ".join(sample.split())[:400]
            lines.append(f"  > {clean}")
        lines.append("")

    # --- Audio ---
    if audio:
        work_audio: list[str] = []
        for item in audio:
            ts_str = item.get("timestamp") or ""
            text = (item.get("transcription") or "").strip()
            device = item.get("device") or ""
            speaker = item.get("speaker_name") or ""

            if not text:
                continue

            if speaker and speaker.lower() in IGNORE_SPEAKERS:
                continue
            # Also filter if any ignored name is spoken in the transcript itself
            text_lower = text.lower()
            if any(name in text_lower for name in IGNORE_SPEAKERS):
                continue

            # Work-hours gate
            try:
                ts_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                local_hour = ts_dt.astimezone(PACIFIC).hour
                if local_hour < WORK_HOUR_START or local_hour >= WORK_HOUR_END:
                    continue
            except (ValueError, AttributeError):
                pass

            ts_short = ts_str[11:16]
            prefix = f"[{ts_short}]"
            if speaker:
                prefix += f" {speaker}:"
            elif "output" in device.lower():
                prefix += " [other person]:"
            else:
                prefix += " [you]:"
            work_audio.append(f"{prefix} {text}")

        if work_audio:
            lines.append("\n=== SPOKEN / AUDIO ===\n")
            lines.extend(work_audio)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------
def get_activity_stats(ocr_frames: list[dict]) -> dict:
    if not ocr_frames:
        return {
            "total_frames": 0,
            "unique_apps": set(),
            "top_apps": [],
            "active_hours": set(),
            "first_activity": None,
            "last_activity": None,
        }

    app_counts: dict[str, int] = {}
    active_hours: set[int] = set()
    timestamps: list[str] = []

    for frame in ocr_frames:
        app = frame.get("app_name") or "Unknown"
        ts_str = frame.get("timestamp") or ""

        app_counts[app] = app_counts.get(app, 0) + 1

        if ts_str:
            timestamps.append(ts_str)
            try:
                ts_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                active_hours.add(ts_dt.astimezone(PACIFIC).hour)
            except (ValueError, AttributeError):
                pass

    top_apps = sorted(app_counts.items(), key=lambda x: x[1], reverse=True)

    return {
        "total_frames": len(ocr_frames),
        "unique_apps": set(app_counts.keys()),
        "top_apps": top_apps,
        "active_hours": active_hours,
        "first_activity": min(timestamps) if timestamps else None,
        "last_activity": max(timestamps) if timestamps else None,
    }


# ---------------------------------------------------------------------------
# REST API helpers
# ---------------------------------------------------------------------------
def search_content(query: str, d: date | None = None) -> list[dict]:
    """Search Screenpipe via REST. Returns up to 50 normalised result dicts."""
    params: dict = {"q": query, "limit": 50}
    if d is not None:
        start, end = get_date_bounds(d)
        params["start_time"] = start
        params["end_time"] = end

    resp = requests.get(
        f"{SCREENPIPE_URL}/search",
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    raw = resp.json().get("data", [])

    results: list[dict] = []
    for item in raw:
        item_type = item.get("type", "").lower()
        c = item.get("content", {})

        if item_type == "ocr":
            results.append({
                "type": "ocr",
                "timestamp": c.get("timestamp"),
                "app_name": c.get("app_name"),
                "window_name": c.get("window_name"),
                "text": c.get("text"),
            })
        elif item_type == "audio":
            results.append({
                "type": "audio",
                "timestamp": c.get("timestamp"),
                "device": c.get("device_name"),
                "speaker": c.get("speaker", {}).get("name") if c.get("speaker") else None,
                "text": c.get("transcription"),
            })

    return results


def health_check() -> dict:
    resp = requests.get(f"{SCREENPIPE_URL}/health", timeout=5)
    resp.raise_for_status()
    return resp.json()
