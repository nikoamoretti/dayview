"""Scan git repos for commit history, summarize via LLM, and upsert into DayView.

Each repo maps to a DayView project via REPO_PROJECT_MAP. Raw commits are
stored as achievements (source='git') and then summarized into human-readable
one-liners cached in the git_summaries table.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections import defaultdict
from datetime import date, timedelta

# ---------------------------------------------------------------------------
# Repo → Project mapping
# ---------------------------------------------------------------------------

REPO_PROJECT_MAP: dict[str, str] = {
    "~/nico_repo/sales-dashboard/": "Sales Dashboard Development",
    "~/nico_repo/automation/cold-calling-stats/": "Outbound Dashboard",
    "~/nico_repo/rail-network-scanner/": "Lead Generation",
    "~/nico_repo/rail-dashboard/": "Revenue Hub",
    "~/Downloads/railcam-mapper/": "Rail Webcam Monitor & Car Tracker",
    "~/nico_repo/telegraph-kb/": "Telegraph Knowledge Base",
    "~/nico_repo/automation/dayview/": "DayView",
}

# Author filter — only Nico's commits
AUTHOR_EMAILS = {"nicoamoretti@gmail.com", "nico@telegraph.com"}
AUTHOR_NAME = "Nico Amoretti"


def _expand(path: str) -> str:
    return os.path.expanduser(path)


def _is_git_repo(path: str) -> bool:
    return os.path.isdir(os.path.join(path, ".git"))


def get_commits(
    repo_path: str,
    days_back: int = 30,
    author: str | None = None,
) -> list[dict]:
    """Get git commits from a repo, filtered by author and time range.

    Returns list of dicts with keys: hash, date, subject, body.
    """
    repo_path = _expand(repo_path)
    if not _is_git_repo(repo_path):
        return []

    since = (date.today() - timedelta(days=days_back)).isoformat()

    # Use %x00 as field separator (null byte) for reliable parsing
    fmt = "%H%x00%ad%x00%s%x00%b%x00"
    cmd = [
        "git", "-C", repo_path, "log",
        f"--since={since}",
        f"--format={fmt}",
        "--date=short",
    ]
    if author:
        cmd.append(f"--author={author}")

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    commits = []
    for block in result.stdout.strip().split("\x00\n"):
        parts = block.strip().split("\x00")
        if len(parts) < 3:
            continue
        hash_, date_str, subject = parts[0], parts[1], parts[2]
        body = parts[3].strip() if len(parts) > 3 else ""
        if hash_ and date_str and subject:
            commits.append({
                "hash": hash_[:8],
                "date": date_str,
                "subject": subject.strip(),
                "body": body,
            })

    return commits


def _clean_subject(subject: str) -> str:
    """Clean up a commit subject for display as an achievement.

    Strips Co-Authored-By lines and trailing whitespace.
    """
    lines = subject.split("\n")
    cleaned = [
        line for line in lines
        if not line.strip().startswith("Co-Authored-By:")
    ]
    return " ".join(cleaned).strip()


def scan_repos(days_back: int = 30) -> dict[str, list[dict]]:
    """Scan all configured repos and return commits grouped by project.

    Returns: {project_name: [{"date": ..., "commits": [subject, ...]}]}
    Each project's commits are grouped by date.
    """
    results: dict[str, dict[str, list[str]]] = {}

    for repo_path, project_name in REPO_PROJECT_MAP.items():
        expanded = _expand(repo_path)
        if not os.path.isdir(expanded):
            print(f"[repo_scanner] Skipping {repo_path} — not found")
            continue

        commits = get_commits(repo_path, days_back=days_back, author=AUTHOR_NAME)
        if not commits:
            print(f"[repo_scanner] {repo_path} — no commits in last {days_back} days")
            continue

        # Group by date
        by_date: dict[str, list[str]] = defaultdict(list)
        for c in commits:
            subject = _clean_subject(c["subject"])
            if subject:
                by_date[c["date"]].append(subject)

        results[project_name] = dict(by_date)
        total = sum(len(v) for v in by_date.values())
        print(f"[repo_scanner] {repo_path} → {project_name}: {total} commits across {len(by_date)} days")

    return results


def sync_repos(days_back: int = 30) -> dict[str, int]:
    """Scan repos and upsert commit entries into DayView DB.

    Returns stats: {projects_synced, entries_added, commits_total}.
    """
    import projects_db

    projects_db.init_db()

    repo_data = scan_repos(days_back=days_back)
    if not repo_data:
        print("[repo_scanner] No commits found across any repos")
        return {"projects_synced": 0, "entries_added": 0, "commits_total": 0}

    projects_synced = 0
    entries_added = 0
    commits_total = 0

    for project_name, dates in repo_data.items():
        # Ensure project exists
        pid = projects_db.upsert_project(
            name=project_name,
            status="active",
            source="git",
        )
        projects_synced += 1

        for date_str, subjects in sorted(dates.items()):
            # Deduplicate subjects for the same day
            seen: set[str] = set()
            unique_subjects: list[str] = []
            for s in subjects:
                key = s.lower()
                if key not in seen:
                    seen.add(key)
                    unique_subjects.append(s)

            commits_total += len(unique_subjects)

            try:
                projects_db.add_entry(
                    project_id=pid,
                    date=date_str,
                    achievements=unique_subjects,
                    in_progress=None,
                    blockers=None,
                    source="git",
                )
                entries_added += 1
            except Exception as exc:
                print(f"[repo_scanner] add_entry failed for {project_name} on {date_str}: {exc}")

    print(
        f"[repo_scanner] Done — {projects_synced} projects, "
        f"{entries_added} entries, {commits_total} commits"
    )

    # Generate human-readable summaries for new git entries
    summaries_added = summarize_git_entries(projects_db)

    return {
        "projects_synced": projects_synced,
        "entries_added": entries_added,
        "commits_total": commits_total,
        "summaries_added": summaries_added,
    }


# ---------------------------------------------------------------------------
# Git commit summarization via LLM
# ---------------------------------------------------------------------------

_SUMMARIZE_PROMPT = """\
Summarize these git commits into ONE concise sentence describing what was \
built or shipped. Write it as a business outcome, not a technical changelog. \
Speak as if telling a colleague what you accomplished.

