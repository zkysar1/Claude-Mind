#!/usr/bin/env python3
"""Status line script — captures context window metrics and writes budget file.

Reads the status line JSON payload from stdin, extracts context_window fields,
writes to <agent>/session/context-budget.json, and prints a one-line status to stdout.

Zone thresholds:
  fresh:  used_pct < 40
  normal: 40 <= used_pct < 65
  tight:  used_pct >= 65
"""

import json
import os
import sys
import threading
from datetime import datetime
from pathlib import Path

# Self-destruct after 10s — prevents zombie processes when parent session dies
# without closing stdin (Windows doesn't propagate EOF to orphaned children).
# MUST be daemon=True so normal exit isn't blocked waiting for the timer.
_timer = threading.Timer(10, lambda: os._exit(0))
_timer.daemon = True
_timer.start()

from _paths import AGENT_DIR

BUDGET_PATH = AGENT_DIR / "session" / "context-budget.json" if AGENT_DIR else None


def classify_zone(used_pct):
    if used_pct < 40:
        return "fresh"
    if used_pct < 65:
        return "normal"
    return "tight"


def main():
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        return

    cw = payload.get("context_window")
    if not cw:
        return

    used_pct = cw.get("used_percentage", 0)
    remaining_pct = cw.get("remaining_percentage", 100)
    window_size = cw.get("context_window_size", 200000)
    usage = cw.get("current_usage") or {}
    input_tokens = usage.get("input_tokens", 0)

    zone = classify_zone(used_pct)

    budget = {
        "used_pct": round(used_pct, 1),
        "remaining_pct": round(remaining_pct, 1),
        "window_size": window_size,
        "input_tokens": input_tokens,
        "zone": zone,
        "updated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }

    # Only write if <agent>/session/ already exists — do NOT mkdir here.
    # The status line fires every prompt, even after factory reset.
    # Using mkdir would silently recreate the session dir when it shouldn't exist.
    if BUDGET_PATH and BUDGET_PATH.parent.is_dir():
        tmp = BUDGET_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(budget), encoding="utf-8")
        os.replace(str(tmp), str(BUDGET_PATH))

    # Status line output (always — status bar works regardless of agent state)
    print(f"CTX: {used_pct:.0f}% [{zone}]")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # Status line must not crash
