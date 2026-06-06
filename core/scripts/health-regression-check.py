#!/usr/bin/env python3
"""health-regression-check.py — health-regression detection sweep (Phase 2).

Spec: core/config/conventions/health-ledger.md §8 (detection gate), §9
(attribution), §10 (calibration). Design: workflow wf_b82a26e9-958.

A budget-metered, medium-tier deferrable sweep meant to run from
aspirations-precheck (Phase 0.5h) every `health_regression.detection.interval`
goals. It evaluates the latest non-warmup ledger record against the
triple-condition gate (slope < threshold AND composite < floor AND
below_baseline) PLUS a consecutive-below-baseline counter (one-off bad
iterations do not trip). On a trip it identifies the most-degraded component
signal, runs change-attribution over the regression window, checks the
evolution-log for an in-window meta-strategy change (so the report never blames
an intended evolution experiment), and emits a structured verdict.

PHASE 2 = DETECT-AND-REPORT. This script NEVER reverts and never files goals
itself — it is a side-effect-light DETECTOR (its only write is the interval
marker). The caller (aspirations-precheck) files the `Investigate:` goal from
the emitted verdict. Reverts (Phase 3) are gated by `mode == full` AND the
calibration AND-gate, which this script reports but does not act on.

Output (`--json`): a verdict object. Exit 0 always (fail-open).

  python3 health-regression-check.py --json
  python3 health-regression-check.py --json --force   # ignore the interval marker
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import _health_ledger as hl  # noqa: E402 — shared ledger reader (SSOT)

_TREND_WINDOW = 10
_SIGNAL_KEYS = ("composite_productivity", "encoding_ratio", "deep_ratio", "tree_writes")

# Stable dedup token for the one-time calibration-complete goal (spec §10). Kept
# global (no agent suffix) so the FIRST agent to calibrate files a single
# team-wide goal and later agents dedup against it — the mode flip is global
# config, so one goal suffices.
_CALIBRATION_DEDUP_KEY = "health-calibration-complete"

_DEFAULTS = {
    "mode": "collect-only",
    "tree_writes_cap": 8,
    "detection": {
        "interval": 10,
        "slope_threshold": -0.02,
        "floor": 0.40,
        "consecutive_below_baseline": 3,
    },
    "calibration": {"min_days": 30, "min_records": 50},
    "revert": {"confidence_gate": 0.70},
}


def _eprint(msg):
    print(f"[health-regression-check] {msg}", file=sys.stderr)


def _load_config(config_dir):
    cfg = json.loads(json.dumps(_DEFAULTS))  # deep copy
    try:
        import yaml
        with open(Path(config_dir) / "aspirations.yaml", encoding="utf-8") as f:
            full = yaml.safe_load(f) or {}
        hr = full.get("health_regression") or {}
        if hr.get("mode"):
            cfg["mode"] = hr["mode"]
        if hr.get("tree_writes_cap") is not None:
            cfg["tree_writes_cap"] = hr["tree_writes_cap"]
        for sub in ("detection", "calibration", "revert"):
            if isinstance(hr.get(sub), dict):
                cfg[sub].update(hr[sub])
    except Exception as e:  # noqa: BLE001 — fail-open
        _eprint(f"config read failed ({e}) — defaults")
    return cfg


def evaluate_gate(record, det):
    """Pure: do the THREE conditions hold on this latest non-warmup record?
    (consecutive counter is evaluated separately.) Returns (tripped, reasons)."""
    if record is None:
        return False, ["no non-warmup record"]
    reasons = []
    trend = record.get("composite_trend")
    comp = record.get("composite")
    slope_ok = isinstance(trend, (int, float)) and trend < det["slope_threshold"]
    floor_ok = isinstance(comp, (int, float)) and comp < det["floor"]
    below_ok = bool(record.get("below_baseline"))
    if not slope_ok:
        reasons.append(f"slope {trend} >= {det['slope_threshold']}")
    if not floor_ok:
        reasons.append(f"composite {comp} >= floor {det['floor']}")
    if not below_ok:
        reasons.append("not below_baseline")
    return (slope_ok and floor_ok and below_ok), reasons


def _norm_signal(key, raw, cap):
    try:
        fv = float(raw)
    except (TypeError, ValueError):
        return None
    if key == "tree_writes":
        return min(fv / cap, 1.0) if cap else 1.0
    return max(0.0, min(fv, 1.0))


def degraded_signal(records, cap):
    """The component signal with the steepest negative slope over the last
    _TREND_WINDOW non-warmup records (oldest->newest), normalized. Falls back to
    'composite_productivity' (the dominant term) when slopes are unavailable or
    none is negative. `records` newest-first."""
    series = [r for r in records
              if not r.get("warmup") and isinstance(r.get("signals"), dict)][:_TREND_WINDOW]
    series = list(reversed(series))  # oldest->newest
    if len(series) < 2:
        return "composite_productivity"
    steepest_key, steepest_slope = None, 0.0
    for key in _SIGNAL_KEYS:
        ys = []
        for r in series:
            v = _norm_signal(key, r["signals"].get(key), cap)
            if v is not None:
                ys.append(v)
        if len(ys) < 2:
            continue
        slope = hl.lsq_slope(ys)
        if slope < steepest_slope:
            steepest_slope, steepest_key = slope, key
    return steepest_key or "composite_productivity"


def _evolution_change_in_window(meta_dir, after_ts, before_ts):
    """True iff meta/evolution-log.jsonl has an entry timestamped within the
    regression window — so the report can flag that the dip may be an intended
    evolution experiment, not a bug (spec §8). Tolerant/fail-open to False."""
    try:
        path = Path(meta_dir) / "evolution-log.jsonl"
        if not path.exists():
            return False
        for ln in path.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
            except json.JSONDecodeError:
                continue
            ts = rec.get("timestamp") or rec.get("ts")
            if not isinstance(ts, str) or len(ts) < 19:
                continue
            try:
                ets = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").timestamp()
            except ValueError:
                continue
            if after_ts <= ets <= before_ts:
                return True
    except Exception as e:  # noqa: BLE001 — tolerant
        _eprint(f"evolution-log read failed ({e})")
    return False


def _window_bounds(records):
    """(after_ts, before_ts) over the last _TREND_WINDOW non-warmup records —
    the same span the trend slope is computed on. before = newest, after =
    oldest in that span. Returns (None, None) if < 2 usable records."""
    usable = [r for r in records
              if not r.get("warmup") and r.get("ts")][:_TREND_WINDOW]
    if len(usable) < 2:
        return None, None

    def _ts(r):
        try:
            return datetime.strptime(r["ts"][:19], "%Y-%m-%dT%H:%M:%S").timestamp()
        except (ValueError, TypeError, KeyError):
            return None
    before = _ts(usable[0])
    after = _ts(usable[-1])
    return after, before


def _load_attribution():
    """Load the hyphenated health-attribution module (only on a trip)."""
    spec = importlib.util.spec_from_file_location(
        "health_attribution", SCRIPT_DIR / "health-attribution.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _read_interval_marker(health_dir):
    try:
        p = Path(health_dir) / ".last-check"
        if p.exists():
            return int(p.read_text(encoding="utf-8").strip() or "-1")
    except (OSError, ValueError):
        pass
    return -1


def _write_interval_marker(health_dir, iteration):
    try:
        Path(health_dir).mkdir(parents=True, exist_ok=True)
        (Path(health_dir) / ".last-check").write_text(str(iteration), encoding="utf-8")
    except OSError as e:
        _eprint(f"interval marker write failed ({e})")


def _calibration_status(health_dir, cal_cfg, now_ts):
    """Return (calibrated, progress, just_completed).

    Evaluates the 30-day/50-record calibration AND-gate (spec §10) over the FULL
    ledger history, in EVERY mode (including collect-only) — so the agent learns
    the instant revert authority becomes mathematically eligible and can file a
    one-time goal to advance `health_regression.mode`, rather than silently
    waiting 30 days.

    Monotonic: on an append-only ledger the gate never reverses once satisfied. A
    one-time `.calibrated` marker records the crossing so `just_completed` fires
    EXACTLY once and the full-history scan is skipped (O(1)) thereafter.

    - progress is (days, records) when freshly scanned, else None (marker
      short-circuit — exact counts no longer needed past the gate).
    """
    marker = Path(health_dir) / ".calibrated"
    if marker.exists():
        return True, None, False
    days, records = hl.full_calibration_progress(health_dir)
    calibrated = days >= cal_cfg["min_days"] and records >= cal_cfg["min_records"]
    if not calibrated:
        return False, (days, records), False
    # First crossing — persist the marker so the edge fires once.
    try:
        Path(health_dir).mkdir(parents=True, exist_ok=True)
        marker.write_text(
            datetime.fromtimestamp(now_ts).strftime("%Y-%m-%dT%H:%M:%S")
            + f" days={days} records={records}\n", encoding="utf-8")
    except OSError as e:
        _eprint(f"calibration marker write failed ({e})")
        # Fire anyway: the goal-filing path dedups on _CALIBRATION_DEDUP_KEY, so a
        # rare double-file (marker never persisted) is harmless. Better to fire
        # twice than to never surface the calibration-complete signal.
    return True, (days, records), True


def _verdict(tripped, reason, **extra):
    v = {"tripped": tripped, "reason": reason}
    v.update(extra)
    return v


def run(*, config_dir, meta_dir, agent_dir, repo_root, now_ts, force=False,
        attribute_fn=None):
    """Evaluate the detection gate and return a verdict dict. Side-effect-light:
    the only write is the interval marker. Pure-ish given injected paths +
    now_ts; `attribute_fn` (defaults to the real attribution engine) is
    injectable so the trip path is hermetically testable."""
    cfg = _load_config(config_dir)
    mode = cfg["mode"]
    health_dir = Path(agent_dir) / "health"

    # Calibration is evaluated in EVERY mode (incl. collect-only) and BEFORE the
    # mode early-returns, so the calibration-complete edge surfaces the instant
    # the gate trips — regardless of which rollout phase the agent is in.
    calibrated, cal_progress, cal_just_done = _calibration_status(
        health_dir, cfg["calibration"], now_ts)
    cal_block = ({"days": cal_progress[0], "records": cal_progress[1]}
                 if cal_progress is not None else {"complete": True})
    cal_ctx = {
        "calibrated": calibrated,
        "calibration": cal_block,
        "calibration_just_completed": cal_just_done,
        "calibration_dedup_key": _CALIBRATION_DEDUP_KEY,
    }

    if mode == "collect-only":
        return _verdict(False, "mode=collect-only — detection disabled",
                        mode=mode, **cal_ctx)

    det = cfg["detection"]
    records = hl.recent_records(health_dir, 120)
    if not records:
        return _verdict(False, "no ledger records yet", mode=mode, **cal_ctx)

    latest = hl.latest_nonwarmup(records)
    cur_iter = (latest or {}).get("iteration")

    # Interval gating (idempotent within a window). force=True bypasses.
    if not force and isinstance(cur_iter, int):
        last = _read_interval_marker(health_dir)
        if last >= 0 and (cur_iter - last) < det["interval"]:
            return _verdict(False, f"interval not elapsed ({cur_iter - last}/{det['interval']})",
                            mode=mode, **cal_ctx)
    if isinstance(cur_iter, int):
        _write_interval_marker(health_dir, cur_iter)

    tripped, reasons = evaluate_gate(latest, det)
    consecutive = hl.consecutive_below_baseline(records)
    counter_ok = consecutive >= det["consecutive_below_baseline"]

    if not (tripped and counter_ok):
        why = "; ".join(reasons) if not tripped else \
            f"consecutive below_baseline {consecutive}/{det['consecutive_below_baseline']}"
        return _verdict(False, f"gate not tripped: {why}",
                        mode=mode, consecutive=consecutive, **cal_ctx)

    # --- TRIPPED: identify degraded signal, attribute, check evolution-log ----
    cap = float(cfg["tree_writes_cap"]) or 8.0
    signal = degraded_signal(records, cap)
    after_ts, before_ts = _window_bounds(records)
    evo_change = (_evolution_change_in_window(meta_dir, after_ts, before_ts)
                  if (after_ts and before_ts) else False)

    candidates = []
    if after_ts and before_ts:
        try:
            fn = attribute_fn or _load_attribution().attribute
            candidates = fn(
                after_ts, before_ts, signal,
                config_dir=config_dir, repo_root=repo_root, agent_dir=agent_dir,
                now_ts=now_ts, confidence_gate=cfg["revert"]["confidence_gate"])
        except Exception as e:  # noqa: BLE001 — attribution is best-effort
            _eprint(f"attribution failed ({e})")

    window_iso = ([datetime.fromtimestamp(after_ts).strftime("%Y-%m-%dT%H:%M:%S"),
                   datetime.fromtimestamp(before_ts).strftime("%Y-%m-%dT%H:%M:%S")]
                  if (after_ts and before_ts) else None)
    dedup_key = f"health-regression:{signal}:{(latest or {}).get('ts', '')[:10]}"

    return _verdict(
        True,
        f"health regression on '{signal}': composite {latest.get('composite')} "
        f"< floor {det['floor']}, trend {latest.get('composite_trend')}, "
        f"{consecutive} consecutive below baseline {latest.get('baseline')}",
        mode=mode,
        signal=signal,
        iteration=cur_iter,          # health-revert.append_pending reads this for
                                     # revert_iteration; without it verify_pending's
                                     # window math collapses (int(None) -> immediate verify).
        window=window_iso,
        composite=latest.get("composite"),
        baseline=latest.get("baseline"),
        composite_trend=latest.get("composite_trend"),
        consecutive=consecutive,
        evolution_change_in_window=evo_change,
        revert_eligible=(mode == "full" and calibrated),
        candidates=candidates,
        dedup_key=dedup_key,
        **cal_ctx,   # calibrated, calibration, calibration_just_completed, dedup key
    )


def main():
    ap = argparse.ArgumentParser(description="Health-regression detection sweep (Phase 2: detect-and-report).")
    ap.add_argument("--json", action="store_true", help="emit JSON verdict")
    ap.add_argument("--force", action="store_true", help="ignore the interval marker (run now)")
    args = ap.parse_args()

    def out(v):
        print(json.dumps(v, ensure_ascii=True) if args.json else v.get("reason", ""))
        return 0

    agent = os.environ.get("MIND_AGENT", "").strip()
    if not agent:
        return out(_verdict(False, "no agent bound"))

    try:
        import _paths
        config_dir = _paths.CONFIG_DIR
        meta_dir = _paths.META_DIR
        agent_dir = _paths.agent_dir(agent)
        repo_root = _paths.PROJECT_ROOT
    except Exception as e:  # noqa: BLE001 — fail-open
        return out(_verdict(False, f"path resolution failed ({e})"))

    return out(run(config_dir=config_dir, meta_dir=meta_dir, agent_dir=agent_dir,
                   repo_root=repo_root, now_ts=datetime.now().timestamp(),
                   force=args.force))


if __name__ == "__main__":
    sys.exit(main())