Project: {project_name}
Date: {date}
Commits:
{commits}

Return ONLY the summary sentence, nothing else. No quotes, no prefix."""


def summarize_git_entries(pdb=None) -> int:
    """Generate human-readable summaries for git entries missing them.

    Checks git_summaries table for gaps and fills them via Claude CLI.
    Returns count of summaries generated.
    """
    if pdb is None:
        import projects_db as pdb

    from project_sync import _call_claude

    with pdb.get_db() as conn:
        # Find git entries without summaries
        rows = conn.execute("""
            SELECT pe.project_id, pe.date, pe.achievements,
                   p.name AS project_name
            FROM project_entries pe
            JOIN projects p ON p.id = pe.project_id
            LEFT JOIN git_summaries gs
                ON gs.project_id = pe.project_id AND gs.date = pe.date
            WHERE pe.source = 'git' AND gs.id IS NULL
            ORDER BY pe.date DESC
        """).fetchall()

    if not rows:
        print("[repo_scanner] All git entries already have summaries")
        return 0

    print(f"[repo_scanner] Generating summaries for {len(rows)} git entries...")
    added = 0

    # Batch all entries into a single LLM call to save time
    if len(rows) <= 20:
        batch_prompt = "Summarize each group of git commits into ONE concise sentence " \
            "describing what was built or shipped. Write as business outcomes, " \
            "not technical changelogs. Speak as if telling a colleague.\n\n" \
            "Return ONLY a JSON array with objects {\"project\": ..., \"date\": ..., \"summary\": ...}\n\n"

        for row in rows:
            achievements = json.loads(row["achievements"]) if row["achievements"] else []
            if not achievements:
                continue
            commits_text = "\n".join(f"- {a}" for a in achievements[:10])
            batch_prompt += f"---\nProject: {row['project_name']}\nDate: {row['date']}\n" \
                f"Commits:\n{commits_text}\n\n"

        try:
            raw = _call_claude("", batch_prompt)
            raw = raw.strip()
            if raw.startswith("```"):
                lines = raw.split("\n")
                end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
                raw = "\n".join(lines[1:end]).strip()
            summaries = json.loads(raw)
        except Exception as exc:
            print(f"[repo_scanner] Batch summarization failed: {exc}")
            # Fall back to individual calls
            summaries = []

        if summaries:
            with pdb.get_db() as conn:
                for s in summaries:
                    proj_name = s.get("project", "")
                    date_str = s.get("date", "")
                    summary = s.get("summary", "")
                    if not summary or not date_str:
                        continue
                    # Find project_id
                    row = conn.execute(
                        "SELECT id FROM projects WHERE name = ? COLLATE NOCASE",
                        (proj_name,),
                    ).fetchone()
                    if not row:
                        continue
                    pid = row["id"]
                    # Count commits
                    entry = conn.execute(
                        "SELECT achievements FROM project_entries WHERE project_id=? AND date=? AND source='git'",
                        (pid, date_str),
                    ).fetchone()
                    count = len(json.loads(entry["achievements"])) if entry and entry["achievements"] else 0
                    try:
                        conn.execute(
                            """INSERT INTO git_summaries (project_id, date, summary, commit_count)
                            VALUES (?, ?, ?, ?)
                            ON CONFLICT(project_id, date) DO UPDATE SET
                                summary = excluded.summary,
                                commit_count = excluded.commit_count""",
                            (pid, date_str, summary, count),
                        )
                        added += 1
                    except Exception as exc:
                        print(f"[repo_scanner] Summary insert failed: {exc}")
            print(f"[repo_scanner] Generated {added} summaries (batch)")
            return added

    # Individual fallback (or >20 entries)
    for row in rows:
        achievements = json.loads(row["achievements"]) if row["achievements"] else []
        if not achievements:
            continue
        commits_text = "\n".join(f"- {a}" for a in achievements[:10])
        prompt = _SUMMARIZE_PROMPT.format(
            project_name=row["project_name"],
            date=row["date"],
            commits=commits_text,
        )
        try:
            summary = _call_claude("", prompt)
            summary = summary.strip().strip('"').strip("'")
            if summary:
                with pdb.get_db() as conn:
                    conn.execute(
                        """INSERT INTO git_summaries (project_id, date, summary, commit_count)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(project_id, date) DO UPDATE SET
                            summary = excluded.summary,
                            commit_count = excluded.commit_count""",
                        (row["project_id"], row["date"], summary, len(achievements)),
                    )
                    added += 1
        except Exception as exc:
            print(f"[repo_scanner] Summary failed for {row['project_name']} {row['date']}: {exc}")

    print(f"[repo_scanner] Generated {added} summaries")
    return added


def get_git_summary(project_id: int, date_str: str) -> str | None:
    """Return cached git summary for a project/date, or None."""
    import projects_db
    with projects_db.get_db() as conn:
        row = conn.execute(
            "SELECT summary FROM git_summaries WHERE project_id = ? AND date = ?",
            (project_id, date_str),
        ).fetchone()
    return row["summary"] if row else None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    days = 30
    if len(sys.argv) > 1:
        try:
            days = int(sys.argv[1])
        except ValueError:
            print(f"Usage: python3 repo_scanner.py [days_back]")
            sys.exit(1)

    result = sync_repos(days_back=days)
    print(result)
