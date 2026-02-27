from __future__ import annotations

import json
import os
import subprocess
from datetime import date, datetime, timedelta, timezone
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DIR = os.path.dirname(os.path.abspath(__file__))

GOOGLE_DOC_ID = "1VvgGqDYfYiU7XGS9TvmfH19LWanY2eGVmdMQc-FT1ak"
GOOGLE_TOKEN_PATH = os.path.expanduser(
    "~/nico_repo/automation/daily-digest/token.json"
)
SLACK_CHANNEL_ID = "C09J9DY57U3"
NICO_USER_ID = "U09GY77GEQP"

LINEAR_API_URL = "https://api.linear.app/graphql"
NICO_LINEAR_ID = "99825a39-b612-4ebb-ab17-c07164075120"

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
GEMINI_MODEL = "gemini-2.0-flash"

DAILY_DIGEST_ENV = os.path.expanduser(
    "~/nico_repo/automation/daily-digest/.env"
)


# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------


def _load_env_file(path: str) -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file into a dict."""
    result: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                result[key.strip()] = value.strip()
    except OSError:
        pass
    return result


def _get_slack_token() -> str:
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if token:
        return token
    env = _load_env_file(DAILY_DIGEST_ENV)
    return env.get("SLACK_BOT_TOKEN", "")


def _get_gemini_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "")
    if key:
        return key
    env = _load_env_file(DAILY_DIGEST_ENV)
    return env.get("GEMINI_API_KEY", "")


def _get_linear_key() -> str:
    key = os.environ.get("LINEAR_API_KEY", "")
    if key:
        return key
    env = _load_env_file(DAILY_DIGEST_ENV)
    return env.get("LINEAR_API_KEY", "")


# ---------------------------------------------------------------------------
# Claude CLI helper (uses user's subscription, no API key needed)
# ---------------------------------------------------------------------------

def _call_claude(system_prompt: str, user_prompt: str, model: str = "haiku") -> str:
    """Call Claude via the CLI in print mode. Returns raw text response."""
    full_prompt = f"{system_prompt}\n\n---\n\n{user_prompt}"

    env = os.environ.copy()
    # Unset nesting guards so claude can run from within a session
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    env.pop("CLAUDECODE", None)

    result = subprocess.run(
        [
            "claude", "-p",
            "--model", model,
            "--tools", "",          # no tools needed for extraction
            "--output-format", "text",
            "--max-turns", "1",
            "--no-session-persistence",
        ],
        input=full_prompt,
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )

    if result.returncode != 0:
        err = result.stderr.strip()[:300]
        raise RuntimeError(f"claude CLI failed (rc={result.returncode}): {err}")

    return result.stdout.strip()


# ---------------------------------------------------------------------------
# fetch_google_doc
# ---------------------------------------------------------------------------


def _load_token_file() -> dict[str, Any]:
    with open(GOOGLE_TOKEN_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_token_file(data: dict[str, Any]) -> None:
    with open(GOOGLE_TOKEN_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _token_is_expired(token_data: dict[str, Any]) -> bool:
    expiry_str: str | None = token_data.get("expiry")
    if not expiry_str:
        return True
    try:
        expiry = datetime.fromisoformat(expiry_str)
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        # Treat as expired 60 s before actual expiry to avoid races.
        return datetime.now(timezone.utc) >= expiry - timedelta(seconds=60)
    except ValueError:
        return True


def _refresh_access_token(token_data: dict[str, Any]) -> dict[str, Any]:
    """POST to token_uri to exchange refresh_token for a new access_token."""
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": token_data["refresh_token"],
        "client_id": token_data["client_id"],
        "client_secret": token_data["client_secret"],
    }
    resp = requests.post(token_data["token_uri"], data=payload, timeout=15)
    resp.raise_for_status()
    refreshed = resp.json()

    token_data["access_token"] = refreshed["access_token"]
    expires_in: int = refreshed.get("expires_in", 3600)
    new_expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    token_data["expiry"] = new_expiry.isoformat()

    _save_token_file(token_data)
    print(f"[project_sync] Google token refreshed, expires {token_data['expiry']}")
    return token_data


def _get_valid_access_token() -> str:
    token_data = _load_token_file()
    if _token_is_expired(token_data):
        token_data = _refresh_access_token(token_data)
    return token_data["access_token"]


def _extract_doc_text(doc: dict[str, Any]) -> str:
    """Walk body.content and concatenate all textRun.content strings."""
    parts: list[str] = []
    for block in doc.get("body", {}).get("content", []):
        paragraph = block.get("paragraph", {})
        for element in paragraph.get("elements", []):
            text_run = element.get("textRun", {})
            content = text_run.get("content", "")
            if content:
                parts.append(content)
    return "".join(parts)


def fetch_google_doc() -> str:
    """Fetch the Google Doc and return its plain text content."""
    print(f"[project_sync] Fetching Google Doc {GOOGLE_DOC_ID}...")
    access_token = _get_valid_access_token()
    url = f"https://docs.googleapis.com/v1/documents/{GOOGLE_DOC_ID}"
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    resp.raise_for_status()
    doc = resp.json()
    text = _extract_doc_text(doc)
    print(f"[project_sync] Google Doc fetched — {len(text):,} chars")
    return text


# ---------------------------------------------------------------------------
# fetch_slack_updates
# ---------------------------------------------------------------------------


SLACK_CACHE_PATH = os.path.join(_DIR, "slack_cache.txt")


def fetch_slack_updates(days_back: int = 14) -> str:
    """Return Slack messages from the group DM.

    Tries the bot token API first; falls back to a cached file written by
    an external process (e.g. Slack MCP during a Claude Code session).
    """
    # Try bot token API first
    token = _get_slack_token()
    if token:
        print(
            f"[project_sync] Fetching Slack messages "
            f"(last {days_back} days) from channel {SLACK_CHANNEL_ID}..."
        )
        oldest = (datetime.now(timezone.utc) - timedelta(days=days_back)).timestamp()
        try:
            resp = requests.get(
                "https://slack.com/api/conversations.history",
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "channel": SLACK_CHANNEL_ID,
                    "oldest": str(oldest),
                    "limit": 100,
                },
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("ok"):
                messages = data.get("messages", [])
                nico_messages = [m for m in messages if m.get("user") == NICO_USER_ID]
                print(
                    f"[project_sync] {len(nico_messages)} messages from Nico "
                    f"(out of {len(messages)} total)"
                )
                lines: list[str] = []
                for msg in reversed(nico_messages):
                    ts = float(msg.get("ts", 0))
                    dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                    text = msg.get("text", "").strip()
                    if text:
                        lines.append(f"[{dt}] {text}")
                return "\n".join(lines)
            else:
                print(f"[project_sync] Slack API error: {data.get('error')} — trying cache")
        except Exception as exc:
            print(f"[project_sync] Slack API failed: {exc} — trying cache")

    # Fallback: read cached Slack data, filtered by days_back
    if os.path.exists(SLACK_CACHE_PATH):
        with open(SLACK_CACHE_PATH, "r", encoding="utf-8") as f:
            text = f.read()
        if text.strip():
            # Filter cached lines by date when days_back is small
            if days_back <= 7:
                cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back))
                cutoff_str = cutoff.strftime("%Y-%m-%d")
                filtered = [
                    line for line in text.strip().split("\n")
                    if line.strip() and line[:1] == "[" and line[1:11] >= cutoff_str
                ]
                text = "\n".join(filtered)
                print(f"[project_sync] Loaded Slack cache, filtered to >={cutoff_str} ({len(filtered)} messages)")
            else:
                print(f"[project_sync] Loaded Slack cache ({len(text):,} chars)")
            return text

    raise RuntimeError("Slack API unavailable and no cache found")


# ---------------------------------------------------------------------------
# fetch_linear_issues
# ---------------------------------------------------------------------------

_LINEAR_PROJECTS_QUERY = """\
{
  projects(first: 50) {
    nodes {
      id name description state startDate targetDate
      lead { id name }
      members { nodes { id name } }
      teams { nodes { name } }
    }
  }
}"""

_LINEAR_ISSUES_QUERY = """\
{
  viewer {
    assignedIssues(
      filter: { state: { type: { in: ["started", "unstarted"] } } }
      first: 50
    ) {
      nodes {
        identifier title
        state { name type }
        priorityLabel
        project { name }
        dueDate
        labels { nodes { name } }
      }
    }
  }
}"""


def _linear_graphql(query: str, token: str) -> dict[str, Any]:
    resp = requests.post(
        LINEAR_API_URL,
        headers={
            "Content-Type": "application/json",
            "Authorization": token,
        },
        json={"query": query},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"Linear GraphQL errors: {data['errors']}")
    return data["data"]


def _is_nicos_project(project: dict[str, Any], issue_project_names: set[str]) -> bool:
    """Return True if Nico leads, is a member of, or has issues on this project."""
    lead = project.get("lead") or {}
    if lead.get("id") == NICO_LINEAR_ID:
        return True
    members = project.get("members", {}).get("nodes", [])
    if any(m.get("id") == NICO_LINEAR_ID for m in members):
        return True
    if project["name"] in issue_project_names:
        return True
    return False


def fetch_linear_data() -> dict[str, Any]:
    """Fetch Nico's Linear projects and assigned issues.

    Returns dict with 'projects' (list) and 'issues' (list).
    Only includes projects Nico leads, is a member of, or has assigned issues on.
    """
    print("[project_sync] Fetching Linear data...")
    token = _get_linear_key()
    if not token:
        raise RuntimeError("LINEAR_API_KEY not set and not found in .env")

    proj_data = _linear_graphql(_LINEAR_PROJECTS_QUERY, token)
    issue_data = _linear_graphql(_LINEAR_ISSUES_QUERY, token)

    all_projects = proj_data.get("projects", {}).get("nodes", [])
    issues = issue_data.get("viewer", {}).get("assignedIssues", {}).get("nodes", [])

    # Projects that have issues assigned to Nico
    issue_proj_names = {
        i["project"]["name"] for i in issues
        if i.get("project")
    }

    projects = [p for p in all_projects if _is_nicos_project(p, issue_proj_names)]

    print(
        f"[project_sync] Linear: {len(projects)} projects (of {len(all_projects)} total), "
        f"{len(issues)} assigned issues"
    )
    return {"projects": projects, "issues": issues}


def _format_linear_for_llm(linear_data: dict[str, Any]) -> str:
    """Format Linear projects and issues into text for the LLM prompt."""
    lines: list[str] = []

    # Active/started projects
    active = [p for p in linear_data["projects"] if p["state"] in ("started", "planned")]
    if active:
        lines.append("### Active Linear Projects")
        for p in active:
            lead = p.get("lead", {})
            lead_name = lead["name"] if lead else "unassigned"
            teams = ", ".join(t["name"] for t in p.get("teams", {}).get("nodes", []))
            lines.append(f"- {p['name']} [{p['state']}] (lead: {lead_name}, teams: {teams})")
            if p.get("description"):
                lines.append(f"  {p['description'][:200]}")

    # Assigned issues
    issues = linear_data["issues"]
    if issues:
        lines.append("\n### Issues Assigned to Nico")
        for i in issues:
            proj = i.get("project", {})
            proj_name = proj["name"] if proj else "no project"
            due = f" due:{i['dueDate']}" if i.get("dueDate") else ""
            lines.append(
                f"- [{i['identifier']}] {i['title']} "
                f"({i['state']['name']}, {i['priorityLabel']}, proj: {proj_name}{due})"
            )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# extract_projects
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are analyzing TODAY's work updates from Nico Amoretti, a sales/GTM person \
at Telegraph (rail logistics). Extract ONLY what Nico did or mentioned TODAY — \
not historical achievements, not things done last week or last month.

## CRITICAL: Only today's work

You will receive Slack messages from TODAY ONLY. Extract achievements, \
in-progress work, and blockers ONLY from these messages. If a message \
references something done in the past ("we shipped that last week"), \
do NOT include it as a today achievement — it already happened.

An "achievement" means Nico completed/delivered/shipped something TODAY. \
"In progress" means work Nico mentioned working on today but hasn't finished.

## Existing Projects (reuse these names)

You will be given a list of existing project names. You MUST:
1. Use the EXACT existing name when a project matches.
2. Only create a NEW project if the work truly doesn't fit any existing project.
3. NEVER create a variant like "Q1 Outbound Campaign" when "Q1 Outbound" exists.

## Rules

- Only include projects that have TODAY's activity. Skip projects with nothing new.
- Max 25 projects. Group related items under one project.
- Ignore engineering/product projects led by others.
- Individual tasks go in achievements/in_progress/blockers, NOT as separate projects.
- Use three statuses only: "active", "completed", "paused".

Return ONLY valid JSON — an array of project objects with no markdown fences."""

