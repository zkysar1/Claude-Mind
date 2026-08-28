#!/usr/bin/env python3
"""Improvement velocity (imp@k) computation and tracking.

Computes the rate of learning improvement over rolling windows.
imp@k = (metric_after - metric_before) / k goals

Subcommands:
  compute   — compute current imp@k for a metric over a window
  snapshot  — record a learning_value entry for a goal
"""

import argparse
import json
import math
import sys
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Standard errors the recent-vs-older shift must exceed before `direction`
# leaves "stable". MIRRORED in mind_api/src/meta/meta_impk.py — the daemon copy
# is the LIVE path (meta-impk.sh is a daemon-only wrapper), so editing this file
# alone changes NOTHING at runtime (guard-742/547).
SIGNIFICANCE_SE_MULTIPLE = 2.0

def _population_variance(entries):
    """Population variance of learning_value over `entries` (0.0 for n<2).

    Population rather than sample: these windows ARE the population of the
    interval being described, not a draw from a larger one, and the n-1
    correction would only inflate the SE at small windows — i.e. make the
    gate more conservative exactly where the goal that motivated this
    (g-115-5902) found it firing on noise. Conservative in the safe
    direction is fine; being conservative for a reason that does not apply
    is a number nobody can re-derive.
    """
    vals = [float(e.get("learning_value", 0) or 0) for e in entries]
    n = len(vals)
    if n < 2:
        return 0.0
    mean = sum(vals) / n
    return sum((v - mean) ** 2 for v in vals) / n


def _direction_from_significance(delta, significance, threshold):
    """Total mapping from (delta, significance) to a direction label.

    TOTAL by construction (guard-805): a sub-threshold significance, an
    unmeasurable one (None), and delta == 0 all land in "stable", so no input
    falls through to a wrong fallback.
    """
    if significance is None or significance < threshold:
        return "stable"
    if delta > 0:
        return "improving"
    if delta < 0:
        return "declining"
    return "stable"

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

from _paths import META_DIR


def read_yaml(path):
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    # OneDrive sync-corruption guard (): a transiently un-synced replica of a
    # shared meta hot-file reads as NUL bytes -- full zero-fill, or (on a partial block
    # sync) a real-YAML prefix + NUL suffix. The mechanism is a sync-write race on a
    # frequently rewritten file, NOT Files-On-Demand dehydration: all meta files are
    # pinned ("always keep on this device"), census-verified 0 dehydration-prone of 40,237
    # () -- so do not chase a dehydration fix, it is already maxed out. Both
    # forms make yaml.safe_load raise
    # a cryptic "#x0000 ReaderError". Translate to an actionable message so the next reader
    # does not burn an investigation. Behavior is UNCHANGED -- the read still raises, so the
    # write path (cmd_snapshot) aborts and the file is NOT overwritten (no clobber); only the
    # message is clearer and names the recovery path. Recovery is assured by the .history
    # snapshots written on every prior successful write.
    if chr(0) in raw:
        raise ValueError(
            f"{path.name}: {raw.count(chr(0))} NUL byte(s) detected -- transient OneDrive "
            f"sync corruption of a shared meta hot-file. NOT overwriting (read aborts, no "
            f"clobber). Usually transient: re-read after OneDrive re-syncs, or restore a "
            f"known-good version via `py -3 core/scripts/history.py restore '{path}' <version>` "
            f"(list versions with `history.py list '{path}'`). (g-115-1271)"
        )
    data = yaml.safe_load(raw)
    return data if data is not None else {}


def write_yaml(path, data):
    """Atomically write YAML with locking and history."""
    from _fileops import locked_write_yaml
    locked_write_yaml(path, data)


def validate_velocity_structure(data, label=""):
    """Validate structural integrity of improvement-velocity data."""
    prefix = f"[{label}] " if label else ""
    if not isinstance(data, dict):
        raise ValueError(f"{prefix}Expected dict, got {type(data).__name__}")
    allowed_keys = {"entries", "rolling_averages"}
    unexpected = set(data.keys()) - allowed_keys
    if unexpected:
        raise ValueError(f"{prefix}Unexpected top-level keys: {unexpected}")
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        raise ValueError(f"{prefix}'entries' is {type(entries).__name__}, expected list")
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"{prefix}entries[{i}] is {type(entry).__name__}, expected dict")
        if "goal_id" not in entry:
            raise ValueError(f"{prefix}entries[{i}] missing 'goal_id'")
        if "learning_value" not in entry:
            raise ValueError(f"{prefix}entries[{i}] missing 'learning_value'")
    ra = data.get("rolling_averages")
    if ra is not None and not isinstance(ra, dict):
        raise ValueError(f"{prefix}'rolling_averages' is {type(ra).__name__}, expected dict")


