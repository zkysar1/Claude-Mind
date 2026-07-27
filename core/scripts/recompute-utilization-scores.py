#!/usr/bin/env python3
"""recompute-utilization-scores.py — one-shot backfill for .

Re-runs recompute_utilization_score over every reasoning-bank and guardrail
record so stored utilization_score / utilization_score_v2 values reflect the
shrinkage denominator (max(rc, credited_usages) + 1). Without this, the 221
rb + 1 guardrail entries whose scores were inflated under the old max(rc, 1)
denominator keep their stored 1.0-5.0 values until their next increment —
and those context-bumped entries are exactly the ones least likely to be
incremented again, so sort_universal_rbs ranking stays poisoned.

Dry-run by default (prints per-store change counts + top deltas); --apply
writes via _fileops.locked_modify_jsonl (DDB lock + force-fresh read +
fenced PUT + .history snapshot + JSONL canary — the bulk-retire-dead-entries
precedent, same stores).

Usage (own-cloud boxes need the governed-root env — bare `py -3` raises
OwnCloudBackend.from_env RuntimeError on the --apply RMW path):
    source core/scripts/_paths.sh && python3 core/scripts/recompute-utilization-scores.py [--apply]
"""
import argparse
import importlib.util
import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS))

from _paths import WORLD_DIR  # noqa: E402


def _load_rb_module():
    spec = importlib.util.spec_from_file_location(
        "rb_cli_backfill", _SCRIPTS / "reasoning-bank.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rb_cli_backfill"] = mod
    spec.loader.exec_module(mod)
    return mod


def _plan(records, recompute):
    """Return (changed_count, deltas) without mutating the input."""
    changed = 0
    deltas = []
    for rec in records:
        util = rec.get("utilization")
        if not isinstance(util, dict) or "retrieval_count" not in util:
            continue
        old_v1 = util.get("utilization_score")
        old_v2 = util.get("utilization_score_v2")
        probe = {"utilization": dict(util)}
        try:
            recompute(probe)
        except Exception:
            continue  # malformed counters — leave untouched
        new_v1 = probe["utilization"]["utilization_score"]
        new_v2 = probe["utilization"]["utilization_score_v2"]
        if new_v1 != old_v1 or new_v2 != old_v2:
            changed += 1
            deltas.append((rec.get("id"), old_v1, new_v1))
    deltas.sort(key=lambda d: (d[1] or 0) - (d[2] or 0), reverse=True)
    return changed, deltas


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write the recomputed scores (default: dry-run report)")
    args = ap.parse_args()

    rb_mod = _load_rb_module()
    recompute = rb_mod.recompute_utilization_score

    stores = [
        ("reasoning-bank", Path(WORLD_DIR) / "reasoning-bank.jsonl"),
        ("guardrails", Path(WORLD_DIR) / "guardrails.jsonl"),
    ]
    summary = {}
    for name, path in stores:
        if not path.exists():
            summary[name] = {"error": "store missing"}
            continue
        records = [json.loads(l) for l in
                   path.read_text(encoding="utf-8").splitlines() if l.strip()]
        changed, deltas = _plan(records, recompute)
        summary[name] = {
            "total": len(records),
            "would_change": changed,
            "top_deltas": [
                {"id": i, "old": o, "new": n} for i, o, n in deltas[:5]],
        }
        if args.apply and changed:
            from _fileops import locked_modify_jsonl

            def _modifier(recs):
                for rec in recs:
                    util = rec.get("utilization")
                    if not isinstance(util, dict) or "retrieval_count" not in util:
                        continue
                    try:
                        recompute(rec)
                    except Exception:
                        continue
                return recs

            locked_modify_jsonl(path, _modifier)
            summary[name]["applied"] = True

    print(json.dumps({"apply": args.apply, "stores": summary}, indent=2))


if __name__ == "__main__":
    main()