_USER_PROMPT_TEMPLATE = """\
## Existing projects (use these EXACT names when applicable)
{existing_projects}

---

## Today's Slack messages
{slack_text}

---

Extract ONLY what Nico worked on TODAY based on the Slack messages above. \
Each item should be something concrete and specific from today's messages. \
Skip projects with no today activity.

IMPORTANT: Reuse project names from the "Existing projects" list above.

Return a JSON array (only projects with today's activity):
[
  {{
    "name": "Existing project name",
    "description": "One-line description",
    "status": "active|completed|paused",
    "achievements": ["Things completed/shipped/delivered TODAY"],
    "in_progress": ["Work mentioned as ongoing TODAY"],
    "blockers": ["Blockers mentioned TODAY"]
  }}
]"""


def _strip_code_fences(raw: str) -> str:
    """Remove surrounding ```json ... ``` or ``` ... ``` if present."""
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        raw = "\n".join(lines[1:end])
    return raw.strip()


def extract_projects(
    slack_text: str,
    existing_project_names: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Send today's Slack text to Claude and return a list of structured project dicts."""
    print("[project_sync] Calling Claude to extract today's updates...")

    names_str = "\n".join(f"- {n}" for n in (existing_project_names or []))
    user_prompt = _USER_PROMPT_TEMPLATE.format(
        existing_projects=names_str or "(none yet)",
        slack_text=slack_text[:20_000],
    )

    raw = _call_claude(_SYSTEM_PROMPT, user_prompt)
    raw = _strip_code_fences(raw)

    # LLM may return explanation text instead of JSON when there's nothing
    if not raw or (not raw.startswith("[") and not raw.startswith("{")):
        print(f"[project_sync] No JSON in response (likely no updates). Raw: {raw[:200]}")
        return []

    try:
        projects: list[dict[str, Any]] = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"[project_sync] JSON parse error: {exc} — raw response:\n{raw[:500]}")
        return []

    # Normalise each project dict to guarantee expected keys
    required_keys: dict[str, Any] = {
        "name": "",
        "description": "",
        "status": "active",
        "achievements": [],
        "in_progress": [],
        "blockers": [],
    }
    for proj in projects:
        for key, default in required_keys.items():
            proj.setdefault(key, default)

    print(f"[project_sync] Extracted {len(projects)} project(s)")
    return projects


