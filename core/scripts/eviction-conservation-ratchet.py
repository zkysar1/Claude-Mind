#!/usr/bin/env python3
"""eviction-conservation-ratchet.py — advisory pigeonhole-drift check with baseline ratchet.

Wired into /verify-learning as a post-test check (g-115-2505). Runs the
read-only conservation audit from aspirations-evict-completed.py (--audit
semantics: in_list + archived_census vs minted-id capacity, g-115-1951 /
g-115-1938), compares total phantom excess against the last-recorded baseline
in meta/audit-baselines.yaml, and reports:

  - excess > baseline: REGRESSED — new resurrection double-count introduced
  - excess < baseline: RATCHETED — lower baseline persisted (repair progress)
  - excess == baseline: STABLE

Why a ratchet and not an exit-0 assertion: at wiring time the world audit
reports 4 historical violations (42 phantom excess — the g-115-2503
investigation's documented finding). Historical drift exists and its repair is
queued future work, so per core/config/conventions/audit-baselines.md the
metric baselines instead of hard-gating. The tripwire the goal wanted still
fires: any NEW resurrection double-count reads REGRESSED.

Exit codes:
  0  any outcome (advisory — never hard-fails /verify-learning)
  2  script error (audit failed, unwriteable baseline file, etc.)

Hard-gate opt-in: VERIFY_LEARNING_DRIFT_HARD_GATE=1 (exit 1 on regressed),
same contract as learning-routing-ratchet.py.
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from _paths import META_DIR, WORLD_DIR  # type: ignore
from _fileops import locked_modify_yaml  # type: ignore

try:
    import yaml  # type: ignore  # noqa: F401
except ImportError:
    print("ERROR: PyYAML not installed", file=sys.stderr)
    sys.exit(2)

import importlib.util
_evict_spec = importlib.util.spec_from_file_location(
    "aspirations_evict_completed",
    Path(__file__).parent / "aspirations-evict-completed.py",
)
evict = importlib.util.module_from_spec(_evict_spec)
_evict_spec.loader.exec_module(evict)


BASELINES_PATH = META_DIR / "audit-baselines.yaml"
KEY = "eviction_conservation_phantom_excess"


def _compute_excess():
    path = Path(WORLD_DIR) / "aspirations.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found")
    # Same refresh-then-read shape as the audit branch in
    # aspirations-evict-completed.main — under own-cloud the local file is a
    # read-through cache; refresh so the count reflects the store of record.
    try:
        from storage_backend import get_backend
        get_backend().refresh(path)
    except Exception as e:
        print(f"[eviction-conservation-ratchet] (refresh skipped: {e})",
              file=sys.stderr)
    items = [json.loads(ln)
             for ln in path.read_text(encoding="utf-8").splitlines()
             if ln.strip()]
    violations = evict._audit_violations(items)
    return {
        "total": sum(v["excess"] for v in violations),
        "violations": len(violations),
        "by_aspiration": {v["id"]: v["excess"] for v in violations},
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true", help="Emit JSON")
    args = ap.parse_args()

    try:
        current = _compute_excess()
    except Exception as e:
        print(f"ERROR: audit failed: {e}", file=sys.stderr)
        return 2

    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    captured: dict = {}

    def _modify(baselines):
        # Locked RMW — read baseline INSIDE the lock ( sibling-stomp
        # pattern; see audit-baselines.md). Sibling writers share this file.
        if not isinstance(baselines, dict):
            baselines = {}
        entry = baselines.get(KEY) or {}
        prior_baseline = entry.get("baseline")

        if prior_baseline is None:
            verdict = "seeded"
            new_baseline = current["total"]
            message = (
                f"Seeded baseline at phantom excess={new_baseline} "
                f"({current['violations']} violating aspiration(s)). Future "
                f"runs warn if excess grows, ratchet if repair shrinks it."
            )
        elif current["total"] > prior_baseline:
            verdict = "regressed"
            new_baseline = prior_baseline  # never raise the baseline
            delta = current["total"] - prior_baseline
            message = (
                f"WARN: phantom excess grew from baseline {prior_baseline} to "
                f"{current['total']} (+{delta}) — a NEW resurrection "
                f"double-count landed since the baseline. Inspect with "
                f"`py -3 core/scripts/aspirations-evict-completed.py --source "
                f"world --audit`; repair via `--repair-census --apply` only "
                f"after understanding the source (g-115-2503)."
            )
        elif current["total"] < prior_baseline:
            verdict = "ratcheted"
            new_baseline = current["total"]
            delta = prior_baseline - current["total"]
            message = (
                f"OK: phantom excess shrank from baseline {prior_baseline} to "
                f"{current['total']} (-{delta}). Baseline ratcheted down."
            )
        else:
            verdict = "stable"
            new_baseline = prior_baseline
            message = f"OK: phantom excess stable at baseline {current['total']}."

        history = entry.get("history") or []
        history.append({
            "recorded_at": now_iso,
            "drift_total": current["total"],
            "verdict": verdict,
            "breakdown": dict(current["by_aspiration"]),
        })
        history = history[-50:]
        baselines[KEY] = {
            "baseline": new_baseline,
            "last_recorded": now_iso,
            "last_verdict": verdict,
            "history": history,
        }
        captured["verdict"] = verdict
        captured["new_baseline"] = new_baseline
        captured["message"] = message
        return baselines

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

    verdict = captured["verdict"]
    result = {
        "verdict": verdict,
        "baseline": captured["new_baseline"],
        "current": current,
        "message": captured["message"],
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"[eviction-conservation-ratchet] {verdict.upper()}: "
              f"{captured['message']}")

    if os.environ.get("VERIFY_LEARNING_DRIFT_HARD_GATE") == "1":
        return 1 if verdict == "regressed" else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
