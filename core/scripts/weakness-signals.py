#!/usr/bin/env python3
"""weakness-signals.py — windowed weakness-signal computation ().

Computes the /reflect Step 5.55 guardrail + pattern-signature weakness signals
over a WINDOW (delta since the last weakness analysis) instead of lifetime
counters, which stop discriminating on a mature store (127/722 guardrails
matched the lifetime `times_active >= 3` threshold; guard-054 sat at 3520
because the precheck guardrail-check mass-matches ~58 rules every iteration).

Baseline lives in the agent's existing `weakness-report.yaml` under a
`signal_baseline:` section (no new agent-dir top-level file — L1-clean; same
advisory-ratchet shape as meta/audit-baselines.yaml):

    signal_baseline:
      captured_at: "2026-07-10T23:00:00"
      guardrail_times_active: {guard-001: 3520, ...}
      signature_outcomes: {sig-20: {total: 3, confirmed: 3}, ...}

First run (no baseline) SEEDS the baseline and emits no windowed signals —
verdict mirrors audit-baselines.md "seeded". Subsequent runs compute deltas.

Discrimination rule (guardrails): ambient mass-matched guards all gain roughly
the same delta per window, so a guard is a SIGNAL only when its delta is BOTH
absolutely material (>= --min-delta, default 3) AND distinguishable from the
ambient level (>= ambient-mult x median of nonzero deltas, default 2.0), AND
ranks in the top --top-k (default 5) by delta.

NOTE (g-115-2141 retirement, re-applied by g-115-2470): the guardrail_signals
output is computed and baselined here but NOT consumed by /reflect Step 5.55 —
times_active increments on keyword scans, so even a windowed delta carries no
genuine-fire information. Computation stays for baseline/schema continuity and
a clean future re-add (re-add condition: a real-fires field distinct from
keyword-scan matches). See the retirement block in reflect/SKILL.md.

Signature rule: window accuracy = delta_confirmed/delta_total when
delta_total >= --min-sig-outcomes (default 3); signal when < --max-accuracy
(default 0.70). Entities created after the baseline use lifetime values (their
lifetime IS the window).

Fail-open: missing stores or a malformed report yield empty signals plus a
`notes` entry — never a crash, never blocks /reflect. Direct store reads are
sanctioned here (scripts layer — same precedent as retrieval_utility_report.py).
"""

import argparse
import json
import os
import statistics
import sys
import tempfile
from datetime import datetime
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


