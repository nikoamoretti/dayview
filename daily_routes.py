from __future__ import annotations

import threading
from datetime import date as date_type

from flask import Flask, jsonify, render_template, request

from classifier import compute_focus_time, compute_role_minutes
from meetings import detect_meetings
from route_helpers import (
    _pending_jobs,
    annotate_timeline_roles,
    auto_summarize,
    parse_date,
    serialize_stats,
)
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


def register_daily_routes(app: Flask) -> None:
    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/api/days")
    def api_days():
        days = [day for day in list_days_with_data() if day is not None]
        return jsonify({"days": days})

    @app.route("/api/day/<date_str>")
    def api_day(date_str: str):
        try:
            day = parse_date(date_str)
        except ValueError:
            return jsonify({"error": "Invalid date format"}), 400

        try:
            frames = get_ocr_frames(day)
            deduped = deduplicate_ocr(frames)
            timeline = build_timeline(deduped)
            annotate_timeline_roles(timeline, deduped)
            stats = get_activity_stats(deduped)
            audio = get_audio_transcripts(day)
            cached = get_cached(day)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

        generating = False
        if cached is None and (deduped or audio) and date_str not in _pending_jobs:
            _pending_jobs[date_str] = True
            thread = threading.Thread(
                target=auto_summarize,
                args=(day, date_str),
                daemon=True,
            )
            thread.start()
            generating = True
        elif date_str in _pending_jobs:
            generating = True

        roles = compute_role_minutes(deduped)
        focus_minutes = compute_focus_time(deduped)

        return jsonify({
            "date": date_str,
            "timeline": timeline,
            "stats": serialize_stats(stats),
            "audio_count": len(audio),
            "content": cached,
            "generating": generating,
            "roles": roles,
            "focus_minutes": focus_minutes,
        })

    @app.route("/api/roles/<date_str>")
    def api_roles(date_str: str):
        try:
            day = parse_date(date_str)
        except ValueError:
            return jsonify({"error": "Invalid date format"}), 400

        try:
            frames = get_ocr_frames(day)
            deduped = deduplicate_ocr(frames)
            roles = compute_role_minutes(deduped)
            focus = compute_focus_time(deduped)
            total = sum(role["minutes"] for role in roles)
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
            day = parse_date(date_str)
        except ValueError:
            return jsonify({"error": "Invalid date format"}), 400

        try:
            frames = get_ocr_frames(day)
            deduped = deduplicate_ocr(frames)
            audio = get_audio_transcripts(day)
            meetings = detect_meetings(deduped, audio)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

        return jsonify({"meetings": meetings})

    @app.route("/api/search")
    def api_search():
        query = request.args.get("q", "")
        if not query:
            return jsonify({"error": "Missing query parameter 'q'"}), 400

        date_str = request.args.get("date")
        date_obj: date_type | None = None
        if date_str:
            try:
                date_obj = parse_date(date_str)
            except ValueError:
                return jsonify({"error": "Invalid date format"}), 400

        try:
            results = search_content(query, date_obj)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

        return jsonify({"results": results})

    @app.route("/api/summarize/<date_str>", methods=["POST"])
    def api_summarize(date_str: str):
        try:
            day = parse_date(date_str)
        except ValueError:
            return jsonify({"error": "Invalid date format"}), 400

        force = request.json.get("force", False) if request.json else False

        try:
            frames = get_ocr_frames(day)
            deduped = deduplicate_ocr(frames)
            audio = get_audio_transcripts(day)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

        if not deduped and not audio:
            return jsonify({"error": "No data found for this date"}), 404

        try:
            activity_text = build_activity_text(deduped, audio)
            content = summarize_day(activity_text, day, force=force)
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