def cmd_compute(args):
    """Compute improvement velocity over a window."""
    vel = read_yaml(META_DIR / "improvement-velocity.yaml")
    entries = vel.get("entries", [])

    window = args.window
    if len(entries) < window:
        result = {
            "series": "learning_value",
            "window": window,
            "imp_at_k": 0.0,
            "direction": "insufficient_data",
            "entries_available": len(entries),
        }
    else:
        recent = entries[-window:]
        older = entries[-(window * 2):-window] if len(entries) >= window * 2 else entries[:len(entries) - window]

        sig_k = args.significance

    # DIRECTION IS A SIGNIFICANCE TEST ON THE RAW DELTA, NOT A COMPARISON OF
    # `imp` AGAINST A FIXED CONSTANT (, 2026-08-28).
    #
    # The old line was:
    #     direction = "improving" if imp > 0.001 else ("declining" if imp < -0.001 else "stable")
    # and `imp` is `delta / window`. So the quantity being thresholded is
    # divided by the window while the threshold is not — the constant is in
    # DIFFERENT UNITS at every window, and no value of it is correct at all of
    # them (guard-3810: "not slightly tight, it is in the WRONG UNITS").
    #
    # MEASURED on the live series 2026-08-28 (n=9912, mean 0.410, stdev 0.358),
    # the same data returning three different verdicts:
    #     w=5   delta -0.0440  imp -0.008800  0.41 SE -> "declining"
    #     w=10  delta -0.0900  imp -0.009000  1.14 SE -> "declining"
    #     w=20  delta +0.1240  imp +0.006200  2.13 SE -> "improving"
    #     w=40  delta -0.00025 imp -0.000006  0.01 SE -> "stable"
    # Note the ORDERING INVERSION, which is worse than the flip and was not in
    # the goal's filing: w=5 is a 0.41-SE noise blip yet reports a LARGER |imp|
    # than w=20, which is the one window where the shift is arguably real. The
    # old statistic ranked noise above signal.
    #
    # WHY NOT A RELATIVE THRESHOLD (candidate (a) in the goal: |imp| > 0.02 *
    # recent_avg). Measured against the same rows: at recent_avg ~0.35 that bar
    # is ~0.007, which keeps w=5's 0.41-SE blip as "declining" AND demotes
    # w=20's 2.13-SE shift to "stable". It moves both readings the WRONG way,
    # so it is not a cheaper version of this fix — it is worse than the bug.
    # (guard-2260: a remedy is a separate claim from the diagnosis and needs
    # its own measurement. It got one, and it failed.)
    #
    # WHAT THIS DOES INSTEAD. Test `delta` — the actual recent-vs-older shift —
    # against the standard error of that difference. `significance` is how many
    # standard errors the shift is; the label fires only past
    # SIGNIFICANCE_SE_MULTIPLE. SE scales with the window exactly as the noise
    # does, so one threshold is meaningful at every window, which is the
    # property the constant could never have.
    #
    # `imp_at_k` IS UNCHANGED and still reported: it is the documented metric
    # and other readers consume it. Only the LABEL derivation moved.
    #
    # The emitted `delta` / `standard_error` / `significance` /
    # `significance_threshold` fields are guard-3810's "diagnostic before
    # numeric" half: a bare label cannot be re-judged by a reader, and a score
    # without its scorer is uninterpretable. A caller that disagrees with the
    # multiple can now see the number the verdict came from.
    #
    # The mapping is TOTAL (guard-805): every (significance, delta) pair lands
    # in exactly one label, with delta == 0 and every sub-threshold value
    # falling to "stable". No uncovered gap.
        if older:
            recent_avg = sum(e.get("learning_value", 0) for e in recent) / len(recent)
            older_avg = sum(e.get("learning_value", 0) for e in older) / len(older)
            delta = recent_avg - older_avg
            imp = delta / window
            se = math.sqrt(_population_variance(recent) / window
                           + _population_variance(older) / window)
            if se > 0:
                significance = abs(delta) / se
            else:
                # Zero variance in BOTH windows: any nonzero delta is a clean
                # step change, not noise. delta == 0 is genuinely stable.
                significance = float("inf") if delta else 0.0
        else:
            # No older window exists, so there is nothing to compare against.
            # The old code took recent_avg / window here, which is positive for
            # any normal series and therefore reported "improving" for a
            # first-ever window on no evidence at all. Report "stable" with a
            # null significance instead: absent a baseline the honest verdict
            # is "no trend measured", and "stable" is also the label that does
            # NOT trigger the aspirations-evolve Step 0.7 META ALERT branch.
            recent_avg = sum(e.get("learning_value", 0) for e in recent) / len(recent)
            delta = None
            imp = recent_avg / window
            se = None
            significance = None

        direction = _direction_from_significance(delta or 0.0, significance, sig_k)
        result = {
            "series": "learning_value",
            "window": window,
            "imp_at_k": round(imp, 6),
            "direction": direction,
            "recent_avg": round(recent_avg, 4),
            "delta": (round(delta, 6) if delta is not None else None),
            "standard_error": (round(se, 6) if se is not None else None),
            "significance": (
                None if significance is None
                else ("inf" if significance == float("inf") else round(significance, 3))
            ),
            "significance_threshold": sig_k,
        }

    # Non-default label: echo it but say plainly the computation ignored it
    # ( — the old output echoed the label as "metric", implying
    # per-metric series that never existed).
    if args.metric != "learning_value":
        result["metric_label"] = args.metric
        result["note"] = ("metric is a caller label; computation always "
                          "runs over the single learning_value series")

    print(json.dumps(result, ensure_ascii=False))