# ---------------------------------------------------------------------------
# sync_projects (main entry point)
# ---------------------------------------------------------------------------


def _sync_linear_projects_direct(
    linear_data: dict[str, Any], projects_db: Any
) -> tuple[int, int]:
    """Upsert Linear project names and statuses only.

    Individual issues are NOT added as entries — Linear tasks are unreliable
    since they're rarely updated. We keep only the project-level metadata
    (name, description, status) for organizational structure.

    Returns (projects_synced, entries_added).
    """
    synced = 0

    state_map = {
        "started": "active",
        "planned": "active",
        "completed": "completed",
        "canceled": "completed",
        "paused": "paused",
        "backlog": "paused",
    }

    for lp in linear_data["projects"]:
        status = state_map.get(lp["state"], "active")
        try:
            projects_db.upsert_project(
                name=lp["name"],
                description=lp.get("description") or None,
                status=status,
                source="linear",
                source_id=lp["id"],
            )
            synced += 1
        except Exception as exc:
            print(f"[project_sync] Linear upsert failed for '{lp['name']}': {exc}")

    return synced, 0


def sync_projects() -> dict[str, int]:
    """Orchestrate fetch → extract → upsert for TODAY's work only.

    Daily sync: pulls only today's Slack messages and extracts what
    Nico did today. Historical data comes from sync_historical().
    """
    import projects_db  # type: ignore[import]

    today = date.today()
    slack_text = ""
    linear_data: dict[str, Any] | None = None

    try:
        slack_text = fetch_slack_updates(days_back=1)  # Today only
    except Exception as exc:
        print(f"[project_sync] Slack fetch failed: {exc}")

    try:
        linear_data = fetch_linear_data()
    except Exception as exc:
        print(f"[project_sync] Linear fetch failed: {exc}")

    projects_db.init_db()
    projects_synced = 0
    entries_added = 0

    # 1) Direct Linear sync (project metadata only, no issues)
    if linear_data:
        ls, le = _sync_linear_projects_direct(linear_data, projects_db)
        projects_synced += ls
        entries_added += le
        print(f"[project_sync] Linear direct: {ls} projects, {le} entries")

    # 2) LLM extraction from today's Slack messages
    if slack_text.strip():
        existing_names = [p["name"] for p in projects_db.get_all_projects()]
        projects = extract_projects(
            slack_text,
            existing_project_names=existing_names,
        )

        for proj in projects:
            try:
                pid = projects_db.upsert_project(
                    name=proj["name"],
                    description=proj["description"],
                    status=proj["status"],
                    source="sync",
                )
                projects_synced += 1
            except Exception as exc:
                print(f"[project_sync] upsert failed for '{proj['name']}': {exc}")
                continue

            has_content = proj["achievements"] or proj["in_progress"] or proj["blockers"]
            if has_content:
                try:
                    projects_db.add_entry(
                        project_id=pid,
                        date=today.isoformat(),
                        achievements=proj["achievements"] or None,
                        in_progress=proj["in_progress"] or None,
                        blockers=proj["blockers"] or None,
                        source="sync",
                    )
                    entries_added += 1
                except Exception as exc:
                    print(f"[project_sync] add_entry failed for '{proj['name']}': {exc}")

    print(
        f"[project_sync] Done — {projects_synced} project(s) synced, "
        f"{entries_added} entry(ies) added."
    )
    return {"projects_synced": projects_synced, "entries_added": entries_added}


