"""Flask web server for the DayView dashboard."""

from __future__ import annotations

import threading
from datetime import date as date_type, datetime

from flask import Flask, jsonify, render_template, request

from classifier import classify_frame, compute_focus_time, compute_role_minutes
from meetings import detect_meetings
from screenpipe import (
    PACIFIC,
    build_activity_text,
    build_timeline,
    deduplicate_ocr,
    get_activity_stats,
    get_audio_transcripts,
    get_ocr_frames,
    health_check,
    list_days_with_data,
    search_content,
)
from summarizer import get_cached, summarize_day

app = Flask(__name__)


def _annotate_timeline_roles(timeline: list[dict], frames: list[dict]) -> None:
    """Add a 'role' key to each timeline session based on the most common role in its frames."""
    # Build a quick timestamp → role lookup from the deduped frames
    ts_role: dict[str, str] = {}
    for f in frames:
        ts_role[f.get("timestamp", "")] = classify_frame(f)

    for session in timeline:
        # Count roles for frames in this session's time range
        start = session.get("start", "")
        end = session.get("end", "")
        role_counts: dict[str, int] = {}
        for ts, role in ts_role.items():
            if start <= ts <= end:
                role_counts[role] = role_counts.get(role, 0) + 1
        # Assign the dominant role
        if role_counts:
            session["role"] = max(role_counts, key=role_counts.get)
        else:
            session["role"] = classify_frame({"app_name": session.get("app", "")})

# Track in-flight background summarization jobs
_pending_jobs: dict[str, bool] = {}  # date_str -> True if running


def _parse_date(date_str: str) -> date_type:
    return datetime.strptime(date_str, "%Y-%m-%d").date()


def _serialize_stats(stats: dict) -> dict:
    return {
        "total_frames": stats["total_frames"],
        "unique_apps": len(stats["unique_apps"]),
        "top_apps": [{"app": app, "count": count} for app, count in stats["top_apps"]],
        "active_hours": sorted(stats["active_hours"]),
        "first_activity": stats["first_activity"],
        "last_activity": stats["last_activity"],
    }


def _auto_summarize(d: date_type, date_str: str) -> None:
    """Background thread: generate and cache structured summary."""
    try:
        frames = get_ocr_frames(d)
        deduped = deduplicate_ocr(frames)
        audio = get_audio_transcripts(d)
        if deduped or audio:
            activity_text = build_activity_text(deduped, audio)
            summarize_day(activity_text, d)
    except Exception:
        pass  # Silently fail — frontend will show "generating" state
    finally:
        _pending_jobs.pop(date_str, None)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/days")
def api_days():
    days = [d for d in list_days_with_data() if d is not None]
    return jsonify({"days": days})


@app.route("/api/day/<date_str>")
def api_day(date_str: str):
    try:
        d = _parse_date(date_str)
    except ValueError:
        return jsonify({"error": "Invalid date format"}), 400

    try:
        frames = get_ocr_frames(d)
        deduped = deduplicate_ocr(frames)
        timeline = build_timeline(deduped)
        _annotate_timeline_roles(timeline, deduped)
        stats = get_activity_stats(deduped)
        audio = get_audio_transcripts(d)
        cached = get_cached(d)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    # Auto-trigger summarization in background if not cached
    generating = False
    if cached is None and (deduped or audio) and date_str not in _pending_jobs:
        _pending_jobs[date_str] = True
        thread = threading.Thread(target=_auto_summarize, args=(d, date_str), daemon=True)
        thread.start()
        generating = True
    elif date_str in _pending_jobs:
        generating = True

    # Role classification
    roles = compute_role_minutes(deduped)
    focus_minutes = compute_focus_time(deduped)

    return jsonify({
        "date": date_str,
        "timeline": timeline,
        "stats": _serialize_stats(stats),
        "audio_count": len(audio),
        "content": cached,       # structured: {summary, insights, activities, next_steps} or null
        "generating": generating,  # true if background job is running
        "roles": roles,
        "focus_minutes": focus_minutes,
    })


@app.route("/api/roles/<date_str>")
def api_roles(date_str: str):
    try:
        d = _parse_date(date_str)
    except ValueError:
        return jsonify({"error": "Invalid date format"}), 400

    try:
        frames = get_ocr_frames(d)
        deduped = deduplicate_ocr(frames)
        roles = compute_role_minutes(deduped)
        focus = compute_focus_time(deduped)
        total = sum(r["minutes"] for r in roles)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify({
        "roles": roles,
        "focus_minutes": focus,
        "total_minutes": total,
    })


@app.route("/api/meetings/<date_str>")
def api_meetings(date_str: str):
    try:
        d = _parse_date(date_str)
    except ValueError:
        return jsonify({"error": "Invalid date format"}), 400

    try:
        frames = get_ocr_frames(d)
        deduped = deduplicate_ocr(frames)
        audio = get_audio_transcripts(d)
        meetings = detect_meetings(deduped, audio)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify({"meetings": meetings})


@app.route("/api/search")
def api_search():
    q = request.args.get("q", "")
    if not q:
        return jsonify({"error": "Missing query parameter 'q'"}), 400

    date_str = request.args.get("date")
    date_obj: date_type | None = None
    if date_str:
        try:
            date_obj = _parse_date(date_str)
        except ValueError:
            return jsonify({"error": "Invalid date format"}), 400

    try:
        results = search_content(q, date_obj)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify({"results": results})


@app.route("/api/summarize/<date_str>", methods=["POST"])
def api_summarize(date_str: str):
    try:
        d = _parse_date(date_str)
    except ValueError:
        return jsonify({"error": "Invalid date format"}), 400

    force = request.json.get("force", False) if request.json else False

    try:
        frames = get_ocr_frames(d)
        deduped = deduplicate_ocr(frames)
        audio = get_audio_transcripts(d)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    if not deduped and not audio:
        return jsonify({"error": "No data found for this date"}), 404

    try:
        activity_text = build_activity_text(deduped, audio)
        content = summarize_day(activity_text, d, force=force)
    except Exception as exc:
        msg = str(exc)
        if "api_key" in msg.lower() or "auth" in msg.lower() or "401" in msg:
            return jsonify({"error": "GEMINI_API_KEY is missing or invalid"}), 503
        return jsonify({"error": msg}), 500
    return jsonify({"content": content})


@app.route("/api/health")
def api_health():
    try:
        return jsonify(health_check())
    except Exception:
        return jsonify({"status": "screenpipe_offline"})


if __name__ == "__main__":
    import os as _os
    app.run(
        host="127.0.0.1",
        port=5051,
        debug=_os.environ.get("FLASK_DEBUG", "1") == "1",
    )
