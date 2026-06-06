#!/usr/bin/env python3
"""health-ledger-query.py — read-only inspection of the self-health ledger.

Spec: core/config/conventions/health-ledger.md. A side-effect-free reporting
utility for an agent (or the user) to answer "how is the ledger doing?" — the
latest records, the composite trend, calibration progress toward the Phase-3
revert AND-gate, the current mode, and the consecutive-below-baseline streak.
Useful first for verifying Phase-1 collection is actually accumulating records.

  python3 health-ledger-query.py                 # human summary (default)
  python3 health-ledger-query.py --json          # machine-readable
  python3 health-ledger-query.py --recent 5      # last N records in the summary
  python3 health-ledger-query.py --agent bravo   # inspect another agent (read-only)

Read-only: never writes the interval marker, never files goals, never reverts.
Fail-open: any error yields an empty/None summary, exit 0.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import _health_ledger as hl  # noqa: E402


def _eprint(msg):
    print(f"[health-ledger-query] {msg}", file=sys.stderr)


def _load_health_config(config_dir):
    """mode + calibration thresholds; tolerant defaults."""
    cfg = {"mode": "collect-only", "calibration": {"min_days": 30, "min_records": 50}}
    try:
        import yaml
        with open(Path(config_dir) / "aspirations.yaml", encoding="utf-8") as f:
            full = yaml.safe_load(f) or {}
        hr = full.get("health_regression") or {}
        if hr.get("mode"):
            cfg["mode"] = hr["mode"]
        if isinstance(hr.get("calibration"), dict):
            cfg["calibration"].update(hr["calibration"])
    except Exception as e:  # noqa: BLE001 — tolerant
        _eprint(f"config read failed ({e}) — defaults")
    return cfg


def build_summary(records, cfg, health_dir=None):
    """Pure: assemble the query summary from ledger records + health config.
    `records` newest-first (as from hl.recent_records).

    Calibration day/record counts come from `full_calibration_progress(health_dir)`
    (the WHOLE ledger) when `health_dir` is given — the bounded `records` window
    under-counts days for an agent recording many iterations/day, exactly the
    sliding-window bug the full scan exists to avoid. When `health_dir` is None
    (pure/test path) it falls back to the windowed count."""
    cal = cfg["calibration"]
    win_days, count = hl.calibration_progress(records)   # count = records in window
    if health_dir is not None:
        days, cal_records = hl.full_calibration_progress(health_dir)
    else:
        days, cal_records = win_days, count
    calibrated = days >= cal["min_days"] and cal_records >= cal["min_records"]
    latest = hl.latest_nonwarmup(records)
    return {
        "mode": cfg["mode"],
        "record_count": count,
        "latest": ({
            "ts": latest.get("ts"),
            "iteration": latest.get("iteration"),
            "composite": latest.get("composite"),
            "composite_trend": latest.get("composite_trend"),
            "baseline": latest.get("baseline"),
            "below_baseline": latest.get("below_baseline"),
        } if latest else None),
        "consecutive_below_baseline": hl.consecutive_below_baseline(records),
        "calibration": {
            "days": days, "records": cal_records,
            "min_days": cal["min_days"], "min_records": cal["min_records"],
            "complete": calibrated,
            "days_remaining": max(0, cal["min_days"] - days),
            "records_remaining": max(0, cal["min_records"] - cal_records),
        },
    }


def _render(summary, recent_records, n):
    lines = []
    lines.append("═══ HEALTH LEDGER ═══════════════════════════")
    lines.append(f"Mode: {summary['mode']}   Records: {summary['record_count']}")
    lat = summary["latest"]
    if lat:
        lines.append(f"Latest: composite={lat['composite']} trend={lat['composite_trend']} "
                     f"baseline={lat['baseline']} below={lat['below_baseline']} "
                     f"(iter {lat['iteration']}, {lat['ts']})")
    else:
        lines.append("Latest: (no non-warmup records yet)")
    lines.append(f"Consecutive below baseline: {summary['consecutive_below_baseline']}")
    cal = summary["calibration"]
    status = "COMPLETE" if cal["complete"] else \
        f"{cal['days']}/{cal['min_days']} days, {cal['records']}/{cal['min_records']} records"
    lines.append(f"Calibration (Phase-3 revert gate): {status}")
    if not cal["complete"]:
        lines.append(f"  remaining: {cal['days_remaining']} days AND {cal['records_remaining']} records")
    if recent_records:
        lines.append(f"Recent {min(n, len(recent_records))} records (newest first):")
        for r in recent_records[:n]:
            wm = " [warmup]" if r.get("warmup") else ""
            lines.append(f"  {r.get('ts', '?')}  comp={r.get('composite')} "
                         f"trend={r.get('composite_trend')} below={r.get('below_baseline')}{wm}")
    lines.append("═════════════════════════════════════════════")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Read-only health-ledger inspection.")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--recent", type=int, default=5, help="N recent records to show (default 5)")
    ap.add_argument("--agent", help="inspect a specific agent (default: bound agent)")
    args = ap.parse_args()

    agent = (args.agent or os.environ.get("MIND_AGENT", "")).strip()
    if not agent:
        print(json.dumps({"error": "no agent"}) if args.json else "No agent bound.")
        return 0

    try:
        import _paths
        config_dir = _paths.CONFIG_DIR
        agent_dir = _paths.agent_dir(agent)
    except Exception as e:  # noqa: BLE001 — fail-open
        print(json.dumps({"error": str(e)}) if args.json else f"path resolution failed ({e})")
        return 0

    cfg = _load_health_config(config_dir)
    health_dir = Path(agent_dir) / "health"
    records = hl.recent_records(health_dir, 200)
    summary = build_summary(records, cfg, health_dir=health_dir)

    if args.json:
        summary["agent"] = agent
        summary["recent"] = records[:args.recent]
        print(json.dumps(summary, ensure_ascii=True, indent=2))
    else:
        print(_render(summary, records, args.recent))
    return 0


if __name__ == "__main__":
    sys.exit(main())