# ---------------------------------------------------------------------------
# Historical backfill — one-time extraction of dated entries
# ---------------------------------------------------------------------------

_HISTORY_SYSTEM_PROMPT = """\
You are extracting a TIMELINE of project updates from Nico Amoretti's work \
communications at Telegraph (rail logistics). Each entry should be attributed \
to a specific date based on context (message timestamps, meeting dates, etc.).

## PROJECT SCOPE BOUNDARIES (CRITICAL — read carefully)

These projects are DISTINCT. Do NOT confuse them:

- **Q1 Outbound**: Phone-based cold calling. Includes: dials, connects, connect \
  rates, call scripts, Adam's calling work, voicemails, gatekeeper strategies, \
  HubSpot call tracking, call time windows. This is the PHONE channel.
- **LinkedIn Outreach**: LinkedIn ONLY. Includes: InMail, LinkedIn connection \
  requests, LinkedIn profiles, Sales Navigator, LinkedIn campaigns. NOT phone calls.
- **Mexico Expansion**: Mexico-specific strategy. Includes: Ferromex, FXE, \
  Mexico InMails, MX accounts, Mexico pipeline. Cross-channel (LinkedIn + phone).
- **Mexico cold calling**: The phone-calling subset of Mexico work specifically.
- **Outbound Dashboard**: BUILDING/CODING the dashboard tool itself. \
  Code, deployment, GitHub Pages, data pipelines. NOT the act of doing outbound.
- **Market Consist Email Sequences**: Apollo EMAIL sequences to Chemicals + Ag. \
  NOT phone calls, NOT LinkedIn.
- **Lead Generation**: Finding NEW leads/prospects. Rail spur data, company lists, \
  prospect identification. NOT contacting them (that's Outbound/LinkedIn/Email).
- **Rail Spur Data Enrichment**: The technical data pipeline — OSM extraction, \
  geographic data, enriching company data. Overlaps with Lead Generation but \
  this is the DATA/ENGINEERING side.
- **Sales Dashboard Development**: BUILDING the sales analytics dashboard code. \
  NOT using it. If Nico shares a dashboard URL = this project's achievement.
- **Lease Health** vs **Lease Health Prototype**: "Lease Health" is the product \
  vision. "Lease Health Prototype" is the built demo. Sharing a demo URL = \
  Prototype achievement.
- **Channel Partnership Development**: Identifying and engaging vertical SaaS \
  partners (Elemica, etc.). NOT direct sales.
- **Objection handling doc. for cold calling** vs **Objection Handling Documentation**: \
  These are the SAME project. Use "Objection handling doc. for cold calling".
- **Board meeting update**: Preparing slides/materials FOR the board. NOT the \
  meeting itself.
- **Customer Survey Analysis** vs **CS survey coding + hosting setup**: Analysis = \
  looking at survey RESULTS. Coding + hosting = BUILDING the survey tool.

## RULES

1. Group updates by the DATE they occurred (use Slack timestamps).
2. ACCOMPLISHED = deliverables shared, URLs sent, explicit "done"/"sent" language.
3. IN PROGRESS = work mentioned as ongoing with no completion signal.
4. Only Nico's work — not Shachar's or Harris's.
5. Be specific — quote actual numbers, names, URLs when available.
6. Skip dates with no meaningful Nico updates.
7. Do NOT duplicate items across projects. Each item belongs to exactly ONE project.
8. When in doubt about which project, pick the MORE SPECIFIC one.

Return ONLY valid JSON — no markdown fences."""

