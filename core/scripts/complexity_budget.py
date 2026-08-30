#!/usr/bin/env python3
# domain-leak-exempt: framework self-measurement infra — counts framework files, no domain strings.
"""Complexity budget — the subtractive gradient's instrument (Phase 2).

Part of the evaluative substrate; eval_harness.py is the keystone + in-code index of all seven.

WHY THIS EXISTS
---------------
The research pass's central first-principles finding (the "complexity ratchet", B1): the
framework's self-improvement gradient is ADDITIVE-only. Every incident yields a new gate / rule /
guardrail, enforced by four layers; the opposing force (retire / consolidate) is only advisory.
Nothing measures the carrying cost of the defense portfolio AS A WHOLE, so it grows unbounded
and becomes the system's largest maintainability risk.

This module is the missing measurement: a cheap, deterministic count of the framework's
complexity surface (gates, rules, skills, scripts, orchestrator size, config size), appended to
a ledger so the trend is visible. It gives the "scar-tissue review" cadence (a sibling to
fresh-eyes whose job each fire is to RETIRE or consolidate one defense) an objective number to
move, and lets `/strategic-pulse` surface "complexity grew again this week." You cannot manage —
or shed — what you do not measure.

DESIGN
------
Pure, hermetic, domain-free. `measure(root)` returns the counts. `append_and_delta(metrics,
ledger)` appends a timestamped row and reports the change since the previous row, with an
advisory verdict. No import-time path resolution; the caller passes the repo root.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

# The metrics that are ADDITIVE pressure (growth is a cost signal). Kept small and
# file-based so the count is deterministic and a future dev can verify it by eye.
_LINE_COUNT_TARGETS = {
    "orchestrator_lines": ".claude/skills/aspirations/SKILL.md",
    "aspirations_yaml_lines": "core/config/aspirations.yaml",
    # g-115-4928: the two entries above measured the surface already under
    # deliberate DOWNWARD pressure (the orchestrator is small precisely because
    # the loop-digest extraction slimmed it), so the instrument read healthy
    # while the growth went somewhere it could not see. Measured on three dates,
    # same `wc -l`, by three agents on three boxes:
    #     aspirations/SKILL.md            819 -> 869 -> 873   (+54,  +6.6%)
    #     aspirations-precheck/SKILL.md  2216 -> 2792 -> 2867  (+651, +29.4%)
    #     (2026-08-04 echo/cc-03 · 08-23 echo/cc-03 · 08-30 bravo/cc-05)
    # The untracked file added 12x more lines at 4.5x the rate; the ratio went
    # 2.7x -> 3.2x -> 3.28x. NOT redundant with hot-path-size-gate.sh, which
    # also sees this file: that gate is a commit-time PASS/FAIL on a CORPUS
    # total (currently red), so it cannot say WHICH of 131 skills grew. This is
    # a per-metric trend across the ledger's rows, consumed by the scar-tissue
    # cadence. Different question, different consumer.
    "precheck_lines": ".claude/skills/aspirations-precheck/SKILL.md",
}


def measure(root) -> Dict[str, Optional[int]]:
    """Count the framework's complexity surface under `root`.

    Counts (not lines, except where size itself is the smell):
      gate_scripts          — *-gate.py + *-gate.sh in core/scripts (the gate portfolio)
      rules                 — .claude/rules/*.md (the behavioral constitution)
      skills                — .claude/skills/*/SKILL.md
      core_scripts          — core/scripts/*.py + *.sh (infra surface)
      conventions           — core/config/conventions/*.md
    Line counts where bulk itself is the maintainability cost:
      orchestrator_lines    — the aspirations SKILL.md (re-read every loop iteration)
      aspirations_yaml_lines— the central tuning config (10+ interlocking knobs)
    Missing files yield None (not 0) so a moved file is visible as a gap, not a silent drop.
    NOTE: gate_scripts and core_scripts OVERLAP — every *-gate.py is also counted in
    core_scripts (gates are a subset of scripts). They are not additive partitions; do not
    sum the metrics into a single "total complexity" number.
    """
    root = Path(root)

    def n(pattern: str) -> int:
        return len(list(root.glob(pattern)))

    def lines(rel: str) -> Optional[int]:
        p = root / rel
        if not p.exists():
            return None
        return len(p.read_text(encoding="utf-8").splitlines())

    metrics: Dict[str, Optional[int]] = {
        "gate_scripts": n("core/scripts/*-gate.py") + n("core/scripts/*-gate.sh"),
        "rules": n(".claude/rules/*.md"),
        "skills": n(".claude/skills/*/SKILL.md"),
        "core_scripts": n("core/scripts/*.py") + n("core/scripts/*.sh"),
        "conventions": n("core/config/conventions/*.md"),
    }
    for key, rel in _LINE_COUNT_TARGETS.items():
        metrics[key] = lines(rel)
    return metrics


