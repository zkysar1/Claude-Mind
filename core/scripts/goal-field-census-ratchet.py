#!/usr/bin/env python3
"""Ratchet the DISTINCT TOP-LEVEL GOAL-FIELD COUNT across the aspiration stores.

Item 3 of g-115-6573. Item 1 shipped the write-time allowlist gate (a new field
is refused unless registered in `_goal_fields.py` or explicitly overridden); item
2 folded the invisible stray content into `description`. This is the detector
that makes the gate's continued effectiveness OBSERVABLE — a gate nobody measures
is indistinguishable from a gate that has been bypassed.

WHAT IS RATCHETED, AND WHY IT IS NOT THE OBVIOUS THING.

  RATCHETED: `distinct_keys` — the number of distinct top-level field names in
  use across all goals. It may go DOWN, never UP. This is exactly what item 1's
  gate enforces at the write, so a rise means the gate was bypassed, overridden,
  or regressed, which is the signal worth waking up for.

  REPORTED BUT NOT RATCHETED: `stray_occurrences`. It is tempting to gate on
  "the stray count must fall", and that assertion is UNSATISFIABLE BY
  CONSTRUCTION today. Measured 2026-08-18: `aspirations.jsonl` is merge-protected
  by the COMMUTATIVE `merge_aspirations` handler, and under own-cloud
  `_merge_reconcile_put` GETs remote, merges and PUTs — so a key absent from a
  write and present remotely resolves to PRESENT, because a commutative merge
  cannot encode a deletion. A migration that pops 34 stray keys writes
  successfully and changes nothing. Ratcheting on a number no available write
  path can lower would produce a permanent WARN that everyone learns to ignore,
  which is worse than not measuring it (the field-level instance of guard-1816;
  see g-115-6486 for the record-level analysis and its REFUSED remedy).
  When a field tombstone lands, promote this to a ratcheted metric.

STATUS ENUMERATION IS LOAD-BEARING. The statuses come from
`aspirations.VALID_GOAL_STATUSES`, never a hand-written list. Measured while
building this: a six-status census (the obvious pending/in-progress/completed/
blocked/skipped/expired) MISSES `decomposed` and `superseded`, and undercounted
this very metric by 2 goals and 1 distinct key. A ratchet that undercounts drifts
its own baseline downward and then reports "regressed" the first time someone
counts correctly.

Usage:
  python goal-field-census-ratchet.py [--json] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _paths import META_DIR  # type: ignore  # noqa: E402
from _fileops import locked_modify_yaml  # type: ignore  # noqa: E402
from _goal_fields import GOAL_STRAY_FIELDS  # noqa: E402
from _runtime_bash import bash_cmd  # noqa: E402
from aspirations import VALID_GOAL_STATUSES  # noqa: E402

KEY = "goal_field_distinct_keys"
BASELINES_PATH = Path(META_DIR) / "audit-baselines.yaml"


def _census() -> dict:
    """Count distinct top-level goal-field names over EVERY valid status."""
    keys: set = set()
    strays: dict = {}
    seen: set = set()
    occurrences = 0
    for status in sorted(VALID_GOAL_STATUSES):
        proc = subprocess.run(
            bash_cmd("core/scripts/aspirations-query.sh",
                     "--goal-status", status, "--full"),
            capture_output=True, text=True, cwd=str(SCRIPT_DIR.parent.parent))
        try:
            goals = json.loads(proc.stdout or "[]")
        except json.JSONDecodeError:
            # A shape change here must NOT be laundered into a clean zero
            # (guard-2298): report the bytes so the failure is legible.
            raise RuntimeError(
                f"aspirations-query returned unparseable output for status "
                f"{status!r} ({len(proc.stdout)} bytes)")
        for goal in goals:
            gid = goal.get("id")
            if gid in seen:
                continue
            seen.add(gid)
            for field in goal:
                keys.add(field)
                if field in GOAL_STRAY_FIELDS:
                    strays[field] = strays.get(field, 0) + 1
                    occurrences += 1
    return {
        "goals_scanned": len(seen),
        "distinct_keys": len(keys),
        "stray_names": len(strays),
        "stray_occurrences": occurrences,
        # The NAMES, not just how many there are. `strays` was built here and
        # discarded, so every consumer learned "17 stray field name(s)" and had
        # no way to find out which — a count with no identities cannot be acted
        # on, and the strays are explicitly the half this ratchet REPORTS rather
        # than gates (see the module docstring), so reporting is its whole job.
        # Sorted by descending count then name: the reader wants the widespread
        # ones first, and a stable order keeps successive runs diffable.
        "strays": dict(sorted(strays.items(), key=lambda kv: (-kv[1], kv[0]))),
        "statuses_scanned": len(VALID_GOAL_STATUSES),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true", help="Emit JSON")
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute and report without touching the baseline file")
    args = ap.parse_args()

    try:
        current = _census()
    except Exception as e:
        print(f"ERROR: goal-field census failed: {e}", file=sys.stderr)
        return 2

    if current["goals_scanned"] == 0:
        # POSITIVE CONTROL. Zero goals means the query failed or the store moved,
        # never a healthy empty fleet — and `distinct_keys: 0` would seed a
        # baseline of 0 and then flag every subsequent honest run as "regressed"
        # (rb-245: verify the population exists before believing a zero).
        msg = ("aspirations-query returned no goals across any status — the "
               "store is unreachable or empty; refusing to seed a baseline of 0")
        print(json.dumps({"verdict": "skipped", "message": msg}, indent=2)
              if args.json else f"[goal-field-census-ratchet] SKIPPED: {msg}")
        return 0

    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    captured: dict = {}

    def _modify(baselines):
        # Read the prior baseline INSIDE the lock: sibling ratchets share this
        # file and this lock, and without the locked RMW two writers each ratchet
        # against an already-stale baseline and the second reverts the first.
        if not isinstance(baselines, dict):
            baselines = {}
        entry = baselines.get(KEY) or {}
        prior = entry.get("baseline")
        cur = current["distinct_keys"]

        if prior is None:
            verdict, new_baseline = "seeded", cur
            message = (f"Seeded baseline at {cur} distinct goal-field name(s) across "
                       f"{current['goals_scanned']} goal(s). Future runs compare against it.")
        elif cur > prior:
            verdict, new_baseline = "regressed", prior  # never raise the baseline
            message = (
                f"WARN: distinct goal-field names grew from baseline {prior} to {cur} "
                f"(+{cur - prior}). The g-115-6573 allowlist gate should have REFUSED "
                f"any unregistered name at the write, so a rise means it was bypassed "
                f"(--allow-new-field / X-Mind-Allow-New-Field, audited in "
                f"world/override-bypass-ledger.jsonl), legitimately extended in "
                f"_goal_fields.py, or regressed. Check the ledger FIRST — a deliberate "
                f"registration legitimately raises this number. DO NOT RE-SEED: the "
                f"assignment above pins new_baseline to `prior` on purpose, and "
                f"coordination_merge.merge_audit_baselines merges `baseline` by MIN "
                f"(one-way shrink, never grow), because audit-baselines.md names "
                f"growing a baseline on regression as THE anti-pattern that defeats "
                f"the ratchet. A hand re-seed therefore verifies STABLE locally and is "
                f"silently reverted at the next merge — measured 2026-08-31 (echo, "
                f"cc-03): 132->134 read STABLE, then 132 again minutes later. When the "
                f"growth is deliberate and ledger-confirmed, LEAVE THE BASELINE ALONE "
                f"and let this advisory stand until the schema shrinks back. It is "
                f"advisory: it gates nothing.")
        elif cur < prior:
            verdict, new_baseline = "ratcheted", cur
            message = (f"OK: distinct goal-field names shrank from baseline {prior} to "
                       f"{cur} (-{prior - cur}). Baseline lowered.")
        else:
            verdict, new_baseline = "stable", prior
            message = f"OK: distinct goal-field names stable at baseline {cur}."

        history = entry.get("history") or []
        history.append({
            "recorded_at": now_iso,
            "drift_total": cur,
            "verdict": verdict,
            "goals_scanned": current["goals_scanned"],
            "stray_occurrences": current["stray_occurrences"],
            "hostname": os.environ.get("HOSTNAME") or socket.gethostname(),
        })
        baselines[KEY] = {
            "baseline": new_baseline,
            "last_recorded": now_iso,
            "last_verdict": verdict,
            # Named so a future reader cannot mistake WHICH number is gated. The
            # stray count is deliberately NOT ratcheted — see the module docstring.
            "ratcheted_metric": "distinct_keys",
            "reported_not_ratcheted": "stray_occurrences",
            "history": history[-50:],
        }
        captured.update(verdict=verdict, new_baseline=new_baseline, message=message)
        return baselines

    if args.dry_run:
        entry = {}
        try:
            import yaml  # type: ignore
            if BASELINES_PATH.is_file():
                entry = (yaml.safe_load(BASELINES_PATH.read_text(encoding="utf-8"))
                         or {}).get(KEY) or {}
        except Exception:
            entry = {}
        prior = entry.get("baseline")
        captured.update(verdict="dry-run", new_baseline=prior,
                        message=f"current={current['distinct_keys']} "
                                f"prior_baseline={prior} (no write)")
    else:
        try:
            locked_modify_yaml(BASELINES_PATH, _modify, initial={})
        except Exception as e:
            print(f"WARN: could not persist baseline to {BASELINES_PATH}: {e}",
                  file=sys.stderr)
            # OVERWRITE, never setdefault. _modify runs INSIDE locked_modify_yaml
            # and populates `captured` before the write; if the write then fails
            # (disk full, conflict-retry exhausted, validation), setdefault is a
            # no-op and this would report the COMPUTED verdict as though it had
            # persisted. stderr is the only contradicting signal and no JSON
            # consumer reads it. A tool must not claim a write it did not make.
            computed = captured.get("verdict")
            captured["verdict"] = "error"
            captured["new_baseline"] = None
            captured["message"] = (
                f"baseline operation FAILED and nothing was persisted: {e}"
                + (f" (the computed verdict was '{computed}' — it did NOT "
                   f"take effect)" if computed else ""))

    result = {
        "verdict": captured["verdict"],
        "baseline": captured["new_baseline"],
        "current": current,
        "ratcheted_metric": "distinct_keys",
        "message": captured["message"],
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"[goal-field-census-ratchet] {captured['verdict'].upper()}: "
              f"{captured['message']}")
        print(f"  goals={current['goals_scanned']} distinct_keys="
              f"{current['distinct_keys']} strays={current['stray_names']} name(s)/"
              f"{current['stray_occurrences']} occurrence(s) "
              f"[strays reported, NOT ratcheted — see --help]")
        # Name them. "17 stray name(s)" with no identities is a number nobody can
        # act on, and reporting is this metric's entire job (it is deliberately
        # not gated). Capped at 12 with an explicit remainder so the line stays
        # readable and never implies it showed everything (guard-1760: a tool
        # must not silently truncate and read as complete). --json carries all.
        if current["strays"]:
            shown = list(current["strays"].items())[:12]
            print("  stray fields: "
                  + ", ".join(f"{n}({c})" for n, c in shown)
                  + (f", ... and {len(current['strays']) - len(shown)} more "
                     f"(--json for all)" if len(current["strays"]) > len(shown) else ""))

    if os.environ.get("VERIFY_LEARNING_DRIFT_HARD_GATE") == "1":
        return 1 if captured["verdict"] == "regressed" else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
