from __future__ import annotations

from datetime import date as date_type, datetime

from classifier import classify_frame
from screenpipe import build_activity_text, deduplicate_ocr, get_audio_transcripts, get_ocr_frames
from summarizer import summarize_day


_pending_jobs: dict[str, bool] = {}


def annotate_timeline_roles(timeline: list[dict], frames: list[dict]) -> None:
    """Add a role to each timeline session based on the dominant frame role."""
    ts_role = {
        frame.get("timestamp", ""): classify_frame(frame)
        for frame in frames
    }

    for session in timeline:
        start = session.get("start", "")
        end = session.get("end", "")
        role_counts: dict[str, int] = {}

        for timestamp, role in ts_role.items():
            if start <= timestamp <= end:
                role_counts[role] = role_counts.get(role, 0) + 1

        if role_counts:
            session["role"] = max(role_counts, key=role_counts.get)
        else:
            session["role"] = classify_frame({"app_name": session.get("app", "")})


def parse_date(date_str: str) -> date_type:
    return datetime.strptime(date_str, "%Y-%m-%d").date()


def truthy_arg(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def serialize_stats(stats: dict) -> dict:
    return {
        "total_frames": stats["total_frames"],
        "unique_apps": len(stats["unique_apps"]),
        "top_apps": [{"app": app, "count": count} for app, count in stats["top_apps"]],
        "active_hours": sorted(stats["active_hours"]),
        "first_activity": stats["first_activity"],
        "last_activity": stats["last_activity"],
    }


def auto_summarize(day: date_type, date_str: str) -> None:
    """Generate a structured summary in the background."""
    try:
        frames = get_ocr_frames(day)
        deduped = deduplicate_ocr(frames)
        audio = get_audio_transcripts(day)
        if deduped or audio:
            activity_text = build_activity_text(deduped, audio)
            summarize_day(activity_text, day)
    except Exception:
        pass
    finally:
        _pending_jobs.pop(date_str, None)