def _read_last_row(ledger_path) -> Optional[dict]:
    """Last valid JSON row in the ledger, scanning backward. Fail-open: a corrupt
    or partially-written trailing line (an interrupted append) is skipped rather
    than bricking every future append — an append-only instrument must not be
    permanently disabled by one bad write."""
    p = Path(ledger_path)
    if not p.exists():
        return None
    rows = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    for ln in reversed(rows):
        try:
            return json.loads(ln)
        except json.JSONDecodeError:
            continue
    return None


def append_and_delta(metrics: Dict[str, Optional[int]], ledger_path,
                     timestamp: Optional[str] = None) -> dict:
    """Append a timestamped metrics row to the JSONL ledger and report the delta
    vs the previous row, with an advisory verdict.

    `deltas` holds numeric changes for metrics that are int in BOTH rows.
    `transitions` separately records metrics that APPEARED or DISAPPEARED between
    rows (absent/None on one side, int on the other) — e.g. a tracked file moved
    (int->None) or restored (None->int). These are NOT numeric deltas but they ARE
    real changes, so they must not be silently swallowed into a "flat" verdict (the
    bug this version fixes).
    verdict (it describes the NUMERIC movement; `transitions` is a SEPARATE channel
    that is ALWAYS reported, so a "growing"/"shrinking"/"mixed" verdict CAN co-occur
    with a transition — read `transitions` too, never the verdict label alone):
      "baseline"  — first row, nothing to compare.
      "growing"   — at least one numeric metric increased and none decreased.
      "shrinking" — at least one numeric metric decreased and none increased.
      "mixed"     — some numeric metrics up, some down.
      "changed"   — no numeric movement, but a metric appeared/disappeared
                    (a transition with no delta; inspect `transitions`).
      "flat"      — nothing changed at all (no deltas, no transitions).
    """
    ts = timestamp or datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    prev = _read_last_row(ledger_path)
    row = {"timestamp": ts, "metrics": metrics}
    deltas: Dict[str, int] = {}
    transitions: Dict[str, str] = {}
    if prev:
        pm = prev.get("metrics", {})
        keys = set(metrics) | set(pm)
        for k in keys:
            v, pv = metrics.get(k), pm.get(k)
            v_int, pv_int = isinstance(v, int), isinstance(pv, int)
            if v_int and pv_int:
                deltas[k] = v - pv
            elif v_int != pv_int:
                # exactly one side is a real count -> an appear/disappear transition
                transitions[k] = f"{pv!r}->{v!r}"
    up = any(d > 0 for d in deltas.values())
    down = any(d < 0 for d in deltas.values())
    if not prev:
        verdict = "baseline"
    elif up and down:
        verdict = "mixed"
    elif up:
        verdict = "growing"
    elif down:
        verdict = "shrinking"
    elif transitions:
        verdict = "changed"
    else:
        verdict = "flat"

    p = Path(ledger_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    return {"row": row, "deltas": deltas, "transitions": transitions,
            "verdict": verdict, "had_previous": bool(prev)}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Complexity budget — measure the framework's complexity surface.")
    ap.add_argument("--root", default=".", help="repo root (default: cwd)")
    ap.add_argument("--ledger", default=None,
                    help="JSONL ledger to append to + diff against (if omitted, measure only)")
    args = ap.parse_args(argv)
    metrics = measure(args.root)
    if args.ledger:
        print(json.dumps(append_and_delta(metrics, args.ledger), indent=2))
    else:
        print(json.dumps({"metrics": metrics}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
