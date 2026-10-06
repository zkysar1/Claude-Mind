#!/usr/bin/env python3
"""Ratchet the count of goal-field names that are neither registered nor declared strays.

Item 3 of g-115-6573. Item 1 shipped the write-time allowlist gate (a new field
is refused unless registered in `_goal_fields.py` or explicitly overridden); item
2 folded the invisible stray content into `description`. This is the detector
that makes the gate's continued effectiveness OBSERVABLE — a gate nobody measures
is indistinguishable from a gate that has been bypassed.

WHAT IS RATCHETED, AND WHY IT IS NOT THE OBVIOUS THING.

  RATCHETED: `undeclared_names` — the number of distinct top-level field names on
  WORLD-queue goals that are in neither `GOAL_KNOWN_FIELDS` nor
  `GOAL_STRAY_FIELDS`. Target 0. A name lands there when a writer set it without
  passing the allowlist gate (an audited override, or an internal writer that
  skips the gate), which is exactly what item 1's gate exists to stop, so a rise
  is the signal worth waking up for. Registering a field on purpose does NOT move
  it. The baseline stores the count only; the NAMES are printed on every run,
  because a name list inside the baseline would be a cache of one registry
  version (guard-5539).

  WHY NOT `distinct_keys`, WHICH THIS RATCHET GATED UNTIL 2026-10-03 (baseline
  key `goal_field_distinct_keys`). Two independent defects, both measured:
  (1) It counted the world queue UNIONED with the BOUND AGENT's private queue,
  because `aspirations-query.sh` returns that union. One box, one store, one
  minute, only MIND_AGENT varied: 148 / 149 / 150 / 148 / 149 — the verdict
  tracked WHICH AGENT RAN IT, and against a baseline merged by MIN across boxes
  no agent could ever read stable (guard-6519, guard-5058, rb-6062).
  (2) It rose whenever a field was registered on purpose, and a baseline that can
  only shrink turns the first legitimate registration into a permanent WARN.
  The retired key is left in `meta/audit-baselines.yaml` and this script never
  writes it: boxes still on the old code keep writing it until they upgrade, and
  recording a new low under a shared key from upgraded code pins every
  un-upgraded peer at "regressed" (guard-6633, guard-7161).

  THE POPULATION. `aspirations-query.sh` is union-only (it has no --source flag),
  so the split is made here on the per-row `source` key. The ratcheted number
  reads WORLD rows only, the one population every agent sees identically. The
  bound agent's private queue is reported beside it (`agent_queue`) and never
  moves the verdict. `read_from` is a query-time marker the endpoint stamps on
  agent rows, not a stored field (rb-12748), so it is excluded wherever it shows
  up.

  THE REGISTRY IS CODE, THE RECORDS ARE DATA. A record reaches every box through
  the shared store at once; `_goal_fields.py` reaches a box only when that box
  merges origin. A box that is behind origin on that file reads a freshly
  registered name as undeclared until it pulls, and the regression message says so.

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
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _paths import META_DIR  # type: ignore  # noqa: E402
from _fileops import locked_modify_yaml  # type: ignore  # noqa: E402
from _ratchet_delta import box_name, describe, since_last_reading  # type: ignore  # noqa: E402
from _goal_fields import GOAL_KNOWN_FIELDS, GOAL_STRAY_FIELDS  # noqa: E402
from _runtime_bash import bash_cmd  # noqa: E402
from aspirations import VALID_GOAL_STATUSES  # noqa: E402

KEY = "goal_field_undeclared_names"
BASELINES_PATH = Path(META_DIR) / "audit-baselines.yaml"

# Names the query endpoint stamps on a row at read time. They are never stored,
# and they appear only under a binding whose runner claim this box does not
# hold, so counting one makes the number depend on which agent asked (rb-12748).
QUERY_TIME_MARKERS = frozenset({"read_from"})


def _undeclared(name: str) -> bool:
    """True when nothing has classified `name`: not registered, not a declared
    stray, not a query-time marker."""
    return (name not in GOAL_KNOWN_FIELDS and name not in GOAL_STRAY_FIELDS
            and name not in QUERY_TIME_MARKERS)


def _widest_first(item: tuple) -> tuple:
    """Sort key for a (name, count) pair: widest first, ties by name."""
    return (-item[1], item[0])


def _by_count(names: dict) -> dict:
    """Name -> carrier count, widest first. Ties break by name so successive runs
    stay diffable."""
    return dict(sorted(names.items(), key=_widest_first))


def _names(counts: dict, cap: int = 12) -> str:
    """`name(count), ...`, capped with an explicit remainder so a long list never
    reads as complete (guard-1760). --json carries every name."""
    shown = list(counts.items())[:cap]
    return (", ".join(f"{n}({c})" for n, c in shown)
            + (f", ... and {len(counts) - len(shown)} more (--json for all)"
               if len(counts) > len(shown) else ""))


def _census() -> dict:
    """Count goal-field names over EVERY valid status, split by queue of origin.

    The ratcheted number reads the WORLD rows only (see the module docstring); the
    bound agent's own rows are tallied beside it and reported, never gated.
    """
    # queue -> {"seen": goal ids, "names": field name -> ids of the goals carrying it}.
    # Names are read from EVERY row, never first-seen-wins by id: a goal can sit in
    # two statuses at once (measured 2026-10-03: 2 world ids, pending + superseded),
    # and a name that only the second row carries would be invisible to the census
    # while the write-time gate it audits covers both rows.
    queues = {"world": {"seen": set(), "names": {}},
              "agent": {"seen": set(), "names": {}}}
    rows = stamped = 0
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
            rows += 1
            origin = goal.get("source")
            stamped += origin is not None
            queue = queues["world" if origin == "world" else "agent"]
            gid = goal.get("id")
            queue["seen"].add(gid)
            for field in goal:
                queue["names"].setdefault(field, set()).add(gid)
    if rows and not stamped:
        # Without the per-row `source` stamp every row lands in the agent queue,
        # the world population reads zero, and the run would report a clean
        # "store unreachable" instead of the shape change that actually happened
        # (guard-2298).
        raise RuntimeError(
            f"aspirations-query rows carry no `source` key ({rows} rows): cannot "
            f"split the world queue from the bound agent's queue")
    world = {n: len(ids) for n, ids in queues["world"]["names"].items()}
    agent = {n: len(ids) for n, ids in queues["agent"]["names"].items()}
    undeclared = {n: c for n, c in world.items() if _undeclared(n)}
    strays = {n: c for n, c in world.items() if n in GOAL_STRAY_FIELDS}
    return {
        "goals_scanned": len(queues["world"]["seen"]),
        # REPORTED, NOT RATCHETED: registering a field on purpose raises it, so it
        # cannot be a one-way gate.
        "distinct_keys": len(world.keys() - QUERY_TIME_MARKERS),
        "undeclared_names": len(undeclared),
        # The NAMES, not just how many there are. A count with no identities
        # cannot be acted on. `strays` is the curated half this ratchet REPORTS
        # rather than gates (see the module docstring), so reporting is its whole
        # job. Widest first keeps the reader's eye on the widespread ones.
        "undeclared": _by_count(undeclared),
        "stray_names": len(strays),
        "stray_occurrences": sum(strays.values()),
        "strays": _by_count(strays),
        "statuses_scanned": len(VALID_GOAL_STATUSES),
        "agent_queue": {
            "goals": len(queues["agent"]["seen"]),
            "undeclared": _by_count({n: c for n, c in agent.items() if _undeclared(n)}),
        },
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
        # POSITIVE CONTROL. Zero world goals means the query failed or the store
        # moved, never a healthy empty fleet — and an unreachable store reads
        # `undeclared_names: 0`, which is the GOOD value, so without this guard a
        # dead query records a clean all-clear (rb-245: verify the population
        # exists before believing a zero).
        msg = ("aspirations-query returned no world goals across any status — the "
               "store is unreachable or empty; refusing to record a clean zero")
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
        cur = current["undeclared_names"]

        if prior is None:
            verdict, new_baseline = "seeded", cur
            message = (f"Seeded baseline at {cur} undeclared goal-field name(s) across "
                       f"{current['goals_scanned']} world goal(s). Future runs compare against it.")
        elif cur > prior:
            verdict, new_baseline = "regressed", prior  # never raise the baseline
            message = (
                f"WARN: undeclared goal-field names grew from baseline {prior} to {cur} "
                f"(+{cur - prior}): {_names(current['undeclared'])}. These are names on "
                f"world goals that are neither registered in GOAL_KNOWN_FIELDS nor "
                f"declared in GOAL_STRAY_FIELDS, so a writer set them without passing "
                f"the g-115-6573 allowlist gate. Every agent reads this same world "
                f"population, so the number does not depend on who ran the check. "
                f"Resolve each name in core/scripts/_goal_fields.py: register it if a "
                f"writer sets it on purpose, declare it in GOAL_STRAY_FIELDS if it is "
                f"drift. If this box is behind origin, pull first and re-run "
                f"(`git log --oneline HEAD..origin/main -- core/scripts/_goal_fields.py`): "
                f"a name registered upstream reads as undeclared until then. DO NOT "
                f"RE-SEED: the assignment above pins new_baseline to `prior` on "
                f"purpose, and coordination_merge.merge_audit_baselines merges "
                f"`baseline` by MIN (one-way shrink, never grow), so a hand re-seed "
                f"verifies STABLE locally and is silently reverted at the next merge. "
                f"It is advisory: it gates nothing.")
        elif cur < prior:
            verdict, new_baseline = "ratcheted", cur
            message = (f"OK: undeclared goal-field names shrank from baseline {prior} to "
                       f"{cur} (-{prior - cur}). Baseline lowered.")
        else:
            verdict, new_baseline = "stable", prior
            message = f"OK: undeclared goal-field names stable at baseline {cur}."

        history = entry.get("history") or []
        # Read this box's previous reading BEFORE the new row is appended.
        host = box_name()
        captured["since"] = since_last_reading(history, cur, host, now_iso)
        history.append({
            "recorded_at": now_iso,
            "drift_total": cur,
            "verdict": verdict,
            "goals_scanned": current["goals_scanned"],
            "stray_occurrences": current["stray_occurrences"],
            "hostname": host,
        })
        baselines[KEY] = {
            "baseline": new_baseline,
            "last_recorded": now_iso,
            "last_verdict": verdict,
            # Named so a future reader cannot mistake WHICH number is gated, or over
            # WHICH population (guard-7085). The stray count, the name total and the
            # bound agent's queue are deliberately NOT ratcheted — see the module
            # docstring.
            "ratcheted_metric": "undeclared_names",
            "scope": "source=world",
            "reported_not_ratcheted": "stray_occurrences, distinct_keys, agent_queue",
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
                        message=f"current={current['undeclared_names']} "
                                f"prior_baseline={prior} (no write)",
                        since=since_last_reading(entry.get("history"),
                                                 current["undeclared_names"],
                                                 box_name(), now_iso))
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
        "ratcheted_metric": "undeclared_names",
        "message": captured["message"],
        "since_last_reading": captured.get("since"),
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"[goal-field-census-ratchet] {captured['verdict'].upper()}: "
              f"{captured['message']}")
        print(f"  world goals={current['goals_scanned']} undeclared="
              f"{current['undeclared_names']} distinct_keys={current['distinct_keys']} "
              f"strays={current['stray_names']} name(s)/"
              f"{current['stray_occurrences']} occurrence(s) "
              f"[only undeclared is ratcheted, the rest is reported — see --help]")
        # Name them. "17 stray name(s)" with no identities is a number nobody can
        # act on. Capped with an explicit remainder so the line stays readable and
        # never implies it showed everything (guard-1760: a tool must not silently
        # truncate and read as complete). --json carries all.
        if current["undeclared"]:
            print("  undeclared fields: " + _names(current["undeclared"]))
        if current["strays"]:
            print("  stray fields: " + _names(current["strays"]))
        agent = current["agent_queue"]
        print(f"  bound-agent queue: {agent['goals']} goal(s), "
              f"{len(agent['undeclared'])} undeclared name(s)"
              + (": " + _names(agent["undeclared"]) if agent["undeclared"] else "")
              + " [reported, NOT ratcheted]")
        if captured.get("since"):
            print(f"  since last reading: {describe(captured['since'])}")

    if os.environ.get("VERIFY_LEARNING_DRIFT_HARD_GATE") == "1":
        return 1 if captured["verdict"] == "regressed" else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
