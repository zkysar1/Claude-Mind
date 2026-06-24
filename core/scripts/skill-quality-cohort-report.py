#!/usr/bin/env python3
"""skill-quality-cohort-report.py -- Layer 5 of the skill-telemetry signal repair master plan.

Joins per-skill QUALITY (meta/skill-quality.yaml -> skills.<name>.aggregate.overall)
with per-skill USAGE (Layer-2 skill-attribution.py aggregation of every
<agent>/skill-invocations.jsonl) and bins each evaluated skill into one of four
COHORTS on the quality x usage grid.

COHORT BOUNDARY RULES (the "documented boundary rules" the contract check verifies):
    quality_high := aggregate.overall >= --quality-threshold   (default 0.50,
                    aligned with skill-quality-strategy.yaml review_threshold)
    usage_high   := total_invocations  >= --use-threshold      (default 5)

    quality_high & usage_high   -> winners                 (keep + amplify)
    quality_high & usage_low    -> silent_gems             (good but under-used: promote)
    quality_low  & usage_high   -> problem_hotspots        (bad but heavily-used: fix first)
    quality_low  & usage_low    -> deprecation_candidates  (bad and unused: retire)

Skills present in the usage data but ABSENT from skill-quality.yaml are reported
separately as `unevaluated_in_use` -- they have no quality score to bin on, so
they are NOT forced into a quality cohort (forcing them would fabricate a
quality verdict). A used-but-unevaluated skill is itself a real signal.

Usage source is Layer 2 (skill-attribution.py --json). When the invocation
ledger is empty (no telemetry accumulated yet, or Layer 2's agent-dir scan finds
nothing), EVERY skill bins as usage_low -- correct, and surfaced via the
`usage_ledger_empty` flag so the reader knows the low-use cohorts reflect ABSENT
data, not measured disuse.

Skill keys are canonicalized (strip leading slash, take first token) exactly as
skill-attribution.py and skill-quality-score.py do, so '/replay' and 'replay'
collapse to one skill.

Usage:
  python3 core/scripts/skill-quality-cohort-report.py            # text report
  python3 core/scripts/skill-quality-cohort-report.py --json     # machine-readable
  python3 core/scripts/skill-quality-cohort-report.py --quality-threshold 0.5 --use-threshold 5
  python3 core/scripts/skill-quality-cohort-report.py --since 30d  # usage window (passed to Layer 2)

Knowledge tree: world/knowledge/tree/system/system-constraints-loop/skill-telemetry-signal-master-plan.md
"""
import os
import sys
import json
import argparse
import subprocess

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, SCRIPT_DIR)

try:
    import yaml
except ImportError:
    yaml = None

import _paths  # noqa: E402

DEFAULT_QUALITY_THRESHOLD = 0.50  # == skill-quality-strategy.yaml review_threshold
DEFAULT_USE_THRESHOLD = 5

COHORT_KEYS = ("winners", "silent_gems", "problem_hotspots", "deprecation_candidates")
COHORT_LABELS = {
    "winners": "WINNERS (high quality + high use -- keep & amplify)",
    "silent_gems": "SILENT GEMS (high quality + low use -- promote)",
    "problem_hotspots": "PROBLEM HOTSPOTS (low quality + high use -- fix first)",
    "deprecation_candidates": "DEPRECATION CANDIDATES (low quality + low use -- retire)",
}


def canon(name):
    """Canonical skill key: strip leading slash + take first token.

    Mirrors skill-attribution.py and skill-quality-score canonicalization so
    '/replay', 'replay', and 'replay --foo' all collapse to 'replay'.
    """
    if not name:
        return name
    return name.lstrip("/").split(None, 1)[0]


def load_quality():
    """Return {canon_skill: overall_score} from meta/skill-quality.yaml aggregate.overall."""
    if yaml is None:
        return {}
    path = os.path.join(str(_paths.META_DIR), "skill-quality.yaml")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        d = yaml.safe_load(f) or {}
    out = {}
    for name, rec in (d.get("skills") or {}).items():
        c = canon(name)
        if not c or not isinstance(rec, dict):
            continue
        overall = (rec.get("aggregate") or {}).get("overall")
        if overall is None:
            continue
        # Canonicalization can collapse '/replay' and 'replay' into one key --
        # keep the most-favorable (max) overall when duplicates appear.
        if c not in out or overall > out[c]:
            out[c] = overall
    return out


