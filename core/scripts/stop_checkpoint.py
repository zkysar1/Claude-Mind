#!/usr/bin/env python3
"""Stop Checkpoint — persistent sentinel for graceful-stop interruption recovery.

Problem (FW-11, g-317-09 / charlie#3): when autocompact interrupts the
graceful-stop handler (.claude/skills/aspirations-graceful-stop) mid-sequence
— most commonly during the long D4 consolidate phase — the stop is left
half-finished. D1 already flipped agent-state to IDLE, so the aspirations loop
bails at its Phase -1.5 state gate (IDLE) and never re-detects stop-requested
(which D3 also clears). The half-finished stop strands: consolidation/handoff
incomplete (learning lost), agent-mode stuck at autonomous (D7 never ran),
loop_state lingering.

Neither iteration-checkpoint.json (GS-1 deletes it) nor stop-requested (D3
clears it) survives the most common interruption window (D4), so a dedicated
persistent sentinel is required. This module manages that sentinel
(agents/<agent>/session/stop-checkpoint.json): written at graceful-stop entry
(GS-0), preserved across resume re-entries, deleted only at clean completion
(D7.1). Its mere PRESENCE means "a graceful stop is in progress / was
interrupted" — the unambiguous detection signal for --resume (guard-348
natural-gate: presence IS the signal, no enabled: boolean).

The D1-D7 phases are already largely idempotent (set-IDLE, clear-signal,
consolidation-precheck FAST-when-done, rm -f, set-mode), so --resume re-runs
the sequence rather than tracking fine-grained phase progress. resume_count
is a circuit breaker (cap MAX_RESUME_ATTEMPTS): if a stop is interrupted and
re-attempted that many times without clean completion, auto-resume stops and
the condition is surfaced for manual intervention (mirrors recovery-gate's
recovery-failed-permanent breaker).

Subcommands:
  write   --target-mode <assistant|reader>   write/refresh the checkpoint
  read                                        print checkpoint JSON or null
  resume-needed                               print JSON; exit 0 if a stop is
                                              in progress (resume needed), 1 if not
  clear                                       delete the checkpoint (D7.1)

Output: JSON to stdout on EVERY exit path (guard-614). Exit codes:
  resume-needed: 0 = resume needed, 1 = not needed / breaker tripped, 2 = error
  write/read/clear: 0 = ok, 2 = error

Cross-references:
  - g-317-09 — originating Idea goal (FW-11, charlie#3)
  - rb-428 — sentinel-lifecycle pattern (write-at-start / clear-at-end)
  - guard-348 — natural-gate via presence (no enabled: boolean)
  - .claude/skills/aspirations-graceful-stop/SKILL.md — the handler that owns the lifecycle
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _paths import agent_state_dir  # type: ignore  # noqa: E402

CHECKPOINT_NAME = "stop-checkpoint.json"
VALID_MODES = ("assistant", "reader")
MAX_RESUME_ATTEMPTS = 3


def _now_iso() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat()


def _agent_name() -> str:
    name = (os.environ.get("MIND_AGENT") or "").strip()
    if not name:
        raise SystemExit(
            "MIND_AGENT not set — stop-checkpoint cannot resolve the agent"
        )
    return name


def _checkpoint_path(session_dir: Path) -> Path:
    return session_dir / CHECKPOINT_NAME


def read_checkpoint(session_dir: Path) -> Optional[Dict[str, Any]]:
    """Return the parsed checkpoint dict, or None if absent/unreadable."""
    path = _checkpoint_path(session_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return None


def write_checkpoint(session_dir: Path, target_mode: str) -> Dict[str, Any]:
    """Write/refresh the checkpoint. Idempotent: preserves stop_started_at and
    increments resume_count when a checkpoint already exists (re-entry)."""
    if target_mode not in VALID_MODES:
        # GS-0 only ever produces assistant/reader, but never crash on a bad
        # value — default to assistant (the post-stop default per CLAUDE.md).
        target_mode = "assistant"
    path = _checkpoint_path(session_dir)
    existing = read_checkpoint(session_dir) or {}
    now = _now_iso()
    is_reentry = bool(existing)
    record = {
        "stop_started_at": existing.get("stop_started_at") or now,
        "target_mode": target_mode,
        "last_updated": now,
        "resume_count": int(existing.get("resume_count", 0)) + (1 if is_reentry else 0),
    }
    session_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    tmp.replace(path)  # atomic on POSIX and Windows (os.replace)
    return record


def clear_checkpoint(session_dir: Path) -> bool:
    """Delete the checkpoint. Idempotent — returns True if a file existed."""
    path = _checkpoint_path(session_dir)
    existed = path.exists()
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return existed


def resume_info(session_dir: Path) -> Dict[str, Any]:
    """Compute whether a graceful-stop resume is needed.

    resume_needed is False when no checkpoint exists OR the resume_count
    breaker has tripped (>= MAX_RESUME_ATTEMPTS) — the latter is surfaced
    so a persistently-failing stop is escalated rather than auto-looped."""
    cp = read_checkpoint(session_dir)
    if cp is None:
        return {"resume_needed": False, "reason": "no_checkpoint", "checkpoint": None}
    count = int(cp.get("resume_count", 0))
    if count >= MAX_RESUME_ATTEMPTS:
        return {
            "resume_needed": False,
            "reason": "breaker_tripped",
            "resume_count": count,
            "max_resume_attempts": MAX_RESUME_ATTEMPTS,
            "checkpoint": cp,
        }
    return {
        "resume_needed": True,
        "reason": "checkpoint_present",
        "resume_count": count,
        "target_mode": cp.get("target_mode"),
        "stop_started_at": cp.get("stop_started_at"),
        "checkpoint": cp,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Manage the graceful-stop checkpoint sentinel."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("write")
    w.add_argument("--target-mode", required=True)
    sub.add_parser("read")
    sub.add_parser("resume-needed")
    sub.add_parser("clear")
    args = ap.parse_args(argv)

    agent = _agent_name()
    session_dir = agent_state_dir(agent)

    if args.cmd == "write":
        rec = write_checkpoint(session_dir, args.target_mode)
        print(json.dumps({"ok": True, "action": "write", **rec}))
        return 0
    if args.cmd == "read":
        cp = read_checkpoint(session_dir)
        print(json.dumps(cp) if cp is not None else "null")
        return 0
    if args.cmd == "resume-needed":
        info = resume_info(session_dir)
        info["agent"] = agent
        info["now"] = _now_iso()
        print(json.dumps(info))
        return 0 if info["resume_needed"] else 1
    if args.cmd == "clear":
        existed = clear_checkpoint(session_dir)
        print(json.dumps({"ok": True, "action": "clear", "existed": existed}))
        return 0
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # never crash the stop sequence
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(2)