_HISTORY_USER_TEMPLATE = """\
## Projects (use these EXACT names)
{project_names}

## Slack Messages (with timestamps)
{slack_text}

## Google Doc (1:1 meeting notes — roughly weekly, sections may not have dates)
{google_doc_text}

---

Extract Nico's project updates as a dated timeline. Each item goes to EXACTLY \
ONE project — the most specific match. Phone/calling work = Q1 Outbound. \
LinkedIn work = LinkedIn Outreach. Mexico-specific = Mexico Expansion. \
Building a tool = the tool's project. Finding leads = Lead Generation.

Return JSON array sorted by date:
[
  {{
    "date": "2026-02-11",
    "entries": [
      {{
        "project": "Mexico Expansion",
        "achievements": ["Sent 55 InMails to logistics managers"],
        "in_progress": ["Refining ICP for MX accounts"],
        "blockers": ["Language barrier"]
      }}
    ]
  }}
]"""


def sync_historical() -> dict[str, int]:
    """One-time backfill: extract dated entries from Slack + Google Doc history."""
    import projects_db

    projects_db.init_db()

    # Get existing project names
    all_projects = projects_db.get_all_projects()
    project_names = "\n".join(f"- {p['name']}" for p in all_projects)
    name_to_id = {p["name"].lower(): p["id"] for p in all_projects}

    # Fetch source data
    google_doc_text = ""
    slack_text = ""
    try:
        google_doc_text = fetch_google_doc()
    except Exception as exc:
        print(f"[history] Google Doc failed: {exc}")

    try:
        slack_text = fetch_slack_updates()
    except Exception as exc:
        print(f"[history] Slack failed: {exc}")

    if not google_doc_text and not slack_text:
        print("[history] No source data available")
        return {"dates": 0, "entries": 0}

    user_prompt = _HISTORY_USER_TEMPLATE.format(
        project_names=project_names,
        slack_text=slack_text[:30_000],
        google_doc_text=google_doc_text[:30_000],
    )

    print("[history] Calling Claude for historical extraction...")
    raw = _call_claude(_HISTORY_SYSTEM_PROMPT, user_prompt)
    raw = _strip_code_fences(raw)

    try:
        timeline = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"[history] JSON parse error: {exc}")
        print(f"[history] Raw: {raw[:500]}")
        return {"dates": 0, "entries": 0}

    if not isinstance(timeline, list):
        print("[history] Expected array, got:", type(timeline))
        return {"dates": 0, "entries": 0}

    # Insert entries
    dates_processed = 0
    entries_added = 0

    for day in timeline:
        d = day.get("date", "")
        if not d:
            continue
        dates_processed += 1

        for entry in day.get("entries", []):
            proj_name = entry.get("project", "")
            pid = name_to_id.get(proj_name.lower())
            if not pid:
                # Fuzzy match: find closest
                for db_name, db_id in name_to_id.items():
                    if proj_name.lower() in db_name or db_name in proj_name.lower():
                        pid = db_id
                        break
            if not pid:
                print(f"[history] No match for project '{proj_name}', skipping")
                continue

            achievements = entry.get("achievements") or []
            in_progress = entry.get("in_progress") or []
            blockers = entry.get("blockers") or []

            if achievements or in_progress or blockers:
                try:
                    projects_db.add_entry(
                        project_id=pid,
                        date=d,
                        achievements=achievements or None,
                        in_progress=in_progress or None,
                        blockers=blockers or None,
                        source="history",
                    )
                    entries_added += 1
                except Exception as exc:
                    print(f"[history] add_entry failed: {exc}")

    print(f"[history] Done — {dates_processed} dates, {entries_added} entries added")
    return {"dates": dates_processed, "entries": entries_added}


