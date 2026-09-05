#!/usr/bin/env python3
"""health-ledger-append.py — append one per-iteration self-health record.

Spec: core/config/conventions/health-ledger.md. Design: workflow
wf_b82a26e9-958 (2026-06-03).

Reads the agent's LATEST record from the productivity-snapshot store
(world/productivity-snapshots.jsonl plus its date segments, composed by
_productivity_snapshots.snapshot_paths — g-358-49) — the
four health signals (composite_productivity, encoding_ratio, deep_ratio,
tree_writes) are already computed once per iteration by productivity-stop-gate.sh
and are reused here, NOT recomputed. Adds the cross-session intelligence the
productivity gate lacks: a weighted health composite, a least-squares trend over
the last 10 non-warmup composites, a baseline comparison (audit-baselines
ratchet), and a session warmup flag. Appends to the daily-rotated per-agent
ledger agents/<agent>/health/<YYYY-MM-DD>.jsonl.

FIRE-AND-FORGET / FAIL-OPEN: any error exits 0 without writing. A telemetry
miss must NEVER break iteration-close. Invoked by the health-snapshot phase of
iteration-close.sh (after productivity-check). Phase 1 (collect-only): this
script only appends; detection and revert live in later phases.
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# stdout/stderr unicode safety on Windows (mirrors history.py)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _fileops  # noqa: E402 — after the sys.path insert above (-e)

# --- Inline defaults (used only if config is missing/unreadable; fail-open) ---
_DEFAULTS = {
    "weights": {"composite_productivity": 0.40, "encoding_ratio": 0.25,
                "deep_ratio": 0.20, "tree_writes": 0.15},
    "tree_writes_cap": 8,
    "min_signals": 2,
    "warmup_records": 2,
    "baseline_tolerance": 0.10,
}
_TREND_WINDOW = 10  # last N non-warmup composites for the LSQ slope


def _eprint(msg):
    print(f"[health-ledger] {msg}", file=sys.stderr)


def _load_config(config_dir):
    """Read health_regression from aspirations.yaml; fail-open to _DEFAULTS."""
    cfg = dict(_DEFAULTS)
    try:
        import yaml
        with open(Path(config_dir) / "aspirations.yaml", encoding="utf-8") as f:
            full = yaml.safe_load(f) or {}
        hr = full.get("health_regression") or {}
        if hr.get("weights"):
            cfg["weights"] = {k: float(v) for k, v in hr["weights"].items()}
        for k in ("tree_writes_cap", "min_signals", "warmup_records"):
            if hr.get(k) is not None:
                cfg[k] = hr[k]
        if hr.get("baseline_tolerance") is not None:
            cfg["baseline_tolerance"] = float(hr["baseline_tolerance"])
    except Exception as e:  # noqa: BLE001 — fail-open to defaults
        _eprint(f"config read failed ({e}) — using defaults")
    return cfg


def _latest_productivity_snapshot(world_dir, agent):
    """The agent's most recent productivity snapshot, or None if absent.

    Reads through the store's composition seam (_productivity_snapshots.
    snapshot_paths — g-358-49) rather than the hardcoded legacy filename. Once
    PRODUCTIVITY_SNAPSHOTS_SEGMENTED is set the writer emits date segments and
    the legacy file stops growing; a legacy-only read here would silently pin
    this agent's health composite to its last pre-cutover record forever, which
    the ledger would then publish as a real (stale) score. Fail-soft to the
    legacy path if the seam cannot be imported.

    Scans newest-first: segments in reverse chronological order, and within each
    file from the end, returning the first record for `agent`."""
    try:
        from _productivity_snapshots import snapshot_paths  # type: ignore
        paths = snapshot_paths(world_dir)
    except Exception as e:  # noqa: BLE001 — fail-soft to the legacy filename
        _eprint(f"snapshot seam unavailable ({e}) — falling back to legacy path")
        paths = [Path(world_dir) / "productivity-snapshots.jsonl"]
    for snap_path in reversed(paths):
        try:
            if not snap_path.exists():
                continue
            lines = snap_path.read_text(encoding="utf-8").splitlines()
        except OSError as e:
            _eprint(f"productivity-snapshots read failed ({e})")
            continue
        for ln in reversed(lines):
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if rec.get("agent") == agent:
                return rec
    return None


def _read_baseline(meta_dir):
    """Read the health_composite baseline from meta/audit-baselines.yaml.

    Tolerant: returns a float baseline or None. The ratchet schema is seeded in
    Phase 2 (T22/T23); until then this returns None and below_baseline is False
    — correct, since the calibration period has not elapsed. Accepts a scalar
    value or a mapping carrying a numeric 'baseline'/'value'/'current' field."""
    try:
        import yaml
        path = Path(meta_dir) / "audit-baselines.yaml"
        if not path.exists():
            return None
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        node = data.get("health_composite")
        if node is None and isinstance(data.get("baselines"), dict):
            node = data["baselines"].get("health_composite")
        if node is None:
            return None
        if isinstance(node, (int, float)):
            return float(node)
        if isinstance(node, dict):
            for k in ("baseline", "value", "current", "current_value"):
                if isinstance(node.get(k), (int, float)):
                    return float(node[k])
    except Exception as e:  # noqa: BLE001 — tolerant
        _eprint(f"baseline read failed ({e})")
    return None


def _recent_records(health_dir, limit):
    """Up to `limit` most recent ledger records (newest-first) by walking the
    daily-rotated day-files newest-date-first, reversing within each file
    (records are append-ordered oldest-first)."""
    out = []
    try:
        if not health_dir.exists():
            return out
        day_files = sorted(
            (p for p in health_dir.iterdir()
             if p.is_file() and p.suffix == ".jsonl"),
            key=lambda p: p.name, reverse=True)
    except OSError:
        return out
    for f in day_files:
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for ln in reversed(lines):
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
            if len(out) >= limit:
                return out
    return out


def _lsq_slope(ys):
    """Least-squares slope over y-values at x = 0..n-1 (oldest->newest)."""
    n = len(ys)
    if n < 2:
        return 0.0
    sx = sum(range(n))
    sy = sum(ys)
    sxy = sum(i * y for i, y in enumerate(ys))
    sxx = sum(i * i for i in range(n))
    denom = n * sxx - sx * sx
    if denom == 0:
        return 0.0
    return (n * sxy - sx * sy) / denom


def normalize_signals(raw, cap):
    """Map raw signal values to [0,1]. tree_writes is rate-normalized by `cap`;
    the others are already 0-1 rates (clamped defensively). None/non-numeric
    values are dropped so the composite renormalizes over what is available."""
    norm = {}
    for k, v in raw.items():
        if v is None:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if k == "tree_writes":
            norm[k] = min(fv / cap, 1.0) if cap else 1.0
        else:
            norm[k] = max(0.0, min(fv, 1.0))
    return norm


def compute_composite(norm, weights, min_signals):
    """Weighted composite over available signals, renormalized by the sum of the
    available weights. Returns None when fewer than `min_signals` are present
    (so the record is excluded from trend/detection)."""
    avail = [k for k in norm if k in weights]
    if len(avail) < int(min_signals):
        return None
    wsum = sum(weights[k] for k in avail)
    if not wsum:
        return None
    return round(sum(weights[k] * norm[k] for k in avail) / wsum, 4)


def _session_id(agent_dir):
    """The runner's SID (for warmup counting), or MIND_SID, or 'unknown'."""
    try:
        p = Path(agent_dir) / "session" / "running-session-id"
        if p.exists():
            sid = p.read_text(encoding="utf-8").strip()
            if sid:
                return sid
    except OSError:
        pass
    return os.environ.get("MIND_SID", "") or "unknown"