def _read_jsonl(path: Path):
    entries = []
    if not path.is_file():
        return entries
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _load_report(path: Path):
    """Absent file -> {}. Present-but-unparseable -> None (caller MUST NOT
    rewrite the file: a corrupt-but-recoverable report would be clobbered
    with only signal_baseline — fresh-eyes F-1, g-115-1905)."""
    if not path.is_file() or yaml is None:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write_report(path: Path, report: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(report, f, default_flow_style=False, sort_keys=False,
                           allow_unicode=True)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


AMBIENT_MIN_COHORT = 6  # below this many movers there is no "ambient" to filter


def _times_active(rec, counters=None):
    """`times_active` for one record — sidecar-aware ().

    `counters` is a PARAMETER rather than a load because this module imports
    stdlib alone at module scope (its only `_paths` use is a lazy sys.path
    insert further down); the seam resolves WORLD_DIR at import. Default None
    => embedded read, byte-identical to pre-seam behaviour.

    Silent-blanking is the failure mode to keep in view. Every signal here is a
    DELTA against a baseline, so once the writer lands and `times_active` moves
    to the sidecar, a frozen embedded read does not report a wrong number — it
    reports `now == base` for every guardrail, every delta collapses to 0, and
    the whole weakness-signal lane goes quiet while looking perfectly healthy.
    An empty signal set is indistinguishable from "nothing is firing".
    """
    if counters:
        from _utilization_store import utilization_of as _uo
        return (_uo(rec, counters).get("times_active")) or 0
    return ((rec.get("utilization") or {}).get("times_active")) or 0


def compute_guardrail_signals(guards, baseline_map, min_delta, ambient_mult, top_k,
                              counters=None):
    """Windowed guardrail deltas -> discriminating subset.

    The ambient-mult filter exists to exclude the mass-matched cohort (the
    precheck guardrail-check moves ~58 guards by the same amount every
    iteration). With fewer than AMBIENT_MIN_COHORT movers there is no cohort
    to be ambient against, so any delta >= min_delta is discriminating on its
    own (fixes the sole-mover edge: one new guard firing 40x was suppressed
    by 2x the median of itself).
    """
    deltas = []
    for g in guards:
        if g.get("status") not in (None, "active"):
            continue
        gid = g.get("id")
        if not gid:
            continue
        now = _times_active(g, counters)
        base = baseline_map.get(gid, 0)  # new-since-baseline: lifetime IS the window
        delta = now - base
        if delta > 0:
            deltas.append({"id": gid, "delta": delta, "times_active": now,
                           "rule_head": (g.get("rule") or "")[:80]})
    if not deltas:
        return [], 0
    nonzero = [d["delta"] for d in deltas]
    median_delta = statistics.median(nonzero)
    if len(nonzero) < AMBIENT_MIN_COHORT:
        threshold = float(min_delta)
    else:
        threshold = max(min_delta, ambient_mult * median_delta)
    deltas.sort(key=lambda d: d["delta"], reverse=True)
    signals = [dict(d, window_threshold=round(threshold, 2),
                    median_delta=median_delta)
               for d in deltas[:top_k] if d["delta"] >= threshold]
    return signals, median_delta


def compute_signature_signals(sigs, baseline_map, min_outcomes, max_accuracy):
    """Windowed signature accuracy -> low-accuracy subset."""
    signals = []
    for s in sigs:
        if s.get("status") not in (None, "active"):
            continue
        sid = s.get("id")
        if not sid:
            continue
        stats = s.get("outcome_stats") or {}
        total = stats.get("total") or 0
        confirmed = stats.get("confirmed") or 0
        base = baseline_map.get(sid) or {}
        d_total = total - (base.get("total") or 0)
        d_confirmed = confirmed - (base.get("confirmed") or 0)
        if d_total >= min_outcomes:
            acc = d_confirmed / d_total
            if acc < max_accuracy:
                signals.append({"id": sid, "name": (s.get("name") or "")[:80],
                                "window_total": d_total,
                                "window_accuracy": round(acc, 4)})
    return signals


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--agent", default=os.environ.get("MIND_AGENT", ""))
    ap.add_argument("--report-path", default=None,
                    help="override weakness-report.yaml path (tests)")
    ap.add_argument("--world-dir", default=None,
                    help="override world dir holding the stores (tests)")
    ap.add_argument("--min-delta", type=int, default=3)
    ap.add_argument("--ambient-mult", type=float, default=2.0)
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--min-sig-outcomes", type=int, default=3)
    ap.add_argument("--max-accuracy", type=float, default=0.70)
    ap.add_argument("--no-baseline-update", action="store_true",
                    help="compute only; do not advance signal_baseline")
    args = ap.parse_args()

    notes = []
    out = {"seeded": False, "guardrail_signals": [], "signature_signals": [],
           "baseline_updated": False, "notes": notes}

    if yaml is None:
        notes.append("PyYAML unavailable — fail-open, no signals")
        print(json.dumps(out))
        return 0

    # Resolve paths. _paths.py is the SSOT for world/agent dirs; overrides are
    # test seams (same posture as retrieval_utility_report.py --store).
    if args.world_dir:
        world = Path(args.world_dir)
    else:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from _paths import WORLD_DIR  # type: ignore
            world = Path(WORLD_DIR)
        except Exception as ex:
            notes.append(f"world dir unresolved ({ex}) — fail-open")
            print(json.dumps(out))
            return 0
    if args.report_path:
        report_path = Path(args.report_path)
    else:
        try:
            from _paths import agent_dir  # type: ignore
            if not args.agent:
                notes.append("no agent binding — fail-open")
                print(json.dumps(out))
                return 0
            report_path = Path(agent_dir(args.agent)) / "weakness-report.yaml"
        except Exception as ex:
            notes.append(f"agent dir unresolved ({ex}) — fail-open")
            print(json.dumps(out))
            return 0

    guards = _read_jsonl(world / "guardrails.jsonl")
    sigs = _read_jsonl(world / "pattern-signatures.jsonl")
    if not guards:
        notes.append("guardrails.jsonl empty/missing")
    if not sigs:
        notes.append("pattern-signatures.jsonl empty/missing")

    report = _load_report(report_path)
    report_unparseable = report is None
    if report_unparseable:
        notes.append("weakness-report.yaml present but unparseable — signals "
                     "computed without baseline; baseline write SKIPPED to "
                     "avoid clobbering recoverable content")
        report = {}
    baseline = report.get("signal_baseline") or {}
    have_baseline = bool(baseline.get("captured_at"))

    # Load the guardrail counter sidecar ONCE (). Fail-open to {} =>
    # embedded reads, i.e. exactly today's behaviour.
    try:
        from _utilization_store import load_counters as _load_counters
        _counters = _load_counters("guardrails")
    except Exception:
        _counters = {}

    # THIS MAP IS WRITTEN BACK AS THE NEXT BASELINE (see
    # "guardrail_times_active" below), while compute_guardrail_signals measures
    # the delta against the PREVIOUS one. So this read and that one must draw
    # from the SAME source: converting only one of the pair would compare a
    # sidecar `now` against an embedded `base`, and every delta would be the
    # gap between two different fields rather than movement over time.
    current_guard_map = {
        g["id"]: _times_active(g, _counters)
        for g in guards if g.get("id") and g.get("status") in (None, "active")
    }
    current_sig_map = {
        s["id"]: {"total": (s.get("outcome_stats") or {}).get("total") or 0,
                  "confirmed": (s.get("outcome_stats") or {}).get("confirmed") or 0}
        for s in sigs if s.get("id") and s.get("status") in (None, "active")
    }

    if have_baseline:
        g_sig, median_delta = compute_guardrail_signals(
            guards, baseline.get("guardrail_times_active") or {},
            args.min_delta, args.ambient_mult, args.top_k, _counters)
        out["guardrail_signals"] = g_sig
        out["guardrail_median_delta"] = median_delta
        out["signature_signals"] = compute_signature_signals(
            sigs, baseline.get("signature_outcomes") or {},
            args.min_sig_outcomes, args.max_accuracy)
        out["window_start"] = baseline.get("captured_at")
    else:
        if not report_unparseable:
            out["seeded"] = True
            notes.append("no prior baseline — seeded; windowed signals available "
                         "from the next analysis")

    if not args.no_baseline_update and not report_unparseable:
        report["signal_baseline"] = {
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "guardrail_times_active": current_guard_map,
            "signature_outcomes": current_sig_map,
        }
        try:
            _write_report(report_path, report)
            out["baseline_updated"] = True
        except Exception as ex:
            notes.append(f"baseline write failed ({ex}) — signals still valid")

    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