# ---------------------------------------------------------------------------
# Screenpipe → Shipped: extract achievements from screen activity
# ---------------------------------------------------------------------------

_SCREENPIPE_SYSTEM = """\
You are analyzing Nico Amoretti's workday at Telegraph (rail logistics). \
You receive a COMPLETE timeline of app sessions showing every app switch, \
window title, document name, search query, and URL visited — plus key OCR \
text samples and audio transcripts.

## Your job
1. Identify the 2-4 MAIN STRATEGIC ARCS of the day by reading the timeline.
2. Extract specific, concrete work items under the right project.

## CRITICAL RULES — READ CAREFULLY

### Only state what you can SEE
- Window titles tell you WHAT was open. OCR samples tell you WHAT was on screen.
- For meetings: you can see the meeting title and who was in it. You CANNOT \
  see what was discussed unless OCR/audio explicitly shows it.
- NEVER fabricate meeting discussion topics, action items, or decisions.
- If you see "Meet - Nicolas / Aditya - Intro chat", say exactly that: \
  "Intro call with Aditya Murthi (~30 min)". Do NOT invent what was discussed.

### Capture the strategic narrative
- When Nico spends 2+ hours researching cold email best practices across \
  Reddit, SaaStr, and comparison sites, then evaluating NeverBounce and \
  ZeroBounce, then testing deliverability with mail-tester.com, then checking \
  DMARC and Postmaster spam rates — that is ONE strategic initiative: \
  "Reworking email deliverability infrastructure". Capture it as such.
- When Nico edits multiple playbook documents (objection handling, cold calling, \
  outbound playbook v2), that's "Updating outbound playbooks and strategy docs".
- Don't fragment a coherent initiative into 8 separate tiny items.

### Be specific with tool/platform names
- Name every tool evaluated: NeverBounce, ZeroBounce, MailReach, mail-tester.com
- Name every document edited: telegraph-outbound-playbook-v2, cold-calling-playbook
- Name every platform used: Apollo, HubSpot, Sales Navigator, Postmaster Tools
- Name search queries that reveal intent (not just "googled stuff")
- Name specific people in LinkedIn/Sales Navigator prospecting

### What to skip
- Personal browsing, food/restaurant searches, personal texts
- Tabs that were just open in the background (no interaction)

### Format
- First person, concise, one sentence per item (max 20 words)
- Achievements = completed, shipped, sent, configured, evaluated, decided
- In-progress = ongoing work, not yet finished

Return ONLY valid JSON:
[
  {{
    "project": "Exact Project Name",
    "achievements": ["Specific item with tool/person/finding names"],
    "in_progress": ["Specific ongoing work"]
  }}
]"""