def main():
    agent = os.environ.get("MIND_AGENT", "").strip()
    if not agent:
        return 0  # no agent bound — nothing to do

    try:
        import _paths
        world_dir = _paths.WORLD_DIR
        meta_dir = _paths.META_DIR
        config_dir = _paths.CONFIG_DIR
        agent_dir = _paths.agent_dir(agent)
    except Exception as e:  # noqa: BLE001 — fail-open
        _eprint(f"path resolution failed ({e}) — no-op")
        return 0
    if not world_dir:
        return 0

    #  (G3 worker rail): a WORKER Body must not append to the
    # agent-wide health ledger. Two boxes appending per-iteration records to one
    # agents/<agent>/health/<date>.jsonl interleaves two different machines'
    # composites into one series, which then feeds health-regression-check and
    # (at mode=full) revert authority — so the corruption is not cosmetic.
    #
    # Use MIND_SID directly, NOT _session_id(). That helper deliberately prefers
    # session/running-session-id — the REDUCER's SID — because it is counting
    # warmup for the runner. On a worker box that is a DIFFERENT Body (or absent),
    # so feeding it to the body-WM predicate would test the reducer's session dir
    # and never fire. The predicate has to be about THIS process's own session.
    #
    # Deriving locally rather than reading BODY_ROLE is per guard-2445. Here the
    # env var would in fact arrive (this runs from iteration-close.sh, inside a
    # Bash-tool chain that bash-agent-inject rewrote), but the local predicate is
    # the same one bash-agent-inject itself uses, so the two cannot drift — and
    # this file is importable from contexts that are not that chain.
    _own_sid = os.environ.get("MIND_SID", "").strip()
    if _own_sid and (Path(agent_dir) / "sessions" / _own_sid
                     / "working-memory.yaml").exists():
        return 0

    cfg = _load_config(config_dir)
    snap = _latest_productivity_snapshot(world_dir, agent)
    if snap is None:
        # No productivity snapshot yet (session < min_iterations goals, or the
        # gate hasn't run). Silent no-op — the ramp is noise.
        return 0

    breakdown = snap.get("breakdown") or {}
    counts = breakdown.get("counts") or {}
    cap = float(cfg["tree_writes_cap"]) or 8.0

    # Reuse the already-computed signals; normalize to [0,1].
    raw = {
        "composite_productivity": snap.get("score"),
        "encoding_ratio": breakdown.get("encoding_ratio"),
        "deep_ratio": breakdown.get("deep_ratio"),
        "tree_writes": counts.get("tree_writes"),
    }
    weights = cfg["weights"]
    norm = normalize_signals(raw, cap)
    composite = compute_composite(norm, weights, cfg["min_signals"])

    session_id = _session_id(agent_dir)
    health_dir = Path(agent_dir) / "health"

    # Warmup: first N non-null records of THIS session are excluded from trend.
    recent = _recent_records(health_dir, 60)
    session_nonnull = sum(
        1 for r in recent
        if r.get("session_id") == session_id and r.get("composite") is not None)
    warmup = (composite is None) or (session_nonnull < int(cfg["warmup_records"]))

    # Trend: LSQ slope over the last (window-1) prior non-warmup composites plus
    # the current one (if non-warmup), oldest->newest.
    prior = [r["composite"] for r in recent
             if (not r.get("warmup")) and isinstance(r.get("composite"), (int, float))]
    series = list(reversed(prior[:_TREND_WINDOW - 1]))  # oldest->newest
    if (not warmup) and composite is not None:
        series.append(composite)
    composite_trend = round(_lsq_slope(series), 4) if len(series) >= 2 else 0.0

    baseline = _read_baseline(meta_dir)
    tol = float(cfg["baseline_tolerance"])
    if baseline is not None and composite is not None:
        below_baseline = composite < baseline * (1.0 - tol)
    else:
        below_baseline = False

    record = {
        "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "agent": agent,
        "session_id": session_id,
        "iteration": snap.get("goals_completed"),
        "signals": {k: raw[k] for k in raw if raw[k] is not None},
        "composite": composite,
        "composite_trend": composite_trend,
        "baseline": baseline,
        "below_baseline": below_baseline,
        "warmup": warmup,
    }

    # Daily-rotated append (T03b). The filename IS the rotation. mkdir via the
    # runtime (bypasses the L1 Write/Edit hook, which gates only those tools).
    #
    # -e: this was a bare open(...,"a"). That is byte-identical to what
    # LocalBackend does, but it takes NO lock and never reaches the backend, so
    # under own-cloud the record landed only in the local mirror and a peer's
    # full-file PUT could overwrite it wholesale. locked_append_jsonl takes the
    # lock, routes through the active backend (force-fresh read + append + PUT
    # with 412 retry), and is now reconciled below the write by the handler
    # registered in coordination_merge for health/*.jsonl. Snapshot-blacklisted
    # in _fileops so the per-iteration append does not snapshot the day-file each
    # time (O(N^2) — same treatment as meta/gate-firings.jsonl).
    try:
        health_dir.mkdir(parents=True, exist_ok=True)
        day_file = health_dir / (datetime.now().strftime("%Y-%m-%d") + ".jsonl")
        _fileops.locked_append_jsonl(day_file, record)
    except Exception as e:
        # Widened from OSError: the locked path can also raise on lock
        # acquisition or a backend error. Telemetry must never break the caller
        # (this runs inside iteration-close), so the failure stays a no-op —
        # same contract as before, one rung wider.
        _eprint(f"append failed ({e}) — no-op")
        return 0

    print(f"[health-ledger] {agent} composite={composite} trend={composite_trend} "
          f"baseline={baseline} below={below_baseline} warmup={warmup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
