#!/usr/bin/env python3
"""L1 distribution skew detector (S1).

Reads `tree-read.sh --stats --by-l1` and flags TAXONOMY-shape defects a real
taxonomy action could fix (g-115-2455 recalibration):

  - dominance:   one L1 holds >= dominance_threshold (default 90%) of a
                 metric's total mass — a hoover bucket; satisfiable by a split.
  - share_creep: the dominant L1's share grew >= share_creep_pp (default 3.0
                 percentage points) since the last cadence fire while already
                 majority (>= 50%) — degradation trend; cadence mode only.
  - empty_l1:    a real L1 has zero nodes — dead weight; satisfiable by
                 populate-or-retire. (total_nodes metric only: derived metrics
                 like mature capability are legitimately 0 on young L1s.)

Max/min ratios are still computed and carried in every finding as EVIDENCE,
but they no longer gate flagging: a tiny-but-healthy L1 (e.g. a deliberate
low-mass strategic bet) makes the min-denominator unsatisfiable by any
realistic action — measured 2026-07-17: 53.6x with the best available split
still leaving 29.5x, producing ~5 board posts/24h of permanent alarm noise.

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


def _build_finding(metric, label, unit, values, dominance_threshold,
                   prev_shares, creep_pp, l1_count, allow_empty_flag):
    """Build one finding dict from sorted (l1, value) pairs.

    Flag reasons (first match wins):
      dominance   — max share >= dominance_threshold (needs >= 2 L1s AND a
                    metric total >= 10: shares over tiny totals are
                    quantization noise — a young tree's 3 matured nodes all
                    landing in one L1 is not a taxonomy defect)
      share_creep — share grew >= creep_pp percentage points vs the prior
                    cadence-fire baseline while already majority (>= 0.5)
      empty_l1    — a real L1 has value 0 (total_nodes metric only)

    Ratio fields are evidence-only — they never set `flagged` (g-115-2455).
    """
    min_l1, min_v = values[0]
    max_l1, max_v = values[-1]
    metric_total = sum(v for _, v in values)
    share = (max_v / metric_total) if metric_total > 0 else 0.0
    if min_v <= 0:
        ratio = float("inf") if max_v > 0 else 1.0
    else:
        ratio = max_v / min_v

    flag_reason = None
    if l1_count >= 2 and metric_total >= 10 and share >= dominance_threshold:
        flag_reason = "dominance"
    elif isinstance(prev_shares, dict) and metric in prev_shares:
        try:
            prev = float(prev_shares[metric])
        except (TypeError, ValueError):
            prev = None
        # Growth requires a POSITIVE delta — with creep_pp=0 a bare `>=`
        # would flag an unchanged share as "creep" (fresh-eyes F2).
        delta_pp = ((share - prev) * 100.0) if prev is not None else 0.0
        if prev is not None and share >= 0.5 and delta_pp > 0 \
                and delta_pp >= creep_pp:
            flag_reason = "share_creep"
    if flag_reason is None and allow_empty_flag and l1_count >= 2 and min_v == 0:
        flag_reason = "empty_l1"

    return {
        "metric": metric,
        "label": label,
        "unit": unit,
        "max_l1": max_l1,
        "max_value": max_v,
        "min_l1": min_l1,
        "min_value": min_v,
        "share": round(share, 4),
        "ratio": round(ratio, 2) if ratio != float("inf") else None,
        "ratio_infinite": ratio == float("inf"),
        "flag_reason": flag_reason,
        "flagged": flag_reason is not None,
    }


def compute_skew(by_l1, threshold, dominance_threshold=0.90,
                 prev_shares=None, creep_pp=3.0):
    """Compute per-metric findings. Return list of findings.

    Each finding: {metric, label, max_l1, max_value, min_l1, min_value,
    share, ratio, flag_reason, flagged}. Excludes `_orphan` bucket (it
    shouldn't be a target). `threshold` is retained for payload/API
    compatibility (ratio evidence context) but no longer gates flagging.
    """
    real_buckets = {k: v for k, v in by_l1.items() if k != "_orphan"}
    findings = []
    if not real_buckets:
        return findings
    l1_count = len(real_buckets)
    for key, label, unit in METRICS:
        values = [(l1, int(b.get(key, 0))) for l1, b in real_buckets.items()]
        values.sort(key=lambda x: x[1])
        if not values:
            continue
        findings.append(_build_finding(
            key, label, unit, values, dominance_threshold, prev_shares,
            creep_pp, l1_count,
            allow_empty_flag=(key == "total_nodes")))
    # Also track mature-capability mass — a thin L1 with no MASTER/EXPLOIT
    # is structurally different from a thin L1 that's just early-stage.
    # allow_empty_flag=False: a young-but-legitimate L1 has 0 matured nodes
    # for weeks; flagging that was part of the pre-recalibration noise.
    cap_values = [
        (l1, _mature_capability_count(b)) for l1, b in real_buckets.items()
    ]
    cap_values.sort(key=lambda x: x[1])
    if cap_values:
        findings.append(_build_finding(
            "mature_capability_mass",
            "mature capability mass (EXPLOIT+MASTER)",
            "matured nodes", cap_values, dominance_threshold, prev_shares,
            creep_pp, l1_count, allow_empty_flag=False))
    _annotate_structural_echo(findings, real_buckets)
    return findings


def _annotate_structural_echo(findings, real_buckets):
    """Mark metrics whose dominance is a mechanical echo of structural mass.

    MEASURED 2026-08-30 (bravo, cc-05, live tree, 4 L1s, 1526 nodes /
    157,534 retrievals): the structural-mass share and the retrieval-volume
    share CO-MOVE at Pearson r = 0.986, and the same L1 (`intelligence`)
    tops both — 76.0% of nodes and 62.7% of retrievals. A big L1 accumulates
    the nodes AND the retrievals, so two `dominance` findings naming the same
    L1 are ONE underlying fact, not two corroborating ones (g-115-4648; same
    class as rb-6425, which established per-node density for the S7 sibling).

    THE RAW TOTAL IS DELIBERATE AND IS NOT REPLACED, because normalizing it
    would make the metric structurally unable to fire: per-node density on
    that same tree is near-even (13.5 / 23.3 / 32.5 / 30.6%, max 32.5%)
    against a 0.90 dominance ceiling, so a normalized metric could never
    reach the threshold. Swapping it would convert a visibly-redundant
    detector into an apparently-fixed inert one (guard-2499).

    So the echo is surfaced as EVIDENCE instead, in the payload a consumer
    actually reads — a source comment alone would not reduce firings or reach
    anything downstream (guard-4649). Evidence-only: this never sets
    `flagged`, matching the existing ratio-field contract.

    Density is carried alongside because it is the genuinely independent
    signal, and it INVERTS the raw ranking: `intelligence` leads raw
    retrievals while holding the LOWEST density (85.2/node) against
    execution's 204.9. A reader comparing L1s wants that number, not the
    total.
    """
    base = next((f for f in findings if f["metric"] == "total_nodes"), None)
    if not base:
        return
    nodes_by_l1 = {l1: int(b.get("total_nodes", 0))
                   for l1, b in real_buckets.items()}
    for f in findings:
        if f["metric"] == "total_nodes":
            continue
        f["echoes_structural_mass"] = (f["max_l1"] == base["max_l1"])
        if f["metric"] == "total_retrieval_count":
            n = nodes_by_l1.get(f["max_l1"], 0)
            f["max_l1_per_node"] = round(f["max_value"] / n, 2) if n else None
            densities = {
                l1: (int(b.get("total_retrieval_count", 0)) / nodes_by_l1[l1])
                for l1, b in real_buckets.items() if nodes_by_l1.get(l1)
            }
            if densities:
                top = max(densities, key=densities.get)
                f["densest_l1"] = top
                f["densest_l1_per_node"] = round(densities[top], 2)


def render_markdown(verdict):
    """Human-readable table for terminal / board posts."""
    lines = []
    lines.append("## L1 distribution skew check — {}".format(verdict["ts"]))
    lines.append("Dominance ceiling: {:.0f}%. Status: {}".format(
        100.0 * verdict.get("dominance_threshold", 0.90),
        "FLAGGED — review L1 boundaries" if verdict["any_flagged"]
        else "balanced",
    ))
    lines.append("")
    lines.append("| Metric | Max L1 | Min L1 | Max | Min | Share | Ratio | Flagged |")
    lines.append("|---|---|---|---:|---:|---:|---:|:--:|")
    for f in verdict["findings"]:
        ratio = "inf" if f["ratio_infinite"] else str(f["ratio"])
        flag = f.get("flag_reason") or " "
        share_pct = "{:.1f}%".format(100.0 * f.get("share", 0.0))
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} |".format(
            f["label"], f["max_l1"], f["min_l1"],
            f["max_value"], f["min_value"], share_pct, ratio, flag,
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
            "  - {} [{}]: {}={} ({:.1f}% share) vs {}={} (ratio {})".format(
                f["label"], f.get("flag_reason", "?"),
                f["max_l1"], f["max_value"], 100.0 * f.get("share", 0.0),
                f["min_l1"], f["min_value"], ratio,
            )
        )
    body_lines.append(
        "Flag basis: dominance >= {:.0f}% share / share_creep / empty_l1 "
        "(g-115-2455). Review L1 boundaries via /fresh-eyes-tree.".format(
            100.0 * verdict.get("dominance_threshold", 0.90)))
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

    Returns dict with goal_cadence, wm_slot, dominance_threshold, and
    share_creep_pp, or None on read error. Defaults: goal_cadence=50,
    wm_slot='last_l1_skew_check', dominance_threshold=0.90,
    share_creep_pp=3.0.
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
            "dominance_threshold": float(
                block.get("dominance_threshold", 0.90)),
            "share_creep_pp": float(block.get("share_creep_pp", 3.0)),
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
    """Return (fire, current, cfg, last_slot). fire=True when cadence crossed.

    last_slot is the prior WM slot dict ({} when unset) — carried out so the
    caller can use its `shares` baseline for share_creep (g-115-2455).

    Reads l1_skew_check.goal_cadence and last-fire counter from WM. On first
    fire (slot unset) the diff is capped at the cadence so the ritual reads
    as "exactly due" rather than "infinitely overdue" — mirrors the
    fresh-eyes-cadence-check first-fire normalization (g-001-190).
    """
    cfg = _load_cadence_config()
    if cfg is None:
        return False, None, None, {}
    current = _count_completed_goals()
    last = _wm_read(cfg["wm_slot"]) or {}
    # Defensive: a legacy/restored slot may hold a bare timestamp string (the
    # pre-dict-migration shape) instead of the {goals_count_at_last_fire: N}
    # dict _record_fire now writes. Without this guard last.get() raises
    # AttributeError and the gate crashes on EVERY fire — and because the crash
    # precedes _record_fire (which writes the dict), the slot can never
    # self-heal: a permanent migration deadlock that silently disabled the L1
    # skew ritual. Coercing a non-dict to {} reseeds via the first-fire path
    # below, after which _record_fire overwrites with the correct dict shape.
    # (rb-810 type-guard-before-method-call; -session l1-skew fix.)
    if not isinstance(last, dict):
        last = {}
    last_count = int(last.get("goals_count_at_last_fire", 0) or 0)
    diff = current - last_count
    if last_count == 0:
        diff = min(diff, cfg["goal_cadence"])
    # Negative-diff self-heal ( pattern, ported here by ).
    # A DOWNWARD count-basis correction (census double-count repair, store
    # surgery, archival) leaves the stamped slot ABOVE the live count. Without
    # this branch diff stays negative, `fire` is permanently False, and the
    # ritual SILENTLY STARVES until the count regrows past the stale stamp.
    #
    # The type-guard above (rb-810) fixed a DIFFERENT permanent-disable of this
    # same ritual — a legacy slot shape that crashed the gate. This is the third
    # way the same gate can go quietly dead, and it presents identically to a
    # healthy "cadence not crossed": no crash, no warning, just silence. A gate
    # that cannot distinguish "not due" from "never again" is not a gate.
    #
    # fresh-eyes-cadence-check.py got this heal first ( per-agent,
    #  team-layer). Its two siblings (this file, felt-sense-cadence-
    # check.py) HAVE SINCE BEEN HEALED TOO — verified 2026-08-28 (alpha, cc-04):
    # the `diff < 0` branch is present in all three (fresh-eyes:376,
    # felt-sense:236, and below). This paragraph read "its two siblings never
    # did" until then, which sent a reader looking for two starving rituals that
    # no longer exist — a dated waypoint left in the present tense, the same
    # already-fixed-work trap as rb-9574. The 2026-07-14 measurement below is
    # kept as the ORIGINATING evidence, not as current state: felt-sense sat at
    # diff=-335 and needed 410 more completed goals before it could fire.
    #
    # Re-stamp to the current count and DO NOT fire. Firing here would trade a
    # starved ritual for one that fires on every basis correction (banner
    # fatigue, guard-1090). Preserve the last REAL fire timestamp — a
    # re-baseline is not a fire and must not masquerade as one. _wm_set already
    # fails safe (logs to stderr, never raises), so a write failure just means
    # the heal retries on the next cadence check.
    if diff < 0:
        # ZERO-GUARD (guard-1091; fresh-eyes-code F-001, 2026-07-14). A FAILED
        # measurement is not a measurement of ZERO. _count_completed_goals()
        # returns 0 as a SILENT FAILURE SENTINEL on EVERY error path it has:
        # subprocess rc != 0, a 10s TimeoutExpired, OSError, and unparseable
        # stdout ALL `return 0`. That is four routine ways to get a fake zero.
        #
        # Re-baselining on it would PERSIST the transient failure as the new
        # basis (goals_count_at_last_fire=0) and then SPURIOUSLY FIRE next
        # iteration via the last_count==0 first-fire path. Note the asymmetry:
        # BEFORE this heal existed a transient 0 was HARMLESS — diff<0 =>
        # fire=False => noop => NO WRITE => self-recovering. The heal must not
        # convert a self-recovering error into permanent state corruption.
        # Noop WITHOUT re-stamping; the next cadence check retries.
        #
        # `current == 0` inside `diff < 0` already implies `last_count > 0`, so
        # this cannot mask a legitimate basis: a real count never falls to zero
        # (it folds in archives + census and is eviction-invariant). A genuinely
        # empty store self-heals the moment ONE goal completes (current >= 1
        # takes the re-baseline below).
        if current == 0:
            print(
                f"[l1-skew-check] negative diff ({diff}) with current=0 vs "
                f"last={last_count} — FAILED MEASUREMENT, not a real basis "
                f"(_count_completed_goals returns 0 on subprocess failure/timeout); "
                f"noop WITHOUT re-stamp — retries next check",
                file=sys.stderr,
            )
            return False, current, cfg, last
        rebase = {
            "timestamp": last.get("timestamp", "0000-00-00T00:00:00"),
            "goals_count_at_last_fire": current,
            "rebaselined_from": last_count,
        }
        # Preserve the share baseline across a re-baseline — a count-basis
        # correction says nothing about tree shape, and dropping the shares
        # would blind the next fire's share_creep comparison ().
        if isinstance(last.get("shares"), dict):
            rebase["shares"] = last["shares"]
        _wm_set(cfg["wm_slot"], rebase)
        print(
            f"[l1-skew-check] negative diff ({diff}) — count basis moved backward "
            f"(last={last_count} > current={current}); re-baselined "
            f"{cfg['wm_slot']} to {current} — noop this iter"
        )
        return False, current, cfg, last
    fire = diff >= cfg["goal_cadence"]
    return fire, current, cfg, last


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--threshold", type=float, default=5.0,
                    help="Ratio evidence context carried in the payload "
                         "(no longer gates flagging — g-115-2455)")
    ap.add_argument("--dominance-threshold", type=float, default=None,
                    help="Max L1 share-of-total that flags dominance "
                         "(default: config or 0.90)")
    ap.add_argument("--share-creep-pp", type=float, default=None,
                    help="Percentage-point share growth vs last cadence fire "
                         "that flags share_creep (default: config or 3.0)")
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
    prev_slot = {}
    cfg = None
    if args.cadence:
        fire, current, cfg, prev_slot = _cadence_gate()
        if not fire:
            # Silent noop — periodic callers expect quiet on no-fire.
            sys.exit(1)
        # Fall through to normal flow; record fire AFTER successful stats read.

    # Resolve flag thresholds: CLI > config (cadence mode) > defaults.
    dominance_threshold = args.dominance_threshold
    if dominance_threshold is None:
        dominance_threshold = (cfg or {}).get("dominance_threshold", 0.90)
    creep_pp = args.share_creep_pp
    if creep_pp is None:
        creep_pp = (cfg or {}).get("share_creep_pp", 3.0)
    # share_creep baseline exists only across cadence fires (the WM slot).
    prev_shares = prev_slot.get("shares") if isinstance(prev_slot, dict) else None
    if not isinstance(prev_shares, dict):
        prev_shares = None

    stats = _read_stats()
    if not stats:
        verdict = {
            "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "threshold": args.threshold,
            "dominance_threshold": dominance_threshold,
            "share_creep_pp": creep_pp,
            "findings": [],
            "any_flagged": False,
            "notes": "stats read failed — no skew computation",
        }
    else:
        by_l1 = stats.get("by_l1") or {}
        findings = compute_skew(by_l1, args.threshold,
                                dominance_threshold=dominance_threshold,
                                prev_shares=prev_shares,
                                creep_pp=creep_pp)
        verdict = {
            "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "threshold": args.threshold,
            "dominance_threshold": dominance_threshold,
            "share_creep_pp": creep_pp,
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
            # share_creep baseline for the NEXT fire ()
            "shares": {f["metric"]: f.get("share", 0.0)
                       for f in verdict.get("findings", [])},
        }
        _wm_set(cfg["wm_slot"], slot_value)

    # Exit code: 0 fired/normal, 1 cadence-noop (handled earlier), 2 stats error.
    # Periodic callers (aspirations-precheck) treat exit 1 as silent noop.
    sys.exit(0 if stats else 2)


if __name__ == "__main__":
    main()
