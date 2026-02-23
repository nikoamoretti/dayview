"""Role-based classification for Screenpipe frames.

Maps browser URLs and app names to work roles (Sales, Engineering, etc.)
and computes time-per-role from frame timestamps.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Role colour palette (consistent across backend JSON and frontend UI)
# ---------------------------------------------------------------------------
ROLE_COLORS: dict[str, str] = {
    "Sales": "#4ADE80",
    "Engineering": "#4A9EFF",
    "Meetings": "#FBBF24",
    "Communication": "#A78BFA",
    "Analytics": "#2DD4BF",
    "Operations": "#94A3B8",
    "Other": "#64748B",
}

# ---------------------------------------------------------------------------
# Domain → role mapping (substring match against the hostname)
# ---------------------------------------------------------------------------
DOMAIN_ROLES: dict[str, str] = {
    "hubspot.com": "Sales",
    "apollo.io": "Sales",
    "linkedin.com": "Sales",
    "metabase": "Analytics",
    "claude.ai": "Engineering",
    "github.com": "Engineering",
    "localhost": "Engineering",
    "meet.google.com": "Meetings",
    "zoom.us": "Meetings",
    "slack.com": "Communication",
    "docs.google.com": "Operations",
    "sheets.google.com": "Operations",
}

# App name → role fallback (exact match on app_name)
APP_ROLES: dict[str, str] = {
    "Code": "Engineering",
    "Obsidian": "Engineering",
    "Slack": "Communication",
    "System Settings": "Other",
}

# Maximum seconds between consecutive frames to count as continuous activity.
# Gaps larger than this are ignored (user was away).
MAX_GAP_SECONDS = 120


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def _domain_from_url(url: str) -> str:
    """Extract hostname from a URL, or return empty string."""
    if not url:
        return ""
    try:
        return urlparse(url).hostname or ""
    except Exception:
        return ""


def classify_frame(frame: dict) -> str:
    """Classify a single frame into a work role.

    Priority: browser_url domain match > app_name match > "Other".
    """
    # 1. Try URL-based classification (most specific)
    url = frame.get("browser_url") or ""
    if url:
        hostname = _domain_from_url(url)
        if hostname:
            # Check longer/more-specific domains first (meet.google.com before google.com)
            for domain, role in sorted(
                DOMAIN_ROLES.items(), key=lambda x: len(x[0]), reverse=True
            ):
                if domain in hostname:
                    return role

    # 2. Fall back to app name
    app = frame.get("app_name") or ""
    if app in APP_ROLES:
        return APP_ROLES[app]

    return "Other"


def classify_frames(frames: list[dict]) -> dict[str, list[dict]]:
    """Group frames by their classified role.

    Returns a dict mapping role name -> list of frames belonging to that role.
    """
    grouped: dict[str, list[dict]] = {}
    for frame in frames:
        role = classify_frame(frame)
        grouped.setdefault(role, []).append(frame)
    return grouped


# ---------------------------------------------------------------------------
# Time computation
# ---------------------------------------------------------------------------
def _parse_ts(ts_str: str) -> datetime | None:
    """Parse an ISO timestamp string into a datetime object."""
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def compute_role_minutes(frames: list[dict]) -> list[dict]:
    """Estimate minutes spent per role using inter-frame time gaps.

    Returns a list sorted by minutes descending:
    [{"role": "Sales", "minutes": 135, "pct": 38, "color": "#4ADE80"}, ...]
    """
    role_seconds: dict[str, float] = {}

    # Sort by timestamp for gap calculation
    sorted_frames = sorted(frames, key=lambda f: f.get("timestamp", ""))

    prev_ts: datetime | None = None
    prev_role: str | None = None

    for frame in sorted_frames:
        role = classify_frame(frame)
        ts = _parse_ts(frame.get("timestamp", ""))

        if ts and prev_ts and prev_role:
            gap = (ts - prev_ts).total_seconds()
            if 0 < gap <= MAX_GAP_SECONDS:
                role_seconds[prev_role] = role_seconds.get(prev_role, 0) + gap

        prev_ts = ts
        prev_role = role

    total_seconds = sum(role_seconds.values())
    if total_seconds == 0:
        return []

    result = []
    for role, secs in sorted(
        role_seconds.items(), key=lambda x: x[1], reverse=True
    ):
        minutes = round(secs / 60)
        if minutes < 1:
            continue
        pct = round(secs / total_seconds * 100)
        result.append({
            "role": role,
            "minutes": minutes,
            "pct": pct,
            "color": ROLE_COLORS.get(role, ROLE_COLORS["Other"]),
        })

    return result


def compute_focus_time(frames: list[dict]) -> int:
    """Sum minutes where focused=True using inter-frame gaps.

    Returns total focused minutes as an integer.
    """
    sorted_frames = sorted(frames, key=lambda f: f.get("timestamp", ""))

    focus_seconds = 0.0
    prev_ts: datetime | None = None
    prev_focused: bool = False

    for frame in sorted_frames:
        ts = _parse_ts(frame.get("timestamp", ""))
        focused = bool(frame.get("focused"))

        if ts and prev_ts and prev_focused:
            gap = (ts - prev_ts).total_seconds()
            if 0 < gap <= MAX_GAP_SECONDS:
                focus_seconds += gap

        prev_ts = ts
        prev_focused = focused

    return round(focus_seconds / 60)
