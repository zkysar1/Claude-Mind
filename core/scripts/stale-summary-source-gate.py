#!/usr/bin/env python3
"""CLI half of the stale-narrative-source gate ().

Predicate + full rationale: core/scripts/gates/stale_summary_source.py.
This file owns ONLY the impure part — statting the file and resolving the
reference time — so the predicate stays pure and directly testable.

Invoke with `py -3` / `python3`, NEVER `bash` (guard-156).

    py -3 core/scripts/stale-summary-source-gate.py \
        --path <file> [--goal <id>] [--source world|agent] \
        [--override-stale-source "<justification>"]

EXIT CODES — the 3 is load-bearing, do not "simplify" it to 1:
    0  allow (including every not-judgeable branch)
    3  BLOCKED: a real, intended refusal
    anything else  the gate could not run (missing file, import error,
                   uncaught exception -> Python's own 1; argparse -> 2)

Python exits 1 on an uncaught exception, so a block code of 1 is
INDISTINGUISHABLE from a crash. Callers must fail OPEN on any non-3 non-zero:
a gate that cannot run must never refuse a write. Shipping it as 1 broke 50
tests in the full suite — every one of them stages a tmp `core/scripts` without
this file, so `py -3 <missing>` exited 1 and the shell read it as a refusal.
That is the mirror of guard-3803: there a fail-open handler swallows a real
refusal; here a fail-closed caller manufactures one.

Always prints one JSON line to stdout; the human-readable refusal goes to
stderr so a shell caller can surface it without parsing.

REFERENCE RESOLUTION, in order:
  1. the goal's `claimed_at`  — per-unit, the moment this work began
  2. the session's start      — via session_artifacts_count.read_session_start(),
                                the declared single source of truth for it.
                                MEASURED WEAK, and say so rather than implying
                                parity: an autonomous session runs for days
                                (5d 4h when this gate was built), so almost
                                every candidate file postdates it and the
                                fallback returns "fresh". It is a floor, not a
                                substitute for `claimed_at` — a goal with no
                                claim gets far less protection, which is the
                                honest state, not a tuning opportunity.
  3. none                     — the predicate then returns not-judgeable

Deliberately NOT a third fallback (guard: session_artifacts_count's own header
records that a plausible-looking fabricated cutoff silently disabled a gate).
Unresolvable means unresolvable; the predicate reports it as its own branch
rather than inventing a time that would make every write look fresh.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _runtime_bash import bash_cmd  # noqa: E402
from gates.stale_summary_source import evaluate  # noqa: E402


def _iso_to_epoch(text):
    """Parse a naive ISO timestamp to epoch seconds; None when unparseable."""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return datetime.fromisoformat(text.strip()).timestamp()
    except ValueError:
        # A malformed stamp is NOT a stale source. Fall through to the next
        # reference rather than refusing on a parse failure we did not cause.
        return None


def _claimed_at_epoch(goal_id, source):
    """Read the goal's claimed_at. Returns (epoch|None, error|None).

    Uses aspirations-query.sh --goal-field id --full, the same reader
    closure-evidence-write.sh:155 already uses for this record — one reader
    shape for one record, so the two cannot disagree about what they see.
    """
    if not goal_id:
        return None, None
    # `source` is accepted by this module's CLI (both call sites have $SOURCE
    # in hand) and deliberately NOT forwarded: aspirations-query.sh is
    # UNION-ONLY BY DESIGN and has no --source flag (). It refuses
    # the flag rc=2 rather than discarding it, which is what caught this on the
    # gate's first live control — the read failed, the reference silently fell
    # back to session_start, and the red control did not fire. Do not "restore"
    # the flag; the union already contains both queues.
    del source
    # bash_cmd, never a bare "bash" argv[0]: that resolves to the System32 WSL
    # stub on win32 and can hang forever (guard-580), and it passes the script
    # path as_posix() because bash strips the backslashes of a str(WindowsPath)
    # (guard-581). Caught by the pre-commit gate on this very change.
    cmd = bash_cmd(
        SCRIPT_DIR / "aspirations-query.sh",
        "--goal-field", "id", str(goal_id), "--full",
    )
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"query-failed: {exc}"
    if out.returncode != 0:
        return None, f"query-rc-{out.returncode}"
    text = (out.stdout or "").strip()
    if not text:
        # Empty output is a MALFUNCTION, not "no claim" (guard-2298). Report it
        # so the caller can tell a broken read from an unclaimed goal.
        return None, "query-empty-output"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"query-unparseable: {exc}"
    records = data if isinstance(data, list) else [data]
    for rec in records:
        if not isinstance(rec, dict):
            continue
        goals = rec.get("goals") if isinstance(rec.get("goals"), list) else [rec]
        for goal in goals:
            if isinstance(goal, dict) and goal.get("id") == goal_id:
                return _iso_to_epoch(goal.get("claimed_at")), None
    return None, None


def _session_start_epoch():
    """Epoch of session_start via its declared SSOT reader; None if unset."""
    try:
        from session_artifacts_count import read_session_start
    except ImportError as exc:
        return None, f"ssot-import-failed: {exc}"
    try:
        return _iso_to_epoch(read_session_start()), None
    except Exception as exc:  # noqa: BLE001 — reported, never swallowed
        return None, f"ssot-read-failed: {exc}"


def main(argv=None):
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--path", required=True, help="the --summary-file path to judge")
    ap.add_argument("--goal", default="", help="goal id, for the claimed_at reference")
    ap.add_argument("--source", default="", choices=["", "world", "agent"])
    ap.add_argument("--override-stale-source", default="",
                    help="justification; allows the write and audits the override")
    ap.add_argument("--caller", default="cli", help="call-site label for telemetry")
    args = ap.parse_args(argv)

    try:
        mtime = os.stat(args.path).st_mtime
    except OSError:
        mtime = None

    reference, kind, ref_error = None, "none", None
    if mtime is not None:
        reference, ref_error = _claimed_at_epoch(args.goal, args.source)
        kind = "claimed_at" if reference is not None else "none"
        if reference is None:
            reference, ss_error = _session_start_epoch()
            ref_error = ref_error or ss_error
            kind = "session_start" if reference is not None else "none"

    verdict = evaluate(args.path, mtime, reference, kind)
    verdict["goal_id"] = args.goal or None
    verdict["reference_error"] = ref_error

    override = args.override_stale_source.strip()
    decision = "noop"
    if verdict["blocked"]:
        decision = "override" if override else "block"

    try:
        import _gate_log
        _gate_log.log(
            "stale-summary-source-gate",
            decision,
            caller=args.caller,
            trigger_matched=verdict["decision_path"],
            payload=args.goal or args.path,
            override_reason=override or None,
            gate_error=ref_error,
            extra={
                "source_path": verdict["source_path"],
                "age_seconds": verdict["age_seconds"],
                "reference_kind": verdict["reference_kind"],
            },
        )
    except Exception as exc:  # noqa: BLE001
        # Telemetry must never decide the outcome. Surface the failure instead
        # of hiding it — a silent logging failure is how a gate's firing record
        # goes empty while the gate itself works.
        print(f"[stale-summary-source-gate] WARN: telemetry failed: {exc}",
              file=sys.stderr)

    verdict["decision"] = decision
    print(json.dumps(verdict))

    if decision == "block":
        print(f"BLOCKED: {verdict['message']}", file=sys.stderr)
        return 3          # dedicated: never Python's generic exception code
    if decision == "override":
        print(f"[stale-summary-source-gate] OVERRIDE on {args.goal or args.path}: "
              f"{override}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