_SCREENPIPE_USER = """\
## Projects to attribute work to (use EXACT names)
## Format: [Tag] Project Name — tag indicates the category

{project_names}

## Tag definitions
- Outbound: Sales outreach — cold calling, email sequences, LinkedIn, objections
- Product: Building/coding tools — dashboards, KB, webcam, revenue hub
- Intelligence: Research — lead gen, data analysis, metabase
- Product Vision: PRDs, prototypes — demurrage, lease health, RFC
- GTM: Go-to-market — partnerships, demos, shipper strategy, board prep
- Internal: Internal ops — surveys, prep, kickoffs, to-dos

## SESSION TIMELINE (complete — every app switch and window title today)
{session_headers}

## KEY OCR SAMPLES (selected moments with on-screen text)
{ocr_samples}

## AUDIO TRANSCRIPTS
{audio_text}

{corrections}

---
Extract Nico's work from the timeline above. Read the FULL session timeline \
to understand the strategic arc before extracting items. Group related \
activities (e.g. researching + evaluating + configuring tools) under one \
project, not fragmented across many."""


def _build_session_headers(activity_text: str) -> str:
    """Extract just the session header lines from build_activity_text output.

    These are the [HH:MM–HH:MM] App — Window1, Window2, ... lines
    that capture every app switch and window title for the full day.
    """
    return "\n".join(
        line for line in activity_text.splitlines()
        if line.startswith("[") and "–" in line[:20]
    )


