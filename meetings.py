from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")

# Window name patterns that indicate a meeting
MEETING_PATTERNS = ["google meet", "zoom", "microsoft teams", "webex", "zoom meeting"]

# Maximum gap between frames to still consider them the same meeting (minutes)
MEETING_GAP_MINUTES = 5

# Suffixes to strip when extracting a clean meeting title
_TITLE_STRIP_SUFFIXES = [
    " - google chrome",
    " - chrome",
    " - firefox",
    " - safari",
    " - microsoft edge",
    " - zoom",
    " - teams",
    " - webex",
    " | zoom",
    " | teams",
    " | webex",
]

# Human-readable app names keyed by the matched pattern
_APP_DISPLAY_NAMES: dict[str, str] = {
    "google meet": "Google Meet",
    "zoom meeting": "Zoom",
    "zoom": "Zoom",
    "microsoft teams": "Microsoft Teams",
    "webex": "Webex",
}


def _parse_timestamp(ts: str) -> datetime:
    """Parse an ISO timestamp string and return it as a Pacific-time datetime.

    Handles both ``2024-01-15T10:30:00Z`` and ``2024-01-15T10:30:00+00:00``
    formats.
    """
    # Replace trailing Z with +00:00 so fromisoformat handles it uniformly
    normalised = ts.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalised)
    return dt.astimezone(PACIFIC)


def _extract_title(window_names: list[str]) -> str:
    """Return the most descriptive meeting title from a collection of window names.

    Strategy:
    1. Strip common browser/app suffixes from every candidate.
    2. Discard names that are only the bare app keyword (e.g. "Zoom").
    3. Return the longest remaining candidate, or "Untitled Meeting" if none.
    """
    candidates: list[str] = []

    for raw in window_names:
        cleaned = raw.strip()
        lower = cleaned.lower()

        # Strip known trailing suffixes (case-insensitive)
        for suffix in _TITLE_STRIP_SUFFIXES:
            if lower.endswith(suffix):
                cleaned = cleaned[: len(cleaned) - len(suffix)].strip()
                lower = cleaned.lower()
                break

        # Skip if the result is just a bare app keyword
        if lower in {p.lower() for p in MEETING_PATTERNS}:
            continue

        if cleaned:
            candidates.append(cleaned)

    if not candidates:
        return "Untitled Meeting"

    # Prefer the longest (most informative) title
    return max(candidates, key=len)


def _match_app(window_name: str) -> str | None:
    """Return the matched pattern string if the window name contains a meeting keyword."""
    lower = window_name.lower()
    # Check longer/more-specific patterns first to avoid "zoom" shadowing "zoom meeting"
    for pattern in sorted(MEETING_PATTERNS, key=len, reverse=True):
        if pattern in lower:
            return pattern
    return None


def _format_time(dt: datetime) -> str:
    """Format a datetime as HH:MM (24-hour, Pacific)."""
    return dt.strftime("%H:%M")


def detect_meetings(
    ocr_frames: list[dict],
    audio_transcripts: list[dict],
) -> list[dict]:
    """Detect meeting blocks from OCR frames and attach audio transcripts.

    Parameters
    ----------
    ocr_frames:
        Deduplicated OCR frame dicts as returned by screenpipe.py.  Each dict
        is expected to have at least ``timestamp`` (ISO str) and
        ``window_name`` (str) keys.
    audio_transcripts:
        Audio transcript dicts as returned by screenpipe.py.  Each dict is
        expected to have ``timestamp`` (ISO str), ``text`` (str), and
        ``is_input_device`` (bool) keys.

    Returns
    -------
    list[dict]
        A list of meeting dicts, each with keys: ``start``, ``end``,
        ``duration_minutes``, ``app``, ``title``, and ``transcript``.
    """
    # ------------------------------------------------------------------ #
    # Pass 1 — Window detection: group matching frames into meeting blocks #
    # ------------------------------------------------------------------ #

    # Sort OCR frames by timestamp
    sorted_frames = sorted(ocr_frames, key=lambda f: f["timestamp"])

    # Each block accumulates matching frames until a gap exceeds the threshold
    blocks: list[dict] = []  # raw block dicts built during grouping
    current_block: dict | None = None

    for frame in sorted_frames:
        window_name: str = frame.get("window_name", "")
        matched_pattern = _match_app(window_name)

        if matched_pattern is None:
            # Not a meeting frame — close any open block
            current_block = None
            continue

        frame_dt = _parse_timestamp(frame["timestamp"])

        if current_block is None:
            # Start a new block
            current_block = {
                "_start_dt": frame_dt,
                "_end_dt": frame_dt,
                "_pattern": matched_pattern,
                "_window_names": [window_name],
            }
            blocks.append(current_block)
        else:
            gap = (frame_dt - current_block["_end_dt"]).total_seconds() / 60
            if gap > MEETING_GAP_MINUTES:
                # Gap too large — start a fresh block
                current_block = {
                    "_start_dt": frame_dt,
                    "_end_dt": frame_dt,
                    "_pattern": matched_pattern,
                    "_window_names": [window_name],
                }
                blocks.append(current_block)
            else:
                # Extend the current block
                current_block["_end_dt"] = frame_dt
                current_block["_window_names"].append(window_name)
                # Allow the pattern to update (e.g. if the user switched apps)
                current_block["_pattern"] = matched_pattern

    # ------------------------------------------------------------------ #
    # Pass 2 — Audio attachment                                            #
    # ------------------------------------------------------------------ #

    # Pre-parse all audio timestamps once for efficiency
    parsed_audio: list[tuple[datetime, dict]] = []
    for entry in audio_transcripts:
        try:
            dt = _parse_timestamp(entry["timestamp"])
            parsed_audio.append((dt, entry))
        except (KeyError, ValueError):
            continue

    meetings: list[dict] = []

    for block in blocks:
        start_dt: datetime = block["_start_dt"]
        end_dt: datetime = block["_end_dt"]
        pattern: str = block["_pattern"]
        window_names: list[str] = block["_window_names"]

        # Attach audio with a ±1-minute buffer
        audio_start = start_dt - timedelta(minutes=1)
        audio_end = end_dt + timedelta(minutes=1)

        transcript: list[dict] = []
        for entry_dt, entry in parsed_audio:
            if audio_start <= entry_dt <= audio_end:
                speaker = "[you]" if entry.get("is_input_device") else "[other]"
                transcript.append(
                    {
                        "time": _format_time(entry_dt),
                        "speaker": speaker,
                        "text": (entry.get("transcription") or "").strip(),
                    }
                )

        # Sort transcript entries chronologically
        transcript.sort(key=lambda t: t["time"])

        duration_minutes = max(
            1, round((end_dt - start_dt).total_seconds() / 60)
        )

        meetings.append(
            {
                "start": _format_time(start_dt),
                "end": _format_time(end_dt),
                "duration_minutes": duration_minutes,
                "app": _APP_DISPLAY_NAMES.get(pattern, pattern.title()),
                "title": _extract_title(window_names),
                "transcript": transcript,
            }
        )

    return meetings