def cmd_snapshot(args):
    """Record a learning_value entry for a goal."""
    vel = read_yaml(META_DIR / "improvement-velocity.yaml")
    validate_velocity_structure(vel, "post-read")

    if "entries" not in vel:
        vel["entries"] = []

    # Per-close idempotency () — mirrors mind_api/src/meta/meta_impk.py
    # snapshot(). The key names the close EVENT, not the goal: a repeat snapshot
    # for the same goal is overwhelmingly LEGITIMATE (measured 2026-08-09 on
    # 7,814 live entries — 2,066 repeat rows, dominated by  recurring
    # closes,  alone n=205), so deduping on goal_id would destroy real
    # learning. Exact-match only; absent key -> unconditional append exactly as
    # before, so no existing caller changes behaviour.
    close_key = (getattr(args, "close_key", "") or "").strip()
    if close_key and any(
            isinstance(e, dict) and e.get("close_key") == close_key
            for e in vel["entries"]):
        # rc=0 on purpose: iteration-close.sh treats any non-zero rc from the
        # audit as "snapshot not recorded" and WARNs. Suppression is the system
        # working, so it must not read as a failure.
        print(json.dumps({"status": "duplicate_suppressed",
                          "goal_id": args.goal_id,
                          "learning_value": float(args.learning_value),
                          "close_key": close_key}))
        return

    entry = {
        "goal_id": args.goal_id,
        "date": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "learning_value": float(args.learning_value),
    }
    if close_key:
        entry["close_key"] = close_key
    if args.category:
        entry["category"] = args.category
    if args.active_changes:
        entry["active_meta_changes"] = [c.strip() for c in args.active_changes.split(",") if c.strip()]

    vel["entries"].append(entry)

    # Recompute rolling averages
    entries = vel["entries"]
    for w in [5, 10, 20]:
        key = f"window_{w}"
        if len(entries) >= w:
            avg = sum(e.get("learning_value", 0) for e in entries[-w:]) / w
            vel.setdefault("rolling_averages", {})[key] = round(avg, 4)
        else:
            vel.setdefault("rolling_averages", {})[key] = 0.0

    # Validate structure before write
    validate_velocity_structure(vel, "pre-write")

    write_yaml(META_DIR / "improvement-velocity.yaml", vel)
    print(json.dumps({"status": "recorded", "goal_id": args.goal_id, "learning_value": entry["learning_value"]}))


def build_parser():
    parser = argparse.ArgumentParser(description="Improvement velocity (imp@k)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_compute = sub.add_parser("compute", help="Compute imp@k over the learning_value series")
    p_compute.add_argument("--window", type=int, required=True, help="Rolling window size (k)")
    # A PARAMETER, NOT A NEW BURIED CONSTANT (rb-4082). The default is the
    # conventional ~2-sigma bar; a caller that wants a different sensitivity
    # says so and the emitted significance_threshold records which bar ran.
    p_compute.add_argument("--significance", type=float, default=SIGNIFICANCE_SE_MULTIPLE,
                           help="How many standard errors the recent-vs-older shift must "
                                "exceed before direction leaves 'stable' (default %(default)s)")
    # : there is exactly ONE series in the store (per-goal
    # learning_value snapshots). The old required --metric was decorative —
    # any name produced identical output, which misled the evolve Step 0.7
    # caller into believing pipeline_accuracy and goal_completion_rate were
    # independently tracked. Optional caller label now; output names the real
    # series and warns on non-default labels.
    p_compute.add_argument("--metric", default="learning_value",
                           help="Caller label only — computation ALWAYS runs "
                                "over the single learning_value series")

    p_snap = sub.add_parser("snapshot", help="Record learning value for a goal")
    p_snap.add_argument("--goal-id", required=True, help="Goal ID")
    p_snap.add_argument("--learning-value", required=True, help="Learning value (0-1)")
    p_snap.add_argument("--category", default="", help="Goal category")
    p_snap.add_argument("--active-changes", default="", help="Comma-separated mc-NNN IDs of active backpressure monitors")
    p_snap.add_argument("--close-key", default="",
                        help="Per-close idempotency token (g-115-4542). A second "
                             "snapshot carrying a close-key already present is "
                             "SUPPRESSED (rc=0) instead of double-weighting the "
                             "goal in the 5/10/20 rolling windows. Omit for the "
                             "legacy unconditional append.")

    return parser


DISPATCH = {
    "compute": cmd_compute,
    "snapshot": cmd_snapshot,
}


def main():
    parser = build_parser()
    args = parser.parse_args()
    DISPATCH[args.command](args)


if __name__ == "__main__":
    main()