def _build_ocr_samples(activity_text: str, max_chars: int = 50_000) -> str:
    """Extract OCR text samples (the '  > ...' lines) from activity text.

    Evenly samples from start, middle, and end to cover the full day.
    """
    sample_lines = [
        line for line in activity_text.splitlines()
        if line.startswith("  > ")
    ]
    if not sample_lines:
        return ""

    # Take samples evenly across the day
    total = len(sample_lines)
    if total <= 100:
        selected = sample_lines
    else:
        step = max(1, total // 100)
        selected = sample_lines[::step][:100]

    result = "\n".join(selected)
    if len(result) > max_chars:
        result = result[:max_chars]
    return result


def _build_audio_section(activity_text: str) -> str:
    """Extract audio transcript section if present."""
    marker = "=== SPOKEN / AUDIO ==="
    idx = activity_text.find(marker)
    if idx < 0:
        return "(no audio transcripts)"
    return activity_text[idx:][:8_000]


def _build_corrections_section(limit: int = 20) -> str:
    """Build natural-language corrections for the LLM prompt."""
    import projects_db

    corrections = projects_db.get_recent_corrections(limit=limit)
    if not corrections:
        return ""

    def _safe(s: str, maxlen: int = 200) -> str:
        return s.replace("\n", " ").replace("\r", "")[:maxlen]

    lines = ["## User Corrections (apply these patterns to avoid repeating mistakes)"]
    for c in corrections:
        action = c["action"]
        orig_proj = _safe(c.get("orig_project_name") or "Unknown")
        corr_proj = _safe(c.get("corr_project_name") or "")
        orig_text = _safe(c.get("original_text") or "")
        corr_text = _safe(c.get("corrected_text") or "")

        if action == "reassign" and corr_proj:
            lines.append(f"- REASSIGNED: \"{orig_text}\" from {orig_proj} → {corr_proj}")
        elif action == "delete":
            lines.append(f"- DELETED: \"{orig_text}\" from {orig_proj} (junk/not-work)")
        elif action == "edit_text" and corr_text:
            lines.append(f"- EDITED: \"{orig_text}\" → \"{corr_text}\" in {orig_proj}")
        elif action == "rename_project" and corr_text:
            lines.append(f"- RENAMED PROJECT: \"{orig_text}\" → \"{corr_text}\"")

    return "\n".join(lines)


def sync_screenpipe_shipped() -> dict[str, int]:
    """Extract achievements from today's screenpipe OCR + audio data.

    Uses session headers (compact, full-day coverage) plus targeted OCR samples
    to give the LLM complete context without truncating the middle of the day.
    """
    import activity_mapper
    import projects_db
    import screenpipe

    today = date.today()
    date_str = today.isoformat()

    # 1) Map activity for today (updates project_activity table)
    print("[screenpipe-sync] Mapping today's screen activity to projects...")
    try:
        activity_mapper.map_activity_for_date(today)
    except Exception as exc:
        print(f"[screenpipe-sync] Activity mapping failed: {exc}")

    # 2) Build the full activity text
    print("[screenpipe-sync] Building activity timeline from OCR + audio...")
    try:
        raw_frames = screenpipe.get_ocr_frames(today)
        deduped = screenpipe.deduplicate_ocr(raw_frames)
        audio = screenpipe.get_audio_transcripts(today)
        activity_text = screenpipe.build_activity_text(deduped, audio)
    except Exception as exc:
        print(f"[screenpipe-sync] Failed to build activity text: {exc}")
        return {"entries_added": 0}

    if not activity_text or len(activity_text) < 200:
        print("[screenpipe-sync] Not enough activity data yet")
        return {"entries_added": 0}

    # 3) Build compact but complete prompt
    session_headers = _build_session_headers(activity_text)
    ocr_samples = _build_ocr_samples(activity_text)
    audio_section = _build_audio_section(activity_text)

    print(f"[screenpipe-sync] Headers: {len(session_headers):,} chars, "
          f"OCR samples: {len(ocr_samples):,} chars, "
          f"Audio: {len(audio_section):,} chars")

    all_projects = projects_db.get_all_projects(status="active")
    project_names = "\n".join(
        f"- [{p.get('tag') or 'Untagged'}] {p['name']}" for p in all_projects
    )
    name_to_id = {p["name"].lower(): p["id"] for p in all_projects}

    corrections_text = _build_corrections_section(limit=20)

    user_prompt = _SCREENPIPE_USER.format(
        project_names=project_names,
        session_headers=session_headers,
        ocr_samples=ocr_samples,
        audio_text=audio_section,
        corrections=corrections_text,
    )

    # 4) Call LLM
    print(f"[screenpipe-sync] Calling Claude ({len(user_prompt):,} chars prompt)...")
    try:
        raw = _call_claude(_SCREENPIPE_SYSTEM, user_prompt, model="sonnet")
    except Exception as exc:
        print(f"[screenpipe-sync] LLM call failed: {exc}")
        return {"entries_added": 0}

    raw = _strip_code_fences(raw)
    if not raw or (not raw.startswith("[") and not raw.startswith("{")):
        print(f"[screenpipe-sync] No JSON in response: {raw[:200]}")
        return {"entries_added": 0}

    try:
        entries_list = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"[screenpipe-sync] JSON parse error: {exc}")
        return {"entries_added": 0}

    # 5) Upsert into project_entries
    entries_added = 0

    for entry in entries_list:
        proj_name = entry.get("project", "")
        pid = name_to_id.get(proj_name.lower())
        if not pid:
            # Fuzzy match
            for db_name, db_id in name_to_id.items():
                if proj_name.lower() in db_name or db_name in proj_name.lower():
                    pid = db_id
                    break
        if not pid:
            print(f"[screenpipe-sync] No match for project '{proj_name}', skipping")
            continue

        achievements = entry.get("achievements") or []
        in_progress = entry.get("in_progress") or []

        if achievements or in_progress:
            try:
                projects_db.add_entry(
                    project_id=pid,
                    date=date_str,
                    achievements=achievements or None,
                    in_progress=in_progress or None,
                    blockers=None,
                    source="screenpipe",
                )
                entries_added += 1
                print(f"  {proj_name}: {len(achievements)} done, {len(in_progress)} wip")
            except Exception as exc:
                print(f"[screenpipe-sync] add_entry failed for '{proj_name}': {exc}")

    print(f"[screenpipe-sync] Done — {entries_added} project entries from screen activity")
    return {"entries_added": entries_added}


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--history":
        result = sync_historical()
    elif len(sys.argv) > 1 and sys.argv[1] == "--repos":
        from repo_scanner import sync_repos
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
        result = sync_repos(days_back=days)
    elif len(sys.argv) > 1 and sys.argv[1] == "--screenpipe":
        result = sync_screenpipe_shipped()
    else:
        result = sync_projects()
    print(result)
