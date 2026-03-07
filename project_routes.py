from __future__ import annotations

import json
from datetime import date as date_type, datetime

from flask import Flask, jsonify, request

import activity_mapper
import projects_db
from route_helpers import parse_date, truthy_arg


_VALID_FIELDS = {"achievements", "in_progress", "blockers"}


def register_project_routes(app: Flask) -> None:
    @app.route("/api/projects")
    def api_projects():
        status = request.args.get("status")
        projects = projects_db.get_all_projects(status=status)

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
                    if pid in achievement_map:
                        continue
                    try:
                        items = json.loads(row["achievements"])
                    except (json.JSONDecodeError, TypeError):
                        items = []
                    if items:
                        achievement_map[pid] = items[0]
        except Exception:
            pass

        for project in projects:
            pid = project["id"]
            project["last_achievement"] = achievement_map.get(pid)

            try:
                recent = activity_mapper.get_project_activity(pid, days=7)
                project["recent_minutes"] = round(sum(row["minutes"] for row in recent), 1)
                project["activity_days"] = len([row for row in recent if row["frame_count"] > 0])
            except Exception:
                project["recent_minutes"] = 0
                project["activity_days"] = 0

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
                    project["last_activity_date"] = row["last_date"] if row else None
            except Exception:
                project["last_activity_date"] = None

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
        from project_sync import sync_projects, sync_screenpipe_shipped
        from repo_scanner import sync_repos

        today = date_type.today()
        result: dict = {}

        try:
            activity_mapper.map_activity_for_date(today)
            result["activity_mapped"] = True
        except Exception as exc:
            result["activity_error"] = str(exc)

        try:
            screenpipe_result = sync_screenpipe_shipped()
            result["screenpipe_entries"] = screenpipe_result["entries_added"]
        except Exception as exc:
            result["screenpipe_error"] = str(exc)

        try:
            git_result = sync_repos(days_back=7)
            result["git_projects"] = git_result["projects_synced"]
            result["git_entries"] = git_result["entries_added"]
            result["git_commits"] = git_result["commits_total"]
        except Exception as exc:
            result["git_error"] = str(exc)

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

    @app.route("/api/activity/<date_str>")
    def api_activity(date_str: str):
        try:
            day = parse_date(date_str)
        except ValueError:
            return jsonify({"error": "Invalid date format"}), 400

        refresh = truthy_arg(request.args.get("refresh"))

        try:
            if not refresh and day < date_type.today() and activity_mapper.has_activity_for_date(date_str):
                result = {
                    "date": date_str,
                    "projects": activity_mapper.get_activity_for_date(date_str),
                    "total_frames": None,
                    "cached": True,
                }
            else:
                result = activity_mapper.map_activity_for_date(day)
                result["cached"] = False
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

        return jsonify(result)

    @app.route("/api/projects/<int:project_id>/activity")
    def api_project_activity(project_id: int):
        days = request.args.get("days", 30, type=int)
        activity = activity_mapper.get_project_activity(project_id, days=days)
        return jsonify({"project_id": project_id, "activity": activity})

    @app.route("/api/projects/stale")
    def api_stale_projects():
        days = request.args.get("days", 5, type=int)
        stale = activity_mapper.get_stale_projects(days_threshold=days)
        return jsonify({"stale": stale, "threshold_days": days})

    @app.route("/api/tags")
    def api_tags():
        return jsonify({
            "tags": projects_db.TAG_COLORS,
            "order": projects_db.TAG_ORDER,
        })

    @app.route("/api/projects/<int:project_id>/tag", methods=["POST"])
    def api_update_project_tag(project_id: int):
        data = request.json or {}
        tag = data.get("tag")
        if tag and tag not in projects_db.TAG_COLORS:
            return jsonify({"error": f"Unknown tag: {tag}"}), 400
        projects_db.update_project_tag(project_id, tag)
        return jsonify({"ok": True})

    @app.route("/api/shipped/edit-item", methods=["POST"])
    def api_edit_item():
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
            with projects_db.get_db() as conn:
                row = conn.execute(
                    "SELECT project_id, date, " + field + " FROM project_entries WHERE id = ?",
                    (entry_id,),
                ).fetchone()
                if not row:
                    return jsonify({"error": "Entry not found"}), 404
                orig_items = json.loads(row[field]) if row[field] else []
                orig_text = orig_items[item_index] if item_index < len(orig_items) else ""

            projects_db.update_entry_item(entry_id, field, item_index, new_text)
            projects_db.add_correction(
                date=row["date"],
                action="edit_text",
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
                row = conn.execute(
                    "SELECT project_id, date, " + field + " FROM project_entries WHERE id = ?",
                    (entry_id,),
                ).fetchone()
                if not row:
                    return jsonify({"error": "Entry not found"}), 404
                orig_items = json.loads(row[field]) if row[field] else []
                orig_text = orig_items[item_index] if item_index < len(orig_items) else ""

            projects_db.delete_entry_item(entry_id, field, item_index)
            projects_db.add_correction(
                date=row["date"],
                action="delete",
                original_project_id=row["project_id"],
                original_text=orig_text,
                source="inline_edit",
            )
            return jsonify({"ok": True})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.route("/api/shipped/move-item", methods=["POST"])
    def api_move_item():
        data = request.json or {}
        entry_id = data.get("entry_id")
        field = data.get("field")
        item_index = data.get("item_index")
        target_project_id = data.get("target_project_id")
        item_date = data.get("date")

        if not all([
            entry_id is not None,
            field,
            item_index is not None,
            target_project_id is not None,
            item_date,
        ]):
            return jsonify({"error": "Missing required fields"}), 400
        if field not in _VALID_FIELDS:
            return jsonify({"error": f"Invalid field: {field}"}), 400

        try:
            datetime.strptime(item_date, "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "Invalid date format"}), 400

        try:
            with projects_db.get_db() as conn:
                row = conn.execute(
                    "SELECT project_id, " + field + " FROM project_entries WHERE id = ?",
                    (entry_id,),
                ).fetchone()
                if not row:
                    return jsonify({"error": "Entry not found"}), 404
                orig_items = json.loads(row[field]) if row[field] else []
                orig_text = orig_items[item_index] if item_index < len(orig_items) else ""

            projects_db.move_entry_item(entry_id, field, item_index, target_project_id, item_date)
            projects_db.add_correction(
                date=item_date,
                action="reassign",
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
        data = request.json or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "Name is required"}), 400

        tag = data.get("tag")
        if tag and tag not in projects_db.TAG_COLORS:
            return jsonify({"error": f"Unknown tag: {tag}"}), 400

        description = data.get("description")
        try:
            project_id = projects_db.create_project(name, tag=tag, description=description)
            return jsonify({"ok": True, "id": project_id})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.route("/api/projects/<int:project_id>/rename", methods=["POST"])
    def api_rename_project(project_id: int):
        data = request.json or {}
        new_name = (data.get("name") or "").strip()
        if not new_name:
            return jsonify({"error": "Name is required"}), 400

        try:
            old = projects_db.get_project(project_id)
            projects_db.rename_project(project_id, new_name)
            if old:
                projects_db.add_correction(
                    date=date_type.today().isoformat(),
                    action="rename_project",
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
        project = projects_db.get_project(project_id)
        if not project:
            return jsonify({"error": "Project not found"}), 404

        projects_db.delete_project(project_id)
        return jsonify({"ok": True})

    @app.route("/api/corrections")
    def api_corrections():
        limit = request.args.get("limit", 50, type=int)
        corrections = projects_db.get_recent_corrections(limit=limit)
        return jsonify({"corrections": corrections})
