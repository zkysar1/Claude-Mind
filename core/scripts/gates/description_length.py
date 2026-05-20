"""Description-length advisory — daemon-safe extraction (PR 7c/5).

Emits a soft warning + telemetry when a non-recurring goal has a short
description (< 80 chars). g-115-336: warn-only, NEVER blocks. The
g-115-335 audit (2026-04-30) found 2.9% of active non-recurring goals
exhibit description.length < 80 — workflow self-correction (manual
backfill) catches the pattern within hours-to-days. This warn surfaces
the issue at filing time instead.

Recurring goals are exempt: title-as-spec is the documented pattern for
infrastructure checks (g-115-01 'Check agent email inbox', etc.). 35/70
active goals follow this pattern as of the audit.

Telemetry feeds future telemetry-driven promotion to hard validation if
the rate proves persistent. Append failures MUST NOT block goal filing
or the warning emission.

Public API:
    evaluate(goal, *, source, meta_dir=None) -> dict

Return shape:
    {
      "warned": bool,        # True if the warning condition fires
      "message": str | None, # Pre-formatted stderr message (None when not warned)
      "telemetry_written": bool,  # True when meta_dir was provided and append succeeded
      "description_length": int,
    }

The CALLER (CLI or daemon endpoint) is responsible for emitting `message`
to stderr / wherever appropriate. Keeping the side effect at the call site
lets daemon endpoints surface the warning via HTTP response header / body
instead of stderr.

Daemon safety:
  - Reads no env directly. meta_dir is explicit.
  - Telemetry append uses _fileops.locked_append_jsonl, fail-open on error.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional


DESCRIPTION_LENGTH_MIN_CHARS = 80


def evaluate(goal: dict, *, source: str,
             meta_dir: Optional[Path] = None) -> dict:
    """Run the advisory. See module docstring.

    Args:
        goal: The goal dict being filed.
        source: One of "agent", "world", etc. Recorded in telemetry.
        meta_dir: Path for telemetry sidecar. When None, telemetry is
            skipped (still returns a valid result dict).
    """
    if goal.get("recurring"):
        return {
            "warned": False,
            "message": None,
            "telemetry_written": False,
            "description_length": 0,
            "_reason": "recurring goals exempt",
        }
    desc = (goal.get("description") or "").strip()
    desc_len = len(desc)
    if desc_len >= DESCRIPTION_LENGTH_MIN_CHARS:
        return {
            "warned": False,
            "message": None,
            "telemetry_written": False,
            "description_length": desc_len,
        }

    message = (
        f"[add-goal] description short ({desc_len} chars < "
        f"{DESCRIPTION_LENGTH_MIN_CHARS}) on non-recurring goal — verify spec "
        f"is complete; consider expanding before status=pending"
    )

    telemetry_written = False
    if meta_dir is not None:
        try:
            from _fileops import locked_append_jsonl
            record = {
                "filing_time": datetime.now().isoformat(timespec="seconds"),
                "goal_id": goal.get("id") or "<auto-assigned>",
                "len": desc_len,
                "recurring": False,
                "defer_set": bool(goal.get("defer_reason")),
                "source": source,
                "decision": "warn",
            }
            locked_append_jsonl(meta_dir / "description-length-telemetry.jsonl",
                                record)
            telemetry_written = True
        except Exception:
            # Best-effort — never block on telemetry. The caller's stderr
            # message is the primary signal.
            telemetry_written = False

    return {
        "warned": True,
        "message": message,
        "telemetry_written": telemetry_written,
        "description_length": desc_len,
    }
