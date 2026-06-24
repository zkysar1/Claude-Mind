#!/usr/bin/env python3
"""Stale-Read Gate CLI — thin shim over gates.stale_read.evaluate().

Sub-task 3 of g-115-410. Invoked from `aspirations.py:cmd_add_goal` when the
new goal cites a `parent_goal` field. Logic lives in
`core/scripts/gates/stale_read.py` (PR 7c/1 extraction); this file is a CLI
adapter for the legacy invocation contract.

Exit codes:
  exit 0 — pass (fresh read, legacy goal, no-parent skip, or override)
  exit 1 — block (stale read with a tracked receipt, no override)
  exit 2 — fail-open (parent not found, no read receipt / never-read, bad input, etc.)

Override: `--override-stale-read "<justification>"` exits 0 and appends to
`world/stale-read-overrides.jsonl` for audit.

Input (stdin JSON):  {"parent_goal": "g-NNN-NN", "agent": "alpha"}
Output (stdout JSON): see gates.stale_read.evaluate() docstring.

The `_fail_open` field in the gate's return dict is used here to map to
exit 2, then stripped from the printed JSON so legacy callers see the
exact same payload they always did.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

#  / : force utf-8 on stdin/stdout/stderr (covers Windows
# cp1252 fallback when callers bypass the _platform.sh PYTHONIOENCODING=utf-8
# shim). Closes acceptance (4) of  — stdin-ingest sweep.
from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()

# Force UTF-8 to match sibling gates (avoids cp1252 fallback on Windows when
# callers bypass the _platform.sh PYTHONIOENCODING shim).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from _paths import WORLD_DIR, AGENT_DIR  # noqa: E402
from gates.stale_read import evaluate  # noqa: E402


def _parse_input() -> dict | None:
    """Read stdin JSON when not a TTY. Returns None on parse error or empty."""
    if sys.stdin.isatty():
        return None
    raw = sys.stdin.read().strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--override-stale-read",
        metavar="JUSTIFICATION",
        help="Bypass the gate. Appended to world/stale-read-overrides.jsonl",
    )
    p.add_argument(
        "--output", choices=("json", "human"), default="json",
        help="Output format (default: json)",
    )
    args = p.parse_args()

    payload = _parse_input()
    if payload is None:
        # Empty/invalid input → fail-open per project policy.
        out = {
            "would_block": False,
            "reason": "no input or invalid JSON — fail-open",
            "parent_goal": None,
            "parent_last_modified": None,
            "agent_last_read": None,
            "override_applied": None,
        }
        if args.output == "json":
            print(json.dumps(out))
        else:
            print(f"FAIL-OPEN: {out['reason']}")
        return 2

    # CLI carries the env-derived agent fallback so the daemon-safe module
    # stays env-free. payload.agent wins; env fallback handles direct CLI
    # invocations that don't set agent in stdin.
    result = evaluate(
        payload,
        override=args.override_stale_read,
        agent_name=os.environ.get("MIND_AGENT") or "",
        world_dir=WORLD_DIR,
        agent_dir=AGENT_DIR,
    )

    # Strip the daemon-only _fail_open flag before printing — preserves
    # the legacy JSON shape callers parse.
    fail_open = result.pop("_fail_open", False)

    if args.output == "json":
        print(json.dumps(result))
    else:
        verdict = "BLOCK" if result["would_block"] else "PASS"
        print(f"{verdict}: {result['reason']}")

    if fail_open:
        return 2
    if result["would_block"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
