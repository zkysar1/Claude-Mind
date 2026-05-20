#!/usr/bin/env python3
"""L1 distribution skew detector (S1).

Reads `tree-read.sh --stats --by-l1`, computes max/min ratios across L1s
for structural mass, retrieval volume, and mature capability mass, and
reports skew above a configurable threshold (default 5x).

Output is JSON by default — a minimal verdict carrying the ratios and the
flagged metrics. Pass --post-board to ALSO post a coordination-channel
message when any tracked ratio exceeds the threshold, so the team has
awareness without a dedicated email.

Designed to be called from a periodic site (aspirations-precheck cadence,
recurring goal, or manually). Fail-open: a read error or empty tree
produces a non-flagged verdict with a `notes` field, not an exception.

Usage:
    py -3 core/scripts/l1-skew-check.py                # JSON to stdout
    py -3 core/scripts/l1-skew-check.py --threshold 3.0
    py -3 core/scripts/l1-skew-check.py --post-board   # board post on skew
    py -3 core/scripts/l1-skew-check.py --markdown     # human-readable table
    py -3 core/scripts/l1-skew-check.py --cadence      # cadence-gated (50 goals)

--cadence is the periodic-caller mode. It reads `core/config/aspirations.yaml`
→ `l1_skew_check.goal_cadence` and the WM slot named in `wm_slot`, fires
the check only if cadence crossed, and updates the slot on fire. Designed
to be called once per aspirations-precheck iteration as a noop-default
periodic gate. Exit 0 = fired, 1 = noop, 2 = stats read error.
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# : force utf-8 on stdin/stdout/stderr
from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()

from _paths import PROJECT_ROOT, CORE_ROOT

import pathlib as _pathlib
_SD = _pathlib.Path(__file__).resolve().parent
if str(_SD) not in sys.path:
    sys.path.insert(0, str(_SD))
import _rt  # canonical Python -> daemon client (post-cutover; see _rt.py)

# In-process tree read + stats compute. Single source of truth for the
# by_l1 numbers — no subprocess to tree.py, no parallel JSON parser.
from tree import safe_read_tree, compute_stats


METRICS = [
    # (key in by_l1 bucket, friendly label, weight in summary)
    ("total_nodes", "structural mass", "nodes"),
    ("leaf_count", "leaf mass", "leaves"),
    ("total_retrieval_count", "retrieval volume", "retrievals"),
]


def _read_stats():
    """Read tree + compute by-l1 stats in-process. Returns None on missing
    or invalid tree (fail-open)."""
    tree = safe_read_tree()
    if tree is None:
        return None
    return compute_stats(tree, by_l1=True)


def _mature_capability_count(bucket):
    """Sum of EXPLOIT + MASTER counts — the 'matured' capability mass."""
    cm = bucket.get("capability_mass", {})
    return int(cm.get("EXPLOIT", 0)) + int(cm.get("MASTER", 0))


def compute_skew(by_l1, threshold):
    """Compute max/min ratios per metric. Return list of findings.

    Each finding: {metric, label, max_l1, max_value, min_l1, min_value, ratio, flagged}.
    Excludes `_orphan` bucket from ratio math (it shouldn't be a target).
    """
    real_buckets = {k: v for k, v in by_l1.items() if k != "_orphan"}
    findings = []
    if not real_buckets:
        return findings
    for key, label, unit in METRICS:
        values = [(l1, int(b.get(key, 0))) for l1, b in real_buckets.items()]
        values.sort(key=lambda x: x[1])
        if not values:
            continue
        min_l1, min_v = values[0]
        max_l1, max_v = values[-1]
        if min_v <= 0:
            # Use a sentinel: ratio undefined, but if max is non-zero this is
            # an extreme imbalance worth flagging.
            ratio = float("inf") if max_v > 0 else 1.0
        else:
            ratio = max_v / min_v
        findings.append({
            "metric": key,
            "label": label,
            "unit": unit,
            "max_l1": max_l1,
            "max_value": max_v,
            "min_l1": min_l1,
            "min_value": min_v,
            "ratio": round(ratio, 2) if ratio != float("inf") else None,
            "ratio_infinite": ratio == float("inf"),
            "flagged": (ratio == float("inf")) or (ratio >= threshold),
        })
    # Also track mature-capability mass — a thin L1 with no MASTER/EXPLOIT
    # is structurally different from a thin L1 that's just early-stage.
    cap_values = [
        (l1, _mature_capability_count(b)) for l1, b in real_buckets.items()
    ]
    cap_values.sort(key=lambda x: x[1])
    if cap_values:
        min_l1, min_v = cap_values[0]
        max_l1, max_v = cap_values[-1]
        ratio = float("inf") if min_v == 0 and max_v > 0 else (
            max_v / min_v if min_v > 0 else 1.0)
        findings.append({
            "metric": "mature_capability_mass",
            "label": "mature capability mass (EXPLOIT+MASTER)",
            "unit": "matured nodes",
            "max_l1": max_l1,
            "max_value": max_v,
            "min_l1": min_l1,
            "min_value": min_v,
            "ratio": round(ratio, 2) if ratio != float("inf") else None,
            "ratio_infinite": ratio == float("inf"),
            "flagged": (ratio == float("inf")) or (ratio >= threshold),
        })
    return findings


def render_markdown(verdict):
    """Human-readable table for terminal / board posts."""
    lines = []
    lines.append("## L1 distribution skew check — {}".format(verdict["ts"]))
    lines.append("Threshold: {}x. Status: {}".format(
        verdict["threshold"],
        "FLAGGED — review L1 boundaries" if verdict["any_flagged"]
        else "balanced",
    ))
    lines.append("")
    lines.append("| Metric | Max L1 | Min L1 | Max | Min | Ratio | Flagged |")
    lines.append("|---|---|---|---:|---:|---:|:--:|")
    for f in verdict["findings"]:
        ratio = "inf" if f["ratio_infinite"] else str(f["ratio"])
        flag = "YES" if f["flagged"] else " "
        lines.append("| {} | {} | {} | {} | {} | {} | {} |".format(
            f["label"], f["max_l1"], f["min_l1"],
            f["max_value"], f["min_value"], ratio, flag,
        ))
    if verdict.get("notes"):
        lines.append("")
        lines.append("Notes: " + verdict["notes"])
    return "\n".join(lines)


def _post_board(verdict):
    """Post a coordination-channel finding via board.py directly (sys.executable
    + board.py), NOT via the bash wrapper.

    On Windows, subprocess.run(['bash', wrapper, ...]) resolves bare 'bash'
    through CreateProcess's app-dir -> SYSTEM32 -> PATH order; SYSTEM32's WSL
    bash stub wins, cannot resolve C:/... paths, and returns rc=127 with no
    useful output. Same defect class as guard-468 / rb-577 / rb-168 (Python
    subprocess invoking .sh on Windows). Mirrors the
    cargo-cult-detector.py:reset_consecutive_routine pattern (sys.executable
    + <target>.py direct).

    Single source of truth: board.py is the daemon-aware writer; bypassing
    the .sh wrapper for the Python caller is correct on every platform — the
    wrapper's only added value was bash-side arg shuffling, which board.py
    reproduces internally. board.py's argparse uses --tags (comma-separated,
    single arg), which is what we already pass.
    """
    body_lines = ["L1 distribution skew detected:"]
    for f in verdict["findings"]:
        if not f["flagged"]:
            continue
        ratio = "inf" if f["ratio_infinite"] else str(f["ratio"])
        body_lines.append(
            "  - {}: {}={} vs {}={} (ratio {})".format(
                f["label"], f["max_l1"], f["max_value"],
                f["min_l1"], f["min_value"], ratio,
            )
        )
    body_lines.append(
        "Threshold: {}x. Review L1 boundaries via /fresh-eyes-tree.".format(
            verdict["threshold"]))
    body = "\n".join(body_lines)
    board_py = str(Path(CORE_ROOT) / "scripts" / "board.py")
    try:
        subprocess.run(
            [sys.executable, board_py, "post",
             "--channel", "coordination",
             "--type", "finding",
             "--tags", "l1-skew,tree-taxonomy"],
            input=body,
            cwd=str(PROJECT_ROOT),
            text=True,
            check=False,
            timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        print("[l1-skew-check] board post failed: " + str(e), file=sys.stderr)


def _load_cadence_config():
    """Load l1_skew_check config block from aspirations.yaml.

    Returns dict with goal_cadence and wm_slot, or None on read error.
    Defaults: goal_cadence=50, wm_slot='last_l1_skew_check'.
    """
    try:
        import yaml
        cfg_path = Path(PROJECT_ROOT) / "core" / "config" / "aspirations.yaml"
        if not cfg_path.exists():
            return None
        with open(str(cfg_path), "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        block = cfg.get("l1_skew_check") or {}
        return {
            "goal_cadence": int(block.get("goal_cadence", 50)),
            "wm_slot": str(block.get("wm_slot", "last_l1_skew_check")),
        }
    except Exception as e:
        print("[l1-skew-check] cadence config read failed: " + str(e),
              file=sys.stderr)
        return None


def _count_completed_goals():
    """Total completed goals across world + agent. Uses fresh-eyes-cadence-check's
    helper to stay consistent with the other cadence rituals."""
    try:
        # Reuse the existing helper — same definition of "completed" across rituals.
        script = str(Path(CORE_ROOT) / "scripts" / "fresh-eyes-cadence-check.py")
        result = subprocess.run(
            [sys.executable, script, "--print-current"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return 0
        return int(result.stdout.strip() or 0)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return 0


def _wm_read(slot):
    """Read WM slot via daemon (post-cutover; wm.py read CLI was deleted)."""
    try:
        raw = _rt.wm_read(slot=slot, as_json=True)
        raw = (raw or "").strip()
        if not raw or raw == "null":
            return None
        return json.loads(raw)
    except _rt.RtError:
        return None
    except (json.JSONDecodeError, Exception):
        return None


def _wm_set(slot, value):
    try:
        wm_script = str(Path(CORE_ROOT) / "scripts" / "wm.py")
        subprocess.run(
            [sys.executable, wm_script, "set", slot],
            input=json.dumps(value),
            capture_output=True, text=True, check=True, timeout=10,
        )
    except Exception as e:
        print("[l1-skew-check] wm-set failed: " + str(e), file=sys.stderr)


def _cadence_gate():
    """Return True if cadence is crossed and we should fire.

    Reads l1_skew_check.goal_cadence and last-fire counter from WM. On first
    fire (slot unset) the diff is capped at the cadence so the ritual reads
    as "exactly due" rather than "infinitely overdue" — mirrors the
    fresh-eyes-cadence-check first-fire normalization (g-001-190).
    """
    cfg = _load_cadence_config()
    if cfg is None:
        return False, None, None
    current = _count_completed_goals()
    last = _wm_read(cfg["wm_slot"]) or {}
    last_count = int(last.get("goals_count_at_last_fire", 0) or 0)
    diff = current - last_count
    if last_count == 0:
        diff = min(diff, cfg["goal_cadence"])
    fire = diff >= cfg["goal_cadence"]
    return fire, current, cfg


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--threshold", type=float, default=5.0,
                    help="Skew ratio threshold (default: 5.0)")
    ap.add_argument("--post-board", action="store_true",
                    help="Post to coordination channel when any metric flagged")
    ap.add_argument("--markdown", action="store_true",
                    help="Output human-readable markdown table instead of JSON")
    ap.add_argument("--cadence", action="store_true",
                    help=("Periodic-caller mode: only fire when "
                          "l1_skew_check.goal_cadence (default 50) goals "
                          "elapsed since last fire. Updates the WM slot on "
                          "fire. Exit 0 fired / 1 noop / 2 stats error."))
    args = ap.parse_args()

    # Cadence gate: check BEFORE doing the read, so the noop path is cheap.
    if args.cadence:
        fire, current, cfg = _cadence_gate()
        if not fire:
            # Silent noop — periodic callers expect quiet on no-fire.
            sys.exit(1)
        # Fall through to normal flow; record fire AFTER successful stats read.

    stats = _read_stats()
    if not stats:
        verdict = {
            "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "threshold": args.threshold,
            "findings": [],
            "any_flagged": False,
            "notes": "stats read failed — no skew computation",
        }
    else:
        by_l1 = stats.get("by_l1") or {}
        findings = compute_skew(by_l1, args.threshold)
        verdict = {
            "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "threshold": args.threshold,
            "l1_count": len([k for k in by_l1 if k != "_orphan"]),
            "total_nodes": stats.get("total_nodes", 0),
            "findings": findings,
            "any_flagged": any(f["flagged"] for f in findings),
        }
        if "_orphan" in by_l1 and by_l1["_orphan"].get("total_nodes", 0) > 0:
            verdict["notes"] = (
                "{} orphan node(s) detected — walk does not reach an L1. "
                "Investigate ancestor chain corruption.".format(
                    by_l1["_orphan"]["total_nodes"]))

    if args.markdown:
        print(render_markdown(verdict))
    else:
        print(json.dumps(verdict, indent=2, ensure_ascii=False))

    if args.post_board and verdict.get("any_flagged"):
        _post_board(verdict)

    # In cadence mode, record this fire in the WM slot so the next call's
    # diff math is correct. Done after the board post so a board failure
    # doesn't suppress the slot update (cadence is per-fire, not per-success).
    if args.cadence and stats:
        slot_value = {
            "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "goals_count_at_last_fire": current,
            "any_flagged": verdict.get("any_flagged", False),
        }
        _wm_set(cfg["wm_slot"], slot_value)

    # Exit code: 0 fired/normal, 1 cadence-noop (handled earlier), 2 stats error.
    # Periodic callers (aspirations-precheck) treat exit 1 as silent noop.
    sys.exit(0 if stats else 2)


if __name__ == "__main__":
    main()
