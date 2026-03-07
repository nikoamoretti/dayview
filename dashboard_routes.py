from __future__ import annotations

from datetime import date as date_type, timedelta

from flask import Flask, jsonify, request

import activity_mapper
import projects_db
from classifier import ROLE_COLORS
from screenpipe import search_content
from summarizer import get_cached


def register_dashboard_routes(app: Flask) -> None:
    @app.route("/api/overview")
    def api_overview():
        n_days = request.args.get("days", 7, type=int)
        if n_days < 1 or n_days > 365:
            return jsonify({"error": "days must be between 1 and 365"}), 400

        today = date_type.today()
        day_records: list[dict] = []

        total_screen_minutes = 0.0
        all_project_ids: set[int] = set()
        total_achievements = 0
        total_blockers = 0

        for offset in range(n_days):
            day = today - timedelta(days=offset)
            date_str = day.isoformat()
            day_label = day.strftime("%a, %b %-d")

            try:
                raw_entries = projects_db.get_entries_for_date(date_str)
            except Exception:
                raw_entries = []

            merged: dict[int, dict] = {}
            for entry in raw_entries:
                pid = entry["project_id"]
                if pid not in merged:
                    merged[pid] = {
                        "project_id": pid,
                        "project_name": entry.get("project_name", ""),
                        "achievements": [],
                        "in_progress": [],
                        "blockers": [],
                    }

                merged_entry = merged[pid]
                for item in (entry.get("achievements") or []):
                    if item not in merged_entry["achievements"]:
                        merged_entry["achievements"].append(item)
                for item in (entry.get("in_progress") or []):
                    if item not in merged_entry["in_progress"]:
                        merged_entry["in_progress"].append(item)
                for item in (entry.get("blockers") or []):
                    if item not in merged_entry["blockers"]:
                        merged_entry["blockers"].append(item)

            max_per_project = 3
            for merged_entry in merged.values():
                merged_entry["achievements"] = merged_entry["achievements"][:max_per_project]
                merged_entry["in_progress"] = merged_entry["in_progress"][:max_per_project]
                merged_entry["blockers"] = merged_entry["blockers"][:max_per_project]
            entries = list(merged.values())

            try:
                activity = activity_mapper.get_activity_for_date(date_str)
            except Exception:
                activity = []

            if not entries and not activity:
                continue

            activity_slim = [
                {
                    "project_id": row["project_id"],
                    "project_name": row["project_name"],
                    "minutes": row["minutes"],
                }
                for row in activity
            ]

            try:
                day_screen_minutes = activity_mapper.get_total_screen_minutes(day)
            except Exception:
                day_screen_minutes = round(sum(row["minutes"] for row in activity), 1)

            projects_touched = len(
                {entry["project_id"] for entry in entries}
                | {row["project_id"] for row in activity}
            )

            try:
                summary = get_cached(day)
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

            total_screen_minutes += day_screen_minutes
            for entry in entries:
                all_project_ids.add(entry["project_id"])
                total_achievements += len(entry.get("achievements") or [])
                total_blockers += len(entry.get("blockers") or [])
            for row in activity:
                all_project_ids.add(row["project_id"])

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
        status_filter = request.args.get("status")
        projects = projects_db.get_all_projects(status=status_filter)

        portfolio: list[dict] = []
        totals = {"done": 0, "wip": 0, "blockers": 0, "completed": 0, "active": 0, "paused": 0}

        for project in projects:
            pid = project["id"]
            timeline = projects_db.get_project_timeline(pid)

            done_items: list[str] = []
            wip_items: list[str] = []
            blocker_items: list[str] = []
            done_set: set[str] = set()
            wip_set: set[str] = set()
            blocker_set: set[str] = set()
            last_date = None

            for entry in timeline:
                entry_date = entry.get("date")
                if entry_date:
                    last_date = entry_date
                for item in (entry.get("achievements") or []):
                    key = item.strip().lower()
                    if key and key not in done_set:
                        done_set.add(key)
                        done_items.append(item)
                for item in (entry.get("in_progress") or []):
                    key = item.strip().lower()
                    if key and key not in wip_set and key not in done_set:
                        wip_set.add(key)
                        wip_items.append(item)
                for item in (entry.get("blockers") or []):
                    key = item.strip().lower()
                    if key and key not in blocker_set:
                        blocker_set.add(key)
                        blocker_items.append(item)

            total_items = len(done_items) + len(wip_items)
            done_pct = round((len(done_items) / total_items) * 100) if total_items > 0 else 0

            portfolio.append({
                "id": pid,
                "name": project["name"],
                "description": project.get("description"),
                "status": project["status"],
                "source": project.get("source"),
                "updated_at": project.get("updated_at"),
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
            totals[project["status"]] = totals.get(project["status"], 0) + 1

        portfolio.sort(key=lambda item: (
            -item["blocker_count"],
            item["done_pct"] if item["total_items"] > 0 else 999,
            item["name"].lower(),
        ))

        return jsonify({"projects": portfolio, "totals": totals})

    @app.route("/api/shipped")
    def api_shipped():
        n_days = request.args.get("days", 14, type=int)
        if n_days < 1 or n_days > 365:
            return jsonify({"error": "days must be between 1 and 365"}), 400

        today = date_type.today()
        day_records: list[dict] = []

        all_projects = projects_db.get_all_projects()
        pid_to_tag: dict[int, str | None] = {project["id"]: project.get("tag") for project in all_projects}
        tag_order = {tag: index for index, tag in enumerate(projects_db.TAG_ORDER)}
        max_tag_order = len(tag_order)

        done_texts_global: set[str] = set()
        projects_with_done: set[int] = set()
        total_blockers = 0

        for offset in range(n_days):
            day = today - timedelta(days=offset)
            date_str = day.isoformat()
            day_label = day.strftime("%a, %b %-d")
            seen_items_for_day: set[str] = set()

            try:
                raw_entries = projects_db.get_entries_for_date(date_str)
            except Exception:
                raw_entries = []

            project_items: dict[int, dict] = {}

            def ensure_project(pid: int, pname: str, ptag: str | None) -> dict:
                if pid not in project_items:
                    project_items[pid] = {
                        "project_id": pid,
                        "project_name": pname,
                        "project_tag": ptag or pid_to_tag.get(pid),
                        "items": [],
                    }
                return project_items[pid]

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

            from repo_scanner import get_git_summary

            for pid, git_data in git_by_project.items():
                subjects = git_data["subjects"]
                if not subjects:
                    continue

                count = len(subjects)
                cached_summary = get_git_summary(pid, date_str)
                if cached_summary:
                    display_text = cached_summary
                else:
                    summaries = []
                    for subject in subjects[:5]:
                        clean = subject
                        for prefix in ("feat: ", "fix: ", "chore: ", "refactor: ", "docs: ", "style: ", "test: "):
                            if clean.lower().startswith(prefix):
                                clean = clean[len(prefix):]
                                break
                        summaries.append(clean)
                    display_text = ", ".join(summaries)
                    if count > 5:
                        display_text += f", +{count - 5} more"

                project_group = ensure_project(pid, git_data["name"], git_data.get("tag"))
                project_group["items"].append({
                    "type": "done",
                    "text": display_text,
                    "source": "git",
                    "commit_count": count,
                })
                done_texts_global.add(f"git-{pid}-{date_str}")
                projects_with_done.add(pid)

            for entry in human_entries:
                pid = entry["project_id"]
                pname = entry.get("project_name", "")
                ptag = entry.get("project_tag")
                entry_id = entry.get("id")
                source = entry.get("source", "")

                for idx, text in enumerate(entry.get("achievements") or []):
                    key = text.strip().lower()
                    if key and key not in seen_items_for_day:
                        seen_items_for_day.add(key)
                        project_group = ensure_project(pid, pname, ptag)
                        project_group["items"].append({
                            "type": "done",
                            "text": text,
                            "entry_id": entry_id,
                            "field": "achievements",
                            "item_index": idx,
                            "source": source,
                        })
                        done_texts_global.add(key)
                        projects_with_done.add(pid)

                for idx, text in enumerate(entry.get("blockers") or []):
                    key = text.strip().lower()
                    if key and key not in seen_items_for_day:
                        seen_items_for_day.add(key)
                        project_group = ensure_project(pid, pname, ptag)
                        project_group["items"].append({
                            "type": "blocker",
                            "text": text,
                            "entry_id": entry_id,
                            "field": "blockers",
                            "item_index": idx,
                            "source": source,
                        })
                        total_blockers += 1

                wip_count_for_project = 0
                for idx, text in enumerate(entry.get("in_progress") or []):
                    key = text.strip().lower()
                    if key and key not in seen_items_for_day:
                        seen_items_for_day.add(key)
                        wip_count_for_project += 1
                        if wip_count_for_project <= 2:
                            project_group = ensure_project(pid, pname, ptag)
                            project_group["items"].append({
                                "type": "wip",
                                "text": text,
                                "entry_id": entry_id,
                                "field": "in_progress",
                                "item_index": idx,
                                "source": source,
                            })

            if not project_items:
                continue

            type_order = {"done": 0, "wip": 1, "blocker": 2}
            for project_group in project_items.values():
                project_group["items"].sort(key=lambda item: type_order.get(item["type"], 9))

            sorted_projects = sorted(
                project_items.values(),
                key=lambda project_group: (
                    tag_order.get(project_group.get("project_tag") or "", max_tag_order),
                    (project_group.get("project_name") or "").lower(),
                ),
            )

            day_screen_minutes = 0.0
            try:
                day_activity = activity_mapper.get_activity_for_date(date_str)
                day_screen_minutes = round(sum(item["minutes"] for item in day_activity), 1)
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
        n_days = request.args.get("days", 7, type=int)
        if n_days < 1 or n_days > 60:
            return jsonify({"error": "days must be between 1 and 60"}), 400

        query = request.args.get("q", "").strip()
        today = date_type.today()

        if query:
            try:
                results = search_content(query)
                return jsonify({"search_results": results, "query": query})
            except Exception as exc:
                return jsonify({"error": str(exc)}), 500

        agg_app_minutes: dict[str, float] = {}
        agg_url_minutes: dict[str, float] = {}
        agg_role_minutes: dict[str, float] = {}
        day_summaries: list[dict] = []
        total_screen = 0.0

        for offset in range(n_days):
            day = today - timedelta(days=offset)
            stats = activity_mapper.get_or_compute_daily_stats(day)
            if not stats:
                continue

            total_screen += stats["total_minutes"]
            for role in stats["roles"]:
                agg_role_minutes[role["role"]] = agg_role_minutes.get(role["role"], 0) + role["minutes"]
            for app_item in stats["top_apps"]:
                agg_app_minutes[app_item["app"]] = agg_app_minutes.get(app_item["app"], 0) + app_item["minutes"]
            for url_item in stats["top_urls"]:
                agg_url_minutes[url_item["domain"]] = agg_url_minutes.get(url_item["domain"], 0) + url_item["minutes"]

            day_summaries.append({
                "date": day.isoformat(),
                "screen_minutes": stats["total_minutes"],
                "roles": stats["roles"],
            })

        role_list = [
            {"role": role, "minutes": round(minutes), "color": ROLE_COLORS.get(role, "#64748B")}
            for role, minutes in sorted(agg_role_minutes.items(), key=lambda item: item[1], reverse=True)
        ]
        top_apps = [
            {"app": app_name, "minutes": round(minutes, 1)}
            for app_name, minutes in sorted(agg_app_minutes.items(), key=lambda item: item[1], reverse=True)[:15]
        ]
        top_urls = [
            {"domain": domain, "minutes": round(minutes, 1)}
            for domain, minutes in sorted(agg_url_minutes.items(), key=lambda item: item[1], reverse=True)[:15]
        ]

        return jsonify({
            "days": n_days,
            "day_summaries": day_summaries,
            "roles": role_list,
            "top_apps": top_apps,
            "top_urls": top_urls,
            "total_screen_minutes": round(total_screen, 1),
        })
