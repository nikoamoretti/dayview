"""Flask web server for the DayView dashboard."""

from __future__ import annotations

import json
import threading
from datetime import date as date_type, datetime, timedelta

from flask import Flask, jsonify, render_template, request

from classifier import classify_frame, compute_focus_time, compute_role_minutes
from meetings import detect_meetings
from screenpipe import (
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


# ---------------------------------------------------------------------------
# Projects API
# ---------------------------------------------------------------------------
import projects_db

projects_db.init_db()


@app.route("/api/projects")
def api_projects():
    status = request.args.get("status")
    projects = projects_db.get_all_projects(status=status)

    # Batch: last achievement text for each project (avoids N+1)
    achievement_map: dict[int, str] = {}
    try:
        with projects_db.get_db() as conn:
            rows = conn.execute("""
                SELECT pe.project_id, pe.achievements, pe.date
                FROM project_entries pe
                INNER JOIN (
                    SELECT project_id, MAX(date) as max_date
                    FROM project_entries
                    WHERE achievements IS NOT NULL AND achievements != '[]'
                    GROUP BY project_id
                ) latest ON pe.project_id = latest.project_id
                        AND pe.date = latest.max_date
                ORDER BY pe.date DESC
            """).fetchall()
            for row in rows:
                pid = row["project_id"]
                if pid not in achievement_map:
                    try:
                        items = json.loads(row["achievements"])
                        if items:
                            achievement_map[pid] = items[0]
                    except (json.JSONDecodeError, TypeError):
                        pass
    except Exception:
        pass

    for p in projects:
        pid = p["id"]
        p["last_achievement"] = achievement_map.get(pid)
        # Screen time last 7 days
        try:
            recent = activity_mapper.get_project_activity(pid, days=7)
            p["recent_minutes"] = round(sum(r["minutes"] for r in recent), 1)
            p["activity_days"] = len([r for r in recent if r["frame_count"] > 0])
        except Exception:
            p["recent_minutes"] = 0
            p["activity_days"] = 0
        # Last activity date (from entries or screen activity)
        try:
            with projects_db.get_db() as conn:
                row = conn.execute("""
                    SELECT MAX(d) as last_date FROM (
                        SELECT MAX(date) as d FROM project_entries WHERE project_id = ?
                        UNION ALL
                        SELECT MAX(date) as d FROM project_activity
                            WHERE project_id = ? AND frame_count > 0
                    )
                """, (pid, pid)).fetchone()
                p["last_activity_date"] = row["last_date"] if row else None
        except Exception:
            p["last_activity_date"] = None
    return jsonify({"projects": projects})


@app.route("/api/projects/<int:project_id>")
def api_project_detail(project_id: int):
    project = projects_db.get_project(project_id)
    if not project:
        return jsonify({"error": "Project not found"}), 404
    timeline = projects_db.get_project_timeline(project_id)
    return jsonify({"project": project, "timeline": timeline})


@app.route("/api/projects/<int:project_id>/status", methods=["POST"])
def api_update_project_status(project_id: int):
    data = request.json or {}
    new_status = data.get("status")
    if new_status not in ("active", "paused", "completed"):
        return jsonify({"error": "Invalid status"}), 400
    projects_db.update_project_status(project_id, new_status)
    return jsonify({"ok": True})


@app.route("/api/projects/sync", methods=["POST"])
def api_sync_projects():
    """Manual sync: screen time → screenpipe LLM → git → Slack + Linear."""
    from project_sync import sync_projects, sync_screenpipe_shipped
    from repo_scanner import sync_repos

    today = date_type.today()
    result: dict = {}

    # Step 1: Map today's screen activity to projects
    try:
        activity_mapper.map_activity_for_date(today)
        result["activity_mapped"] = True
    except Exception as exc:
        result["activity_error"] = str(exc)

    # Step 2: LLM extraction from screenpipe OCR
    try:
        sp_result = sync_screenpipe_shipped()
        result["screenpipe_entries"] = sp_result["entries_added"]
    except Exception as exc:
        result["screenpipe_error"] = str(exc)

    # Step 3: Git commit sync
    try:
        git_result = sync_repos(days_back=7)
        result["git_projects"] = git_result["projects_synced"]
        result["git_entries"] = git_result["entries_added"]
        result["git_commits"] = git_result["commits_total"]
    except Exception as exc:
        result["git_error"] = str(exc)

    # Step 4: Slack + Linear
    try:
        sync_result = sync_projects()
        result["projects_synced"] = sync_result["projects_synced"]
        result["entries_added"] = sync_result["entries_added"]
    except Exception as exc:
        result["sync_error"] = str(exc)

    return jsonify(result)


@app.route("/api/projects/day/<date_str>")
def api_projects_for_day(date_str: str):
    entries = projects_db.get_entries_for_date(date_str)
    return jsonify({"date": date_str, "entries": entries})


# ---------------------------------------------------------------------------
# Activity Mapping API
# ---------------------------------------------------------------------------
import activity_mapper

activity_mapper.init_activity_db()


@app.route("/api/activity/<date_str>")
def api_activity(date_str: str):
    """Return project-level time tracking for a date (from Screenpipe OCR)."""
    try:
        d = _parse_date(date_str)
    except ValueError:
        return jsonify({"error": "Invalid date format"}), 400

    try:
        result = activity_mapper.map_activity_for_date(d)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify(result)


@app.route("/api/projects/<int:project_id>/activity")
def api_project_activity(project_id: int):
    """Return daily activity history for a project (last 30 days)."""
    days = request.args.get("days", 30, type=int)
    activity = activity_mapper.get_project_activity(project_id, days=days)
    return jsonify({"project_id": project_id, "activity": activity})


@app.route("/api/projects/stale")
def api_stale_projects():
    """Return active projects with no recent activity."""
    days = request.args.get("days", 5, type=int)
    stale = activity_mapper.get_stale_projects(days_threshold=days)
    return jsonify({"stale": stale, "threshold_days": days})


@app.route("/api/overview")
def api_overview():
    """Aggregate entries + activity across the last N days for a weekly overview.

    Query params:
        days (int, default 7): Number of days to look back (newest-first).

    Days with zero entries AND zero activity are omitted from the response.
    """
    n_days = request.args.get("days", 7, type=int)
    if n_days < 1 or n_days > 365:
        return jsonify({"error": "days must be between 1 and 365"}), 400

    today = date_type.today()
    day_records: list[dict] = []

    # Accumulators for week_stats
    total_screen_minutes = 0.0
    all_project_ids: set[int] = set()
    total_achievements = 0
    total_blockers = 0

    for offset in range(n_days):
        d = today - timedelta(days=offset)
        date_str = d.isoformat()
        day_label = d.strftime("%a, %b %-d")

        try:
            raw_entries = projects_db.get_entries_for_date(date_str)
        except Exception:
            raw_entries = []

        # Merge entries by project_id: combine achievements/in_progress/blockers
        # and deduplicate across sources (linear, sync, screenpipe, etc.)
        merged: dict[int, dict] = {}
        for e in raw_entries:
            pid = e["project_id"]
            if pid not in merged:
                merged[pid] = {
                    "project_id": pid,
                    "project_name": e.get("project_name", ""),
                    "achievements": [],
                    "in_progress": [],
                    "blockers": [],
                }
            m = merged[pid]
            for a in (e.get("achievements") or []):
                if a not in m["achievements"]:
                    m["achievements"].append(a)
            for a in (e.get("in_progress") or []):
                if a not in m["in_progress"]:
                    m["in_progress"].append(a)
            for b in (e.get("blockers") or []):
                if b not in m["blockers"]:
                    m["blockers"].append(b)
        # Cap per-project items for overview readability
        _MAX_PER_PROJECT = 3
        for m in merged.values():
            m["achievements"] = m["achievements"][:_MAX_PER_PROJECT]
            m["in_progress"] = m["in_progress"][:_MAX_PER_PROJECT]
            m["blockers"] = m["blockers"][:_MAX_PER_PROJECT]
        entries = list(merged.values())

        try:
            activity = activity_mapper.get_activity_for_date(date_str)
        except Exception:
            activity = []

        # Skip days with nothing
        if not entries and not activity:
            continue

        # Strip app_breakdown from activity — not needed at overview level
        activity_slim = [
            {
                "project_id": row["project_id"],
                "project_name": row["project_name"],
                "minutes": row["minutes"],
            }
            for row in activity
        ]

        # Use total unique frames to avoid double-counting overlapping projects
        try:
            day_screen_minutes = activity_mapper.get_total_screen_minutes(d)
        except Exception:
            day_screen_minutes = round(sum(row["minutes"] for row in activity), 1)
        projects_touched = len(
            {e["project_id"] for e in entries} | {a["project_id"] for a in activity}
        )

        # Optional cached summary (lightweight — just the cached dict, not re-generating)
        try:
            summary = get_cached(d)
        except Exception:
            summary = None

        day_records.append({
            "date": date_str,
            "day_label": day_label,
            "entries": entries,
            "activity": activity_slim,
            "total_screen_minutes": day_screen_minutes,
            "projects_touched": projects_touched,
            "summary": summary,
        })

        # Accumulate week stats
        total_screen_minutes += day_screen_minutes
        for e in entries:
            all_project_ids.add(e["project_id"])
            total_achievements += len(e.get("achievements") or [])
            total_blockers += len(e.get("blockers") or [])
        for a in activity:
            all_project_ids.add(a["project_id"])

    return jsonify({
        "days": day_records,
        "week_stats": {
            "total_screen_minutes": round(total_screen_minutes, 1),
            "projects_active": len(all_project_ids),
            "total_achievements": total_achievements,
            "total_blockers": total_blockers,
        },
    })


@app.route("/api/portfolio")
def api_portfolio():
    """Return a consolidated portfolio view of all projects.

    Each project includes aggregated achievements, in-progress items, and
    blockers across ALL entries (not just the latest).  Items are deduplicated
    and the most recent entry date is surfaced for each project.
    """
    status_filter = request.args.get("status")  # optional
    projects = projects_db.get_all_projects(status=status_filter)

    portfolio: list[dict] = []
    totals = {"done": 0, "wip": 0, "blockers": 0, "completed": 0, "active": 0, "paused": 0}

    for p in projects:
        pid = p["id"]
        timeline = projects_db.get_project_timeline(pid)  # oldest-first

        # Aggregate and deduplicate across all entries
        done_items: list[str] = []
        wip_items: list[str] = []
        blocker_items: list[str] = []
        done_set: set[str] = set()
        wip_set: set[str] = set()
        blocker_set: set[str] = set()
        last_date = None

        for entry in timeline:
            d = entry.get("date")
            if d:
                last_date = d
            for a in (entry.get("achievements") or []):
                key = a.strip().lower()
                if key and key not in done_set:
                    done_set.add(key)
                    done_items.append(a)
            for a in (entry.get("in_progress") or []):
                key = a.strip().lower()
                if key and key not in wip_set and key not in done_set:
                    wip_set.add(key)
                    wip_items.append(a)
            for b in (entry.get("blockers") or []):
                key = b.strip().lower()
                if key and key not in blocker_set:
                    blocker_set.add(key)
                    blocker_items.append(b)

        total_items = len(done_items) + len(wip_items)
        done_pct = round((len(done_items) / total_items) * 100) if total_items > 0 else 0

        portfolio.append({
            "id": pid,
            "name": p["name"],
            "description": p.get("description"),
            "status": p["status"],
            "source": p.get("source"),
            "updated_at": p.get("updated_at"),
            "last_entry_date": last_date,
            "done": done_items,
            "wip": wip_items,
            "blockers": blocker_items,
            "done_count": len(done_items),
            "wip_count": len(wip_items),
            "blocker_count": len(blocker_items),
            "total_items": total_items,
            "done_pct": done_pct,
        })

        totals["done"] += len(done_items)
        totals["wip"] += len(wip_items)
        totals["blockers"] += len(blocker_items)
        totals[p["status"]] = totals.get(p["status"], 0) + 1

    # Sort: blockers first, then by progress %, then name
    portfolio.sort(key=lambda x: (
        -x["blocker_count"],
        x["done_pct"] if x["total_items"] > 0 else 999,
        x["name"].lower(),
    ))

    return jsonify({
        "projects": portfolio,
        "totals": totals,
    })


# ---------------------------------------------------------------------------
# Shipped / Activity APIs
# ---------------------------------------------------------------------------

from classifier import ROLE_COLORS
from urllib.parse import urlparse


@app.route("/api/shipped")
def api_shipped():
    """Return recent achievements grouped by date → project, with git commits collapsed.

    Response shape: { days: [{ date, projects: [{ items }] }], stats, tag_colors }

    Query params:
        days (int, default 14): Number of days to look back.
    """
    n_days = request.args.get("days", 14, type=int)
    if n_days < 1 or n_days > 365:
        return jsonify({"error": "days must be between 1 and 365"}), 400

    today = date_type.today()
    day_records: list[dict] = []

    # Build pid→tag lookup
    all_projects = projects_db.get_all_projects()
    pid_to_tag: dict[int, str | None] = {p["id"]: p.get("tag") for p in all_projects}

    # Tag ordering for sorting projects within a day
    tag_order = {t: i for i, t in enumerate(projects_db.TAG_ORDER)}
    max_tag_order = len(tag_order)

    # Stats accumulators
    done_texts_global: set[str] = set()
    projects_with_done: set[int] = set()
    total_blockers = 0

    # Global dedup tracker (case-insensitive text)
    seen_items: set[str] = set()

    for offset in range(n_days):
        d = today - timedelta(days=offset)
        date_str = d.isoformat()
        day_label = d.strftime("%a, %b %-d")

        try:
            raw_entries = projects_db.get_entries_for_date(date_str)
        except Exception:
            raw_entries = []

        # Collect items per project: pid → {items, name, tag}
        project_items: dict[int, dict] = {}

        def _ensure_project(pid: int, pname: str, ptag: str | None) -> dict:
            if pid not in project_items:
                project_items[pid] = {
                    "project_id": pid,
                    "project_name": pname,
                    "project_tag": ptag or pid_to_tag.get(pid),
                    "items": [],
                }
            return project_items[pid]

        # Separate git entries from human entries
        git_by_project: dict[int, dict] = {}
        human_entries: list[dict] = []

        for entry in raw_entries:
            if entry.get("source") == "git":
                pid = entry["project_id"]
                if pid not in git_by_project:
                    git_by_project[pid] = {
                        "name": entry.get("project_name", ""),
                        "tag": entry.get("project_tag"),
                        "subjects": [],
                    }
                for text in (entry.get("achievements") or []):
                    git_by_project[pid]["subjects"].append(text)
            else:
                human_entries.append(entry)

        # Collapse git commits per project
        from repo_scanner import get_git_summary
        for pid, gdata in git_by_project.items():
            subjects = gdata["subjects"]
            if not subjects:
                continue
            count = len(subjects)

            cached_summary = get_git_summary(pid, date_str)
            if cached_summary:
                display_text = cached_summary
            else:
                summaries = []
                for s in subjects[:5]:
                    clean = s
                    for prefix in ("feat: ", "fix: ", "chore: ", "refactor: ", "docs: ", "style: ", "test: "):
                        if clean.lower().startswith(prefix):
                            clean = clean[len(prefix):]
                            break
                    summaries.append(clean)
                display_text = ", ".join(summaries)
                if count > 5:
                    display_text += f", +{count - 5} more"

            pg = _ensure_project(pid, gdata["name"], gdata.get("tag"))
            pg["items"].append({
                "type": "done",
                "text": display_text,
                "source": "git",
                "commit_count": count,
            })
            done_texts_global.add(f"git-{pid}-{date_str}")
            projects_with_done.add(pid)

        # Process human entries
        for entry in human_entries:
            pid = entry["project_id"]
            pname = entry.get("project_name", "")
            ptag = entry.get("project_tag")
            eid = entry.get("id")
            esource = entry.get("source", "")

            for idx, text in enumerate(entry.get("achievements") or []):
                key = text.strip().lower()
                if key and key not in seen_items:
                    seen_items.add(key)
                    pg = _ensure_project(pid, pname, ptag)
                    pg["items"].append({
                        "type": "done", "text": text,
                        "entry_id": eid, "field": "achievements",
                        "item_index": idx, "source": esource,
                    })
                    done_texts_global.add(key)
                    projects_with_done.add(pid)

            for idx, text in enumerate(entry.get("blockers") or []):
                key = text.strip().lower()
                if key and key not in seen_items:
                    seen_items.add(key)
                    pg = _ensure_project(pid, pname, ptag)
                    pg["items"].append({
                        "type": "blocker", "text": text,
                        "entry_id": eid, "field": "blockers",
                        "item_index": idx, "source": esource,
                    })
                    total_blockers += 1

            wip_count_for_project = 0
            for idx, text in enumerate(entry.get("in_progress") or []):
                key = text.strip().lower()
                if key and key not in seen_items:
                    seen_items.add(key)
                    wip_count_for_project += 1
                    if wip_count_for_project <= 2:
                        pg = _ensure_project(pid, pname, ptag)
                        pg["items"].append({
                            "type": "wip", "text": text,
                            "entry_id": eid, "field": "in_progress",
                            "item_index": idx, "source": esource,
                        })

        if not project_items:
            continue

        # Sort items within each project: done → wip → blocker
        type_order = {"done": 0, "wip": 1, "blocker": 2}
        for pg in project_items.values():
            pg["items"].sort(key=lambda x: type_order.get(x["type"], 9))

        # Sort projects by tag order, then name
        sorted_projects = sorted(
            project_items.values(),
            key=lambda pg: (
                tag_order.get(pg.get("project_tag") or "", max_tag_order),
                (pg.get("project_name") or "").lower(),
            ),
        )

        # Screen time for this day
        day_screen_minutes = 0.0
        try:
            day_activity = activity_mapper.get_activity_for_date(date_str)
            day_screen_minutes = round(sum(a["minutes"] for a in day_activity), 1)
        except Exception:
            pass

        day_records.append({
            "date": date_str,
            "day_label": day_label,
            "projects": sorted_projects,
            "screen_minutes": day_screen_minutes,
        })

    return jsonify({
        "days": day_records,
        "stats": {
            "things_shipped": len(done_texts_global),
            "projects_moved": len(projects_with_done),
            "blockers": total_blockers,
        },
        "tag_colors": projects_db.TAG_COLORS,
    })


@app.route("/api/activity_summary")
def api_activity_summary():
    """Screenpipe activity dashboard: role breakdown, top apps, top URLs.

    Query params:
        days (int, default 7): lookback period.
        q    (str, optional):  OCR text search.
    """
    n_days = request.args.get("days", 7, type=int)
    if n_days < 1 or n_days > 60:
        return jsonify({"error": "days must be between 1 and 60"}), 400

    q = request.args.get("q", "").strip()
    today = date_type.today()

    # If searching, use Screenpipe REST search
    if q:
        try:
            results = search_content(q)
            return jsonify({"search_results": results, "query": q})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    # Aggregate across days using cached daily stats
    agg_app_minutes: dict[str, float] = {}
    agg_url_minutes: dict[str, float] = {}
    agg_role_minutes: dict[str, float] = {}
    day_summaries: list[dict] = []
    total_screen = 0.0

    for offset in range(n_days):
        d = today - timedelta(days=offset)
        stats = activity_mapper.get_or_compute_daily_stats(d)
        if not stats:
            continue

        total_screen += stats["total_minutes"]

        for r in stats["roles"]:
            agg_role_minutes[r["role"]] = agg_role_minutes.get(r["role"], 0) + r["minutes"]
        for a in stats["top_apps"]:
            agg_app_minutes[a["app"]] = agg_app_minutes.get(a["app"], 0) + a["minutes"]
        for u in stats["top_urls"]:
            agg_url_minutes[u["domain"]] = agg_url_minutes.get(u["domain"], 0) + u["minutes"]

        day_summaries.append({
            "date": d.isoformat(),
            "screen_minutes": stats["total_minutes"],
            "roles": stats["roles"],
        })

    role_list = [
        {"role": k, "minutes": round(v), "color": ROLE_COLORS.get(k, "#64748B")}
        for k, v in sorted(agg_role_minutes.items(), key=lambda x: x[1], reverse=True)
    ]
    top_apps = [
        {"app": a, "minutes": round(m, 1)}
        for a, m in sorted(agg_app_minutes.items(), key=lambda x: x[1], reverse=True)[:15]
    ]
    top_urls = [
        {"domain": d_name, "minutes": round(m, 1)}
        for d_name, m in sorted(agg_url_minutes.items(), key=lambda x: x[1], reverse=True)[:15]
    ]

    return jsonify({
        "days": n_days,
        "day_summaries": day_summaries,
        "roles": role_list,
        "top_apps": top_apps,
        "top_urls": top_urls,
        "total_screen_minutes": round(total_screen, 1),
    })


# ---------------------------------------------------------------------------
# Tag API
# ---------------------------------------------------------------------------

@app.route("/api/tags")
def api_tags():
    """Return tag definitions and colors."""
    return jsonify({
        "tags": projects_db.TAG_COLORS,
        "order": projects_db.TAG_ORDER,
    })


@app.route("/api/projects/<int:project_id>/tag", methods=["POST"])
def api_update_project_tag(project_id: int):
    """Set or clear a project's tag."""
    data = request.json or {}
    tag = data.get("tag")
    if tag and tag not in projects_db.TAG_COLORS:
        return jsonify({"error": f"Unknown tag: {tag}"}), 400
    projects_db.update_project_tag(project_id, tag)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Inline editing + project management APIs
# ---------------------------------------------------------------------------

_VALID_FIELDS = {"achievements", "in_progress", "blockers"}


@app.route("/api/shipped/edit-item", methods=["POST"])
def api_edit_item():
    """Edit an item's text within a project entry."""
    data = request.json or {}
    entry_id = data.get("entry_id")
    field = data.get("field")
    item_index = data.get("item_index")
    new_text = data.get("new_text", "").strip()
    if not all([entry_id is not None, field, item_index is not None, new_text]):
        return jsonify({"error": "Missing required fields"}), 400
    if field not in _VALID_FIELDS:
        return jsonify({"error": f"Invalid field: {field}"}), 400
    try:
        # Get original text for correction log
        with projects_db.get_db() as conn:
            row = conn.execute("SELECT project_id, date, " + field + " FROM project_entries WHERE id = ?", (entry_id,)).fetchone()
            if not row:
                return jsonify({"error": "Entry not found"}), 404
            orig_items = json.loads(row[field]) if row[field] else []
            orig_text = orig_items[item_index] if item_index < len(orig_items) else ""

        projects_db.update_entry_item(entry_id, field, item_index, new_text)
        projects_db.add_correction(
            date=row["date"], action="edit_text",
            original_project_id=row["project_id"],
            original_text=orig_text,
            corrected_text=new_text,
            source="inline_edit",
        )
        return jsonify({"ok": True})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/shipped/delete-item", methods=["POST"])
def api_delete_item():
    """Remove an item from a project entry."""
    data = request.json or {}
    entry_id = data.get("entry_id")
    field = data.get("field")
    item_index = data.get("item_index")
    if not all([entry_id is not None, field, item_index is not None]):
        return jsonify({"error": "Missing required fields"}), 400
    if field not in _VALID_FIELDS:
        return jsonify({"error": f"Invalid field: {field}"}), 400
    try:
        with projects_db.get_db() as conn:
            row = conn.execute("SELECT project_id, date, " + field + " FROM project_entries WHERE id = ?", (entry_id,)).fetchone()
            if not row:
                return jsonify({"error": "Entry not found"}), 404
            orig_items = json.loads(row[field]) if row[field] else []
            orig_text = orig_items[item_index] if item_index < len(orig_items) else ""

        projects_db.delete_entry_item(entry_id, field, item_index)
        projects_db.add_correction(
            date=row["date"], action="delete",
            original_project_id=row["project_id"],
            original_text=orig_text,
            source="inline_edit",
        )
        return jsonify({"ok": True})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/shipped/move-item", methods=["POST"])
def api_move_item():
    """Move an item from one project to another."""
    data = request.json or {}
    entry_id = data.get("entry_id")
    field = data.get("field")
    item_index = data.get("item_index")
    target_project_id = data.get("target_project_id")
    item_date = data.get("date")
    if not all([entry_id is not None, field, item_index is not None,
                target_project_id is not None, item_date]):
        return jsonify({"error": "Missing required fields"}), 400
    if field not in _VALID_FIELDS:
        return jsonify({"error": f"Invalid field: {field}"}), 400
    try:
        datetime.strptime(item_date, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "Invalid date format"}), 400
    try:
        with projects_db.get_db() as conn:
            row = conn.execute("SELECT project_id, " + field + " FROM project_entries WHERE id = ?", (entry_id,)).fetchone()
            if not row:
                return jsonify({"error": "Entry not found"}), 404
            orig_items = json.loads(row[field]) if row[field] else []
            orig_text = orig_items[item_index] if item_index < len(orig_items) else ""

        projects_db.move_entry_item(entry_id, field, item_index, target_project_id, item_date)
        projects_db.add_correction(
            date=item_date, action="reassign",
            original_project_id=row["project_id"],
            original_text=orig_text,
            corrected_project_id=target_project_id,
            source="inline_edit",
        )
        return jsonify({"ok": True})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/projects/create", methods=["POST"])
