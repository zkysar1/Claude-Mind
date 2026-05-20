#!/usr/bin/env python3
"""Conclusion-Record — append a negative conclusion to the `conclusions` WM slot.

Resolves the phantom-read finding from signal-lifecycle-gate (2026-04-19):
the `conclusions` slot is declared in wm.py ARRAY_SLOTS, read by
aspirations-consolidate Step 2.7 (judgment-quality stats) and by
postcompact-restore/consolidation-precheck, but NO skill or script ever
appended to it. Step 2.7's judgment audit therefore always saw an empty
list and silently reported "no negative conclusions drawn this session"
even when the agent had just created multiple blockers.

Per `core/config/conventions/negative-conclusions.md` section "Recording
Conclusions", each entry must include:
  - The conclusion text
  - Evidence signals with weights (0 = silent/zero-info, 1 = real)
  - Which goals it blocks
  - A re-verify timestamp (30 min for blocking conclusions)

This script writes ONE entry per invocation by invoking wm.py append on
the conclusions slot. The blocker-create-gate has already structurally
validated the evidence (rule 2.55 in CREATE_BLOCKER), so the conclusion
written here is the same evidence-tested payload — no double-checking.

Contract
  stdin   JSON blocker_json the blocker gate already accepted, OR the same
          shape with `conclusion` instead of `failure_reason`.
  --blocks-goals <ids>   comma-separated goal IDs the conclusion blocks
  --reverify-minutes N   minutes until re-verify (default 30)
  --type <str>           conclusion type (default "negative")

Exit codes
  0 — appended successfully
  1 — wm.py append failed (AGENT_DIR unresolved, disk error, etc.)
  2 — stdin JSON invalid / required fields missing

Fail-quiet: if AGENT_DIR is not resolvable (NO_AGENT state, race, etc.)
the script exits 1 with a short stderr message but prints nothing to
stdout. Callers treat it as best-effort: a blocker still lands even if
the conclusion wasn't recorded. Never block blocker creation on this.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

# : force utf-8 on stdin/stdout/stderr (covers Windows cp1252 fallback
# when callers bypass the _platform.sh PYTHONIOENCODING=utf-8 shim).
from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()

from _paths import CORE_ROOT
from _gate_log import log as _gate_log

# Invoke wm.py directly via sys.executable rather than bashing the wrapper.
# The .sh wrapper works fine from interactive shells but MSYS bash gets
# confused by Windows absolute paths like C:/... when invoked through
# subprocess (the path form that survives subprocess is not the same form
# cygwin bash parses). sys.executable is portable and avoids the whole
# class of shell-path-translation bugs. Same discipline as blocker-recheck.py
# (see its INVARIANT comment on sys.executable vs bash subprocess).


def _slug(s: str, n: int = 24) -> str:
    """Trim to n chars, lowercase, kebab, no runs of hyphens."""
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return (s[:n] or "unspecified").rstrip("-")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Record a negative conclusion to the conclusions WM slot.",
    )
    ap.add_argument("--blocks-goals", default="",
                    help="Comma-separated goal IDs this conclusion blocks.")
    ap.add_argument("--reverify-minutes", type=int, default=30,
                    help="Minutes until the conclusion should be re-verified.")
    ap.add_argument("--type", default="negative",
                    help="Conclusion type (default: negative).")
    args = ap.parse_args()

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[conclusion-record] invalid JSON on stdin: {e}", file=sys.stderr)
        return 2

    # Accept either `conclusion` or `failure_reason` as the claim text, so
    # the caller can pipe the same blocker_json it sent to blocker-create-gate.
    claim = payload.get("conclusion") or payload.get("failure_reason", "")
    if not claim or not isinstance(claim, str):
        print(f"[conclusion-record] stdin missing 'conclusion' or "
              f"'failure_reason' (got keys: {sorted(payload.keys())})",
              file=sys.stderr)
        return 2

    evidence_in = payload.get("evidence", [])
    if not isinstance(evidence_in, list):
        print(f"[conclusion-record] 'evidence' must be a list", file=sys.stderr)
        return 2

    # Weight: the blocker-create-gate has already classified silent-flag
    # commands as zero-information. Apply the same rule here so Step 2.7's
    # avg_signals reflects only real evidence.
    silent_markers = ("-sf", "--silent", "--quiet", "2>/dev/null", "|| true")
    evidence = []
    silent_count = 0
    matched_markers = set()
    for e in evidence_in:
        if not isinstance(e, dict):
            continue
        cmd = e.get("command") or ""
        hits = [m for m in silent_markers if m in cmd]
        silent = bool(hits)
        if silent:
            silent_count += 1
            matched_markers.update(hits)
        evidence.append({
            "signal": cmd or e.get("tool") or e.get("evidence_type") or "unspecified",
            "weight": 0 if silent else 1,
            "evidence_type": e.get("evidence_type"),
        })

    # gate_id MUST match core/config/gates.yaml id.
    if not evidence:
        _decision = "noop"
        _trigger = None
    elif silent_count > 0:
        _decision = "block"
        _trigger = ",".join(sorted(matched_markers))
    else:
        _decision = "pass"
        _trigger = None
    _gate_log("conclusion-record-silent-flag", _decision,
              caller="conclusion-record.py:main",
              trigger_matched=_trigger,
              payload=claim[:200],
              extra={"silent_count": silent_count, "total_evidence": len(evidence)})

    blocks = [g.strip() for g in args.blocks_goals.split(",") if g.strip()]

    now = datetime.now()
    entry = {
        "id": f"conc-{now.strftime('%Y-%m-%dT%H-%M-%S')}-{_slug(claim)}",
        "created_at": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "type": args.type,
        "conclusion": claim,
        "evidence": evidence,
        "blocks_goals": blocks,
        "reverify_at": (now + timedelta(minutes=args.reverify_minutes))
                       .strftime("%Y-%m-%dT%H:%M:%S"),
        "outcome": None,
    }

    # INVARIANT: keep "wm.py", "append", "conclusions" on ONE physical line
    # below. signal-lifecycle-gate.py scans single lines for the
    # `wm.py ... append ... <slot>` pattern to confirm a writer exists.
    # A refactor that (a) splits the argv list across lines, or (b) hoists
    # "wm.py" into a variable, will hide this writer from the gate and
    # regress `conclusions` to phantom-read — the exact bug the gate exists
    # to catch.
    argv = [sys.executable, str(CORE_ROOT / "scripts" / "wm.py"), "append", "conclusions"]
    try:
        proc = subprocess.run(argv, input=json.dumps(entry), text=True,
                              capture_output=True, check=False,
                              encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"[conclusion-record] wm.py append invoke failed: {e}",
              file=sys.stderr)
        return 1

    if proc.returncode != 0:
        print(f"[conclusion-record] wm.py append exit {proc.returncode}: "
              f"{(proc.stderr or '').strip()[:200]}",
              file=sys.stderr)
        return 1

    print(json.dumps({"recorded": True, "id": entry["id"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
