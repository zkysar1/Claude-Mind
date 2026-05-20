#!/usr/bin/env python3
"""learning-routing-ratchet.py — advisory drift check with baseline ratchet.

Wired into /verify-learning as a post-test check. Runs the audit, compares
drift_total against the last-recorded baseline in meta/audit-baselines.yaml,
and reports:

  - drift > baseline: WARN — new drift introduced since baseline
  - drift < baseline: OK (ratcheted) — lower baseline persisted
  - drift == baseline: OK (stable)

The ratchet ensures drift can only shrink over time. The baseline is auto-seeded
on first run (creates the YAML with current drift_total).

Exit codes:
  0  any outcome (advisory — never hard-fails /verify-learning)
  2  script error (audit failed, unwriteable baseline file, etc.)

The exit-always-0 choice is deliberate: historical drift already fixed; future
drift should be visible but shouldn't block routine verify-learning runs. If
hard-gating is wanted later, set VERIFY_LEARNING_DRIFT_HARD_GATE=1 in the env.
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
from _paths import META_DIR  # type: ignore
from _fileops import locked_modify_yaml  # type: ignore

try:
    import yaml  # type: ignore
except ImportError:
    print("ERROR: PyYAML not installed", file=sys.stderr)
    sys.exit(2)

import importlib.util
_audit_spec = importlib.util.spec_from_file_location(
    "learning_routing_audit",
    Path(__file__).parent / "learning-routing-audit.py",
)
audit = importlib.util.module_from_spec(_audit_spec)
_audit_spec.loader.exec_module(audit)


BASELINES_PATH = META_DIR / "audit-baselines.yaml"
KEY = "learning_routing_drift"


def _compute_drift_total():
    stores = {
        "reasoning_bank": audit.load_reasoning_bank(),
        "guardrails": audit.load_guardrails(),
        "pipeline": audit.load_pipeline(),
        "pattern_signatures": audit.load_pattern_signatures(),
        "experience": audit.load_all_experiences(),
    }
    tree_keys = audit.load_tree_node_keys()
    ids = audit.build_id_sets(stores)
    dangling, _prose = audit.audit_cross_refs(stores, ids, tree_keys)
    doc_findings = audit.audit_doc_pointers()
    catalog_findings = audit.audit_store_catalog()
    return {
        "total": len(dangling) + len(doc_findings) + len(catalog_findings),
        "dangling": len(dangling),
        "doc_pointers": len(doc_findings),
        "catalog": len(catalog_findings),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true", help="Emit JSON")
    args = ap.parse_args()

    try:
        current = _compute_drift_total()
    except Exception as e:
        print(f"ERROR: audit failed: {e}", file=sys.stderr)
        return 2

    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    captured: dict = {}

    def _modify(baselines):
        # : read baseline INSIDE the lock so the verdict + new_baseline
        # decision sees the same prior_baseline that the write commits. Sibling
        # writers (session-manifest-orphan-ratchet) share this lock via the same
        # file path — see core/config/conventions/audit-baselines.md. Without
        # the locked RMW, two writers each "ratchet" against an already-stale
        # baseline and the second writer reverts the first writer's foreign-key
        # changes (sibling-stomp).
        if not isinstance(baselines, dict):
            baselines = {}
        entry = baselines.get(KEY) or {}
        prior_baseline = entry.get("baseline")

        # First-run seed
        if prior_baseline is None:
            verdict = "seeded"
            new_baseline = current["total"]
            message = (
                f"Seeded baseline at drift={new_baseline}. Future runs will "
                f"warn if drift exceeds this, ratchet if it shrinks."
            )
        elif current["total"] > prior_baseline:
            verdict = "regressed"
            new_baseline = prior_baseline  # do NOT raise the baseline
            delta = current["total"] - prior_baseline
            message = (
                f"WARN: drift increased from baseline {prior_baseline} to "
                f"{current['total']} (+{delta}). Run "
                f"`bash core/scripts/learning-routing-audit.sh` to inspect, and "
                f"consider `bash core/scripts/learning-routing-repair.sh --apply` "
                f"if the new drift is historical."
            )
        elif current["total"] < prior_baseline:
            verdict = "ratcheted"
            new_baseline = current["total"]
            delta = prior_baseline - current["total"]
            message = (
                f"OK: drift shrank from baseline {prior_baseline} to "
                f"{current['total']} (-{delta}). Baseline ratcheted down."
            )
        else:
            verdict = "stable"
            new_baseline = prior_baseline
            message = f"OK: drift stable at baseline {current['total']}."

        # Persist baseline + history
        history = entry.get("history") or []
        history.append({
            "recorded_at": now_iso,
            "drift_total": current["total"],
            "verdict": verdict,
            "breakdown": {
                "dangling": current["dangling"],
                "doc_pointers": current["doc_pointers"],
                "catalog": current["catalog"],
            },
        })
        # Bound history to last 50 entries to prevent unbounded growth
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
        captured.setdefault("verdict", "error")
        captured.setdefault("new_baseline", None)
        captured.setdefault("message", f"baseline operation failed: {e}")

    verdict = captured["verdict"]
    new_baseline = captured["new_baseline"]
    message = captured["message"]

    result = {
        "verdict": verdict,
        "baseline": new_baseline,
        "current": current,
        "message": message,
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"[learning-routing-ratchet] {verdict.upper()}: {message}")

    # Hard-gate only if explicitly opted in
    if os.environ.get("VERIFY_LEARNING_DRIFT_HARD_GATE") == "1":
        return 1 if verdict == "regressed" else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