def api_create_project():
    """Create a new project."""
    data = request.json or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    tag = data.get("tag")
    if tag and tag not in projects_db.TAG_COLORS:
        return jsonify({"error": f"Unknown tag: {tag}"}), 400
    description = data.get("description")
    try:
        pid = projects_db.create_project(name, tag=tag, description=description)
        return jsonify({"ok": True, "id": pid})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/projects/<int:project_id>/rename", methods=["POST"])
def api_rename_project(project_id: int):
    """Rename a project."""
    data = request.json or {}
    new_name = (data.get("name") or "").strip()
    if not new_name:
        return jsonify({"error": "Name is required"}), 400
    try:
        old = projects_db.get_project(project_id)
        projects_db.rename_project(project_id, new_name)
        if old:
            projects_db.add_correction(
                date=date_type.today().isoformat(), action="rename_project",
                original_project_id=project_id,
                original_text=old["name"],
                corrected_text=new_name,
                source="project_mgmt",
            )
        return jsonify({"ok": True})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/projects/<int:project_id>", methods=["DELETE"])
def api_delete_project(project_id: int):
    """Delete a project and all its entries."""
    project = projects_db.get_project(project_id)
    if not project:
        return jsonify({"error": "Project not found"}), 404
    projects_db.delete_project(project_id)
    return jsonify({"ok": True})


@app.route("/api/corrections")
def api_corrections():
    """Return recent corrections for debugging/verification."""
    limit = request.args.get("limit", 50, type=int)
    corrections = projects_db.get_recent_corrections(limit=limit)
    return jsonify({"corrections": corrections})


if __name__ == "__main__":
    import os as _os

    app.run(
        host="127.0.0.1",
        port=5051,
        debug=_os.environ.get("FLASK_DEBUG", "1") == "1",
    )
