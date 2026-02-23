from __future__ import annotations

import os
import json
from datetime import date, datetime, timezone
from openai import OpenAI

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
GEMINI_MODEL = "gemini-2.0-flash"
MAX_CONTEXT_CHARS = 80_000

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONTEXT_FILE = os.path.join(SCRIPT_DIR, "context.md")

SYSTEM_PROMPT_TEMPLATE = """\
You are a precise work activity analyst. You receive raw screen OCR data and audio transcripts from a workday. Your job is to extract SPECIFIC, CONCRETE details — not vague summaries.

## About Me
{user_context}

## Precision Rules — CRITICAL
- ALWAYS name specific people, companies, deal names, and projects you see in the data
- ALWAYS include specific numbers, metrics, and dollar amounts when visible in the OCR text
- ALWAYS reference exact window titles, document names, and URLs when they reveal what I was working on
- Quote specific phrases from meeting transcripts — don't just say "discussed revenue"
- When you see a company name (e.g. "Glencore", "Valero"), mention it by name
- When you see a metric (e.g. "$50K pipeline", "17,000 companies"), include the number
- If you see a tab title like "Outreach Performance — Jan 19 – Feb 23", reference it specifically
- Write in first person for the summary
- Do NOT mention Screenpipe, OCR, screen capture, or that this was auto-generated
- Do NOT include passwords, tokens, or API keys
- Skip personal apps (Telegram, etc.) unless work-related
- For audio: ONLY include work-related conversations. Omit personal/social entirely.
- Note: timestamps in the data are UTC. I'm in Pacific Time (UTC-8). Convert all times to Pacific in your output.

Return ONLY valid JSON with this exact structure:
{{
  "summary": "2-3 sentence overview — name the key projects, people, and outcomes",
  "insights": [
    "Specific observation with numbers — e.g. 'Spent 2h15m in the Sales Pipeline meeting (10:34-12:48 PT), the longest block today'",
    "Pattern observation — e.g. 'Switched between Chrome and VS Code 14 times between 11:00-12:00, suggesting multitasking during the meeting'"
  ],
  "activities": [
    {{
      "title": "Specific activity name — e.g. 'Sales Pipeline meeting with Shachar'",
      "time": "HH:MM – HH:MM (Pacific)",
      "description": "Concrete details: who was there, what was discussed/decided, specific companies or deals mentioned, metrics reviewed. NOT vague summaries."
    }}
  ],
  "next_steps": [
    {{
      "item": "Concrete follow-up action — e.g. 'Follow up with Valero on InMail response'",
      "context": "Where this came from — e.g. 'Sales Pipeline meeting' or 'HubSpot review'"
    }}
  ]
}}

Guidelines:
- **summary**: Specific narrative. Name the 2-3 main things that happened. Include a concrete detail in each sentence.
- **insights**: 3-5 data-driven observations about the day. Use actual time calculations from the timestamps. Note patterns like: time per app, meeting-to-focus ratio, context switch frequency. Every insight must include a number or specific reference.
- **activities**: 4-8 blocks. Each must have specifics extracted from the OCR/audio — company names mentioned, documents opened, metrics seen on dashboards, action items from meetings. Don't say "reviewed pipeline" when you can say "reviewed Glencore and Mitsubishi deals in HubSpot pipeline".
- **next_steps**: 2-5 concrete action items extracted from meetings, emails, task boards, or other visible work. Each must be specific and actionable — not "review pipeline" but "send Glencore proposal to Sarah by Thursday". Only include items clearly visible in the data.\
"""


def get_cached(d: date) -> dict | None:
    cache_path = os.path.join(CACHE_DIR, f"{d}.json")
    if not os.path.exists(cache_path):
        return None
    with open(cache_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("content")


def save_cached(d: date, content: dict, meta: dict | None = None) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(CACHE_DIR, f"{d}.json")
    payload = {
        "content": content,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "meta": meta,
    }
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def load_context() -> str:
    if not os.path.exists(CONTEXT_FILE):
        return ""
    with open(CONTEXT_FILE, "r", encoding="utf-8") as f:
        return f.read()


def generate(activity_text: str, d: date) -> dict:
    """Call Gemini and return structured dict with summary, insights, activities."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    client = OpenAI(api_key=api_key, base_url=GEMINI_BASE_URL)

    user_context = load_context()
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(user_context=user_context)

    # Take first half + last half of the context window so the LLM sees the
    # full time range of the day (not just the morning).
    if len(activity_text) > MAX_CONTEXT_CHARS:
        half = MAX_CONTEXT_CHARS // 2
        truncated = (
            activity_text[:half]
            + "\n\n[... middle portion omitted for length ...]\n\n"
            + activity_text[-half:]
        )
    else:
        truncated = activity_text

    response = client.chat.completions.create(
        model=GEMINI_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Date: {d}\n\n{truncated}"},
        ],
        temperature=0.3,
        max_tokens=4000,
    )

    raw = response.choices[0].message.content or "{}"

    # Strip markdown code fences if present
    raw = raw.strip()
    if raw.startswith("```"):
        # Remove first line (```json) and last line (```)
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        # Fallback: treat entire response as a plain summary
        result = {"summary": raw, "insights": [], "activities": []}

    # Ensure all keys exist
    result.setdefault("summary", "")
    result.setdefault("insights", [])
    result.setdefault("activities", [])
    result.setdefault("next_steps", [])

    return result


def summarize_day(activity_text: str, d: date, force: bool = False) -> dict:
    """Main entry point. Returns cached structured content or generates + caches."""
    if not force:
        cached = get_cached(d)
        if cached is not None:
            return cached

    content = generate(activity_text, d)

    meta = {
        "input_chars": len(activity_text),
        "truncated": len(activity_text) > MAX_CONTEXT_CHARS,
    }
    save_cached(d, content, meta)

    return content


# Backward compat alias
def get_cached_summary(d: date) -> dict | None:
    return get_cached(d)
