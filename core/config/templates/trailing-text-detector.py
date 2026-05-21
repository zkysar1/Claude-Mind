#!/usr/bin/env python3
"""trailing-text-detector.py — Layer-C observability for the Stop hook.

Seeded into world/scripts/ by init-world.sh from core/config/templates/.

Contract (called from core/scripts/stop-hook.sh):
    Args: --stop-hook
    Input: stdin JSON (Claude Code Stop hook payload, includes transcript_path)
    Output stdout JSON on detection:
        {"marker": <name>, "severity": "high"|"medium"|"low", "evidence": <snippet>}
    On no detection or any error: empty stdout (fail-open).

Markers (per .claude/rules/return-protocol.md "Layered Defense" table):
    insight_block               HIGH    — trailing "✶ Insight" block after final text
    phase_summary               HIGH    — "Phase N complete: ..." narrative after a tool call
    next_step_narration         MEDIUM  — "Next, I'll ..." prose after tool calls
    trailing_prose              LOW     — generic non-trivial prose after final tool call

Only severity=high markers appear in the Stop hook's BLOCK message.
Low/medium markers are still recorded for offline analysis but stay quiet.

Edit this file in world/scripts/ to add domain-specific anti-pattern markers.
"""
from __future__ import annotations

import json
import re
import sys


# Patterns ordered by severity (higher severity = checked first, returned first).
_PATTERNS = (
    # (marker_name, severity, regex_against_tail_2k)
    ("insight_block",       "high",   re.compile(r"✶\s*Insight", re.IGNORECASE)),
    ("phase_summary",       "high",   re.compile(r"\bPhase\s+[0-9.\-]+\s+(complete|done|finished)", re.IGNORECASE)),
    ("next_step_narration", "medium", re.compile(r"\b(Next,?\s+I[' ]?ll|Now\s+I[' ]?ll\s+|I[' ]?ll\s+(now|next))", re.IGNORECASE)),
)


def _last_assistant_text(transcript_path: str) -> str:
    """Return the concatenated text-block content of the most recent assistant turn."""
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except (OSError, FileNotFoundError):
        return ""

    for line in reversed(lines):
        try:
            evt = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if evt.get("type") != "assistant":
            continue
        msg = evt.get("message") or {}
        content = msg.get("content") or []
        if not isinstance(content, list):
            continue
        return "".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def detect(transcript_path: str) -> dict | None:
    """Return marker dict on detection, None otherwise."""
    text = _last_assistant_text(transcript_path)
    if not text.strip():
        return None

    tail = text[-2000:]

    for marker, severity, pattern in _PATTERNS:
        m = pattern.search(tail)
        if m:
            # Evidence: last ~200 chars of the assistant text (capped).
            evidence = text[-200:].strip()
            return {"marker": marker, "severity": severity, "evidence": evidence}

    # Trailing-prose fallback (LOW). Heuristic: > 80 chars of non-fence prose
    # at the tail. Domain may tighten or replace.
    tail_strip = text[-200:].strip()
    if len(tail_strip) > 80 and "```" not in tail_strip:
        return {"marker": "trailing_prose", "severity": "low", "evidence": tail_strip}

    return None


def main() -> int:
    if "--stop-hook" not in sys.argv[1:]:
        return 0
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        return 0
    transcript = payload.get("transcript_path") or ""
    if not transcript:
        return 0
    try:
        result = detect(transcript)
    except Exception:
        # Fail-open on any unexpected error — stop-hook must not break.
        return 0
    if result:
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