def load_usage(since=""):
    """Return ({canon_skill: total_invocations}, ledger_empty) from Layer-2 skill-attribution.py --json.

    Layer 2 is the single source of truth for usage. We shell out to its stable
    --json contract rather than re-walking the per-agent ledgers (DRY). Any
    failure (Layer 2 missing, timeout, malformed JSON) degrades to empty usage
    with ledger_empty=True so the report still emits a valid schema.
    """
    cmd = [sys.executable, os.path.join(SCRIPT_DIR, "skill-attribution.py"), "--json"]
    if since:
        cmd += ["--since", since]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        data = json.loads(res.stdout)
    except Exception:
        return {}, True
    usage = {}
    for name, s in (data.get("per_skill") or {}).items():
        c = canon(name)
        if not c:
            continue
        usage[c] = usage.get(c, 0) + (s.get("total_invocations", 0) or 0)
    ledger_empty = (data.get("total_rows", 0) or 0) == 0
    return usage, ledger_empty


def build_cohorts(quality, usage, q_thresh, u_thresh):
    """Bin each evaluated skill into one of four cohorts; collect used-but-unevaluated separately."""
    cohorts = {k: [] for k in COHORT_KEYS}
    for sk in sorted(quality):
        q = quality[sk]
        u = usage.get(sk, 0)
        q_high = q >= q_thresh
        u_high = u >= u_thresh
        rec = {"skill": sk, "overall": round(q, 3), "total_invocations": u}
        if q_high and u_high:
            cohorts["winners"].append(rec)
        elif q_high:
            cohorts["silent_gems"].append(rec)
        elif u_high:
            cohorts["problem_hotspots"].append(rec)
        else:
            cohorts["deprecation_candidates"].append(rec)
    unevaluated_in_use = sorted(
        ({"skill": sk, "total_invocations": usage[sk]} for sk in usage if sk not in quality),
        key=lambda r: -r["total_invocations"],
    )
    return cohorts, unevaluated_in_use


def build_report(q_thresh, u_thresh, since=""):
    quality = load_quality()
    usage, ledger_empty = load_usage(since)
    cohorts, unevaluated = build_cohorts(quality, usage, q_thresh, u_thresh)
    return {
        "quality_threshold": q_thresh,
        "use_threshold": u_thresh,
        "window_since": since or "all_time",
        "skills_evaluated": len(quality),
        "usage_ledger_empty": ledger_empty,
        "cohort_counts": {k: len(cohorts[k]) for k in COHORT_KEYS},
        "cohorts": cohorts,
        "unevaluated_in_use": unevaluated,
    }


def print_text(report):
    print("=== skill-quality cohort report (Layer 5) ===")
    print(f"  quality_threshold={report['quality_threshold']}  "
          f"use_threshold={report['use_threshold']}  window={report['window_since']}")
    print(f"  skills evaluated: {report['skills_evaluated']}")
    if report["usage_ledger_empty"]:
        print("  NOTE: usage ledger is EMPTY -- every skill bins as usage_low "
              "(absent telemetry, not measured disuse)")
    for key in COHORT_KEYS:
        recs = report["cohorts"][key]
        print(f"\n  {COHORT_LABELS[key]}  [{len(recs)}]")
        for r in recs:
            print(f"    {r['skill']:28} overall={r['overall']:.2f}  invocations={r['total_invocations']}")
    uneval = report["unevaluated_in_use"]
    if uneval:
        print(f"\n  UNEVALUATED-IN-USE (usage but no quality eval) [{len(uneval)}]")
        for r in uneval:
            print(f"    {r['skill']:28} invocations={r['total_invocations']}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Layer-5 skill quality x usage cohort report.")
    ap.add_argument("--json", action="store_true", help="Machine-readable output")
    ap.add_argument("--quality-threshold", type=float, default=DEFAULT_QUALITY_THRESHOLD,
                    help=f"overall >= this is high-quality (default {DEFAULT_QUALITY_THRESHOLD})")
    ap.add_argument("--use-threshold", type=int, default=DEFAULT_USE_THRESHOLD,
                    help=f"total_invocations >= this is high-use (default {DEFAULT_USE_THRESHOLD})")
    ap.add_argument("--since", default="", help="Usage window passed to Layer 2 (7d, 24h, ISO)")
    args = ap.parse_args(argv)

    report = build_report(args.quality_threshold, args.use_threshold, args.since)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print_text(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
