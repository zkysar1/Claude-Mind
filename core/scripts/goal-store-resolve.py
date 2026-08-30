#!/usr/bin/env python3
"""goal-store-resolve.py — which queue holds a goal, and does it carry blocker evidence?

Called by iteration-close.sh at every phase entry so `--source` is DERIVED from the
goal id instead of demanded from the caller, and by do_verify before a
`--status blocked` write so the refusal comes with a shell-level remedy.

WHY (measured 2026-08-30, coach@zc-03, small-model Bodies): a Body that had
correctly diagnosed a human-gated block (a third-party API returning 403 until
the operator re-authorizes the app) tried `iteration-close.sh --phase verify
--status blocked` FOUR times — with `--source external` (not a store), then
`--source agent` (the wrong store), then `--source world` (right store, but the
daemon refused `blocker_ref_required_for_blocked_status` with a remedy that
names an HTTP header). Every recovery hint parroted the Body's own invalid
`--source` back as the retry command. The goal never recorded its block.

    goal-store-resolve.py --goal <id> [--source <given>]
        stdout line 1: the resolved store (world|agent); line 2 (optional): a
        note when the resolved store differs from the given one.
        rc 0 resolved | 1 refused (stderr says why) | 2 unknown (probe
        unavailable — daemon down or an id with no derivable aspiration): the
        caller keeps its own value.
    goal-store-resolve.py --goal <id> --source <store> --blocker-evidence
        rc 0 the goal carries blocker_ref or a non-empty blocked_by |
        1 it carries neither | 2 unknown.

`decide()` is pure and branch-tested; the probe is the daemon-only
aspirations-read.sh wrapper (never a direct store read — guard-996).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from _runtime_bash import bash_cmd  # noqa: E402  guard-580/581: never a bare "bash" argv[0]

STORES = ("world", "agent")


def decide(goal_id: str, given: str | None, holders: list[str] | None):
    """(store, note, error). `holders` = stores that hold the goal, or None when the
    probe was unavailable (unknown, not absent)."""
    given = (given or "").strip()
    known = holders is not None
    holders = [s for s in STORES if holders and s in holders]
    if given in STORES:
        if not known or given in holders:
            return given, None, None
        if holders:
            return (holders[0],
                    f"--source {given} does not hold {goal_id}; it lives in the "
                    f"{holders[0]} queue — using --source {holders[0]}", None)
        return None, None, f"{goal_id} was found in neither queue (probed world and agent)"
    if holders:
        why = f"('{given}' is not a store — only world|agent are)" if given else "(--source omitted)"
        return holders[0], f"resolved --source {holders[0]} from the goal id {why}", None
    if given:
        tail = ("could not resolve it from the goal id either" if not known
                else f"{goal_id} was found in neither queue")
        return None, None, f"--source must be world or agent (got '{given}'), and {tail}"
    if not known:
        return None, None, "--source omitted and it could not be resolved from the goal id"
    return None, None, f"--source omitted and {goal_id} was found in neither queue"


def has_blocker_evidence(record: dict) -> bool:
    if str(record.get("blocker_ref") or "").strip():
        return True
    blocked_by = record.get("blocked_by")
    return isinstance(blocked_by, list) and len(blocked_by) > 0


def _read_aspiration(store: str, asp_id: str) -> dict | None:
    """The aspiration record from one store via the daemon wrapper, or None when
    that store did not answer with one (unreachable, absent, error payload)."""
    try:
        proc = subprocess.run(bash_cmd(SCRIPT_DIR / "aspirations-read.sh", "--source", store, "--id", asp_id),
                              capture_output=True, text=True, timeout=60)
    except Exception:  # noqa: BLE001
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return None
    rec = data if isinstance(data, dict) else (data[0] if data else None)
    if not isinstance(rec, dict) or "goals" not in rec:
        return None
    return rec


def probe(goal_id: str):
    """({store: goal-record}, known) — known=False when no store answered at all."""
    m = re.match(r"^g-(\d+)-", goal_id or "")
    if not m:
        return {}, False
    asp_id = f"asp-{m.group(1)}"
    found, answered = {}, False
    for store in STORES:
        rec = _read_aspiration(store, asp_id)
        if rec is None:
            continue
        answered = True
        for g in rec.get("goals") or []:
            if isinstance(g, dict) and g.get("id") == goal_id:
                found[store] = g
                break
    return found, answered


def main(argv: list[str]) -> int:
    goal_id, given, evidence = "", None, False
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--goal" and i + 1 < len(argv):
            goal_id = argv[i + 1]; i += 2
        elif a == "--source" and i + 1 < len(argv):
            given = argv[i + 1]; i += 2
        elif a == "--blocker-evidence":
            evidence = True; i += 1
        else:
            i += 1
    if not goal_id:
        print("goal-store-resolve: --goal required", file=sys.stderr)
        return 2
    found, answered = probe(goal_id)
    if evidence:
        store = given if given in STORES else (sorted(found)[0] if found else None)
        rec = found.get(store) if store else None
        if rec is None:
            return 2
        return 0 if has_blocker_evidence(rec) else 1
    store, note, error = decide(goal_id, given, list(found) if answered else None)
    if error:
        print(error, file=sys.stderr)
        return 1
    if store is None:
        return 2
    print(store)
    if note:
        print(note)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
