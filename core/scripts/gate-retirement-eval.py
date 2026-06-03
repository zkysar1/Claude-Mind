#!/usr/bin/env python3
"""Gate retirement evaluator — Phase 5 of the gate audit/retirement plan.

Reads `meta/gate-firings.jsonl` + `core/config/gates.yaml` and produces
per-gate recommendations: retire | tighten | widen | investigate | keep
| insufficient_data | uninstrumented.

Recommender, not judge — every recommendation includes the raw counts and
ratios that produced it so a reviewer (or downstream evolution loop) can
sanity-check the math. Never deletes anything; output is JSON-only.

Usage:
  py -3 core/scripts/gate-retirement-eval.py [--days N] [--min-fires K]
                                             [--gate <id>] [--output json|human]

  --days N       Look-back window in days (default 30).
  --min-fires K  Volume threshold applied to both named guards
                 (MIN_TOTAL_FIRINGS, MIN_RATE_SAMPLES) (default 10).
  --gate <id>    Restrict output to a single gate id.
  --output       json (default) or human-readable summary table.

Recommendation rules (see core/config/conventions/gate-overrides.md and
gates.yaml for context). Two named volume guards gate distinct sample sets:
MIN_TOTAL_FIRINGS gates `total` (retire / widen / insufficient_data),
MIN_RATE_SAMPLES gates `block + override` (tighten). Both default to
`--min-fires`.

  retire         decision != noop count == 0 over window
                 AND total >= MIN_TOTAL_FIRINGS
                 AND retirement_eligible: true
                 → Gate has never fired meaningfully. Delete it.

  tighten        override / (block + override) > tighten_threshold (0.5)
                 AND (block + override) >= MIN_RATE_SAMPLES
                 → FP-dominant: caller bypasses more than half the time.
                   Trigger pattern is too generous.

  widen          noop / total >= widen_threshold (0.95)
                 AND total >= MIN_TOTAL_FIRINGS
                 AND retirement_eligible: true
                 AND keyword_bias == "generous" (i.e., FN-dominant intent)
                 → Gate almost never triggers; may be missing real cases.

  investigate    fail_open count > 0
                 → Gate threw an exception at least once. Look at the
                   recorded gate_error and fix.

  keep           None of the above; gate behaving within expected envelope.

  insufficient_data   total firings < MIN_TOTAL_FIRINGS
                      → Cannot make a confident recommendation yet.

  uninstrumented      gate.instrumented: false in gates.yaml
                      → Gate has no telemetry to evaluate.

Contract: never raises. A bad firing record is skipped with a stderr WARN.
A missing gates.yaml is fatal (exit 2).
"""

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(2)

from _paths import META_DIR, CONFIG_DIR
# Decision enum — single source of truth in _gate_log.py. Importing (not
# redeclaring) means any future change to the taxonomy lands in one place;
# mismatches between writer and reader become impossible by construction.
from _gate_log import _VALID_DECISIONS

GATES_YAML = CONFIG_DIR / "gates.yaml"
FIRINGS_JSONL = META_DIR / "gate-firings.jsonl"

TIGHTEN_THRESHOLD = 0.5  # override / (block + override) above this → tighten
WIDEN_THRESHOLD = 0.95   # noop / total at or above this → widen (FN-only)


def _load_gates():
    """Load gates.yaml. Return list of gate dicts. Fatal if file is missing.

    Reads ONLY `gates:` — the audited+instrumented list. The sibling
    `pending_audit:` list (gates discovered but not yet asymmetry-analyzed)
    is INTENTIONALLY excluded: scoring gates without instrumented telemetry
    would manufacture noise. Promote pending_audit entries by completing
    their audit row in gates.yaml; until then they are invisible here.
    """
    if not GATES_YAML.is_file():
        print(f"[gate-retirement-eval] FATAL: {GATES_YAML} not found.",
              file=sys.stderr)
        sys.exit(2)
    try:
        data = yaml.safe_load(GATES_YAML.read_text(encoding="utf-8")) or {}
    except Exception as e:
        print(f"[gate-retirement-eval] FATAL: {GATES_YAML} parse error: {e}",
              file=sys.stderr)
        sys.exit(2)
    return data.get("gates", []) or []


def _load_firings(since):
    """Yield firing records with ts >= `since`. Skip malformed lines (WARN)."""
    if not FIRINGS_JSONL.is_file():
        return
    skipped = 0
    for line in FIRINGS_JSONL.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            skipped += 1
            continue
        ts_raw = rec.get("ts")
        if not isinstance(ts_raw, str):
            skipped += 1
            continue
        try:
            ts = datetime.strptime(ts_raw, "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            skipped += 1
            continue
        # decision is guaranteed valid by _gate_log.log() (invalid/null coerces
        # to "fail_open" at write time). A record reaching here without a valid
        # decision indicates schema drift or a non-canonical writer — skip with
        # a WARN so the count surfaces rather than silently corrupting stats.
        if rec.get("decision") not in _VALID_DECISIONS:
            skipped += 1
            continue
        if ts >= since:
            yield rec
    if skipped:
        print(f"[gate-retirement-eval] WARN: skipped {skipped} malformed "
              f"firing record(s)", file=sys.stderr)


def _score_gate(gate, counts, min_fires):
    """Apply recommendation rules to one gate's count summary.

    Returns dict with `recommendation`, `reason`, and the raw evidence.

    `min_fires` is the single CLI knob, applied inside the function as two
    named thresholds — one per distinct sample set.
    """
    # Two volume guards — named for the sample set each gates, NOT the
    # threshold value. Do not collapse them: MIN_RATE_SAMPLES gates
    # `block + override` (the rate-stability sample), which is a proper
    # subset of the `total` that MIN_TOTAL_FIRINGS gates. A future
    # `--min-rate-samples` override can diverge the rate guard without
    # touching the volume guard — that is the point of the split.
    MIN_TOTAL_FIRINGS = min_fires   # gates `total`: retire + widen + insufficient_data
    MIN_RATE_SAMPLES = min_fires    # gates `block + override`: tighten only

    gid = gate["id"]
    instrumented = gate.get("instrumented", False)
    retirement_eligible = gate.get("retirement_eligible", False)
    keyword_bias = gate.get("keyword_bias", "balanced")
    dominant_risk = gate.get("dominant_risk")

    total = sum(counts.values())
    noop = counts.get("noop", 0)
    pass_ = counts.get("pass", 0)
    block = counts.get("block", 0)
    override = counts.get("override", 0)
    fail_open = counts.get("fail_open", 0)
    meaningful = total - noop  # decision != noop

    evidence = {
        "total_firings": total,
        "noop": noop,
        "pass": pass_,
        "block": block,
        "override": override,
        "fail_open": fail_open,
        "meaningful_firings": meaningful,
        "keyword_bias": keyword_bias,
        "retirement_eligible": retirement_eligible,
        "asymmetry_magnitude": gate.get("asymmetry_magnitude"),
        "dominant_risk": dominant_risk,
    }

    if not instrumented:
        return {"recommendation": "uninstrumented",
                "reason": "Gate has instrumented: false in gates.yaml; "
                          "no telemetry to evaluate.",
                "evidence": evidence}

    if total == 0:
        return {"recommendation": "insufficient_data",
                "reason": f"Zero firings in window — gate may not be on any "
                          f"hot path, or telemetry was just enabled.",
                "evidence": evidence}

    # Fail-open is a hard signal — gate raised an exception, has a bug.
    if fail_open > 0:
        return {"recommendation": "investigate",
                "reason": f"Gate fail_open count = {fail_open}. The gate "
                          f"raised an exception at least once. Inspect the "
                          f"`gate_error` field on those firings and fix.",
                "evidence": evidence}

    # Total-volume insufficient_data check uses MIN_TOTAL_FIRINGS (the retire
    # rule's guard). This uses `total`, not `meaningful` — a gate with 100
    # noop firings is exactly the case widen wants to surface; gating widen
    # behind meaningful-firing count is self-defeating.
    if total < MIN_TOTAL_FIRINGS:
        return {"recommendation": "insufficient_data",
                "reason": f"Only {total} total firing(s) "
                          f"(MIN_TOTAL_FIRINGS={MIN_TOTAL_FIRINGS}). "
                          f"Need more data before scoring.",
                "evidence": evidence}

    # Retire path: meaningful firings == 0 AND enough total firings.
    # The MIN_TOTAL_FIRINGS guard above prevents a single smoke-test noop
    # on an under-exercised gate from triggering false retirement.
    if meaningful == 0 and retirement_eligible:
        # FN-dominant guard (rb 2026-05-28, canonical gate prose-verification-drift):
        # an all-noop gate with dominant_risk=FN is typically a WORKING preventive
        # guard whose rare, costly guarded condition simply didn't occur in-window
        # -- retiring it removes the guard exactly when it's quiet. Route to
        # investigate, not retire: decide whether it's correctly scoped for a rare
        # event (keep) or mis-wired (widen/fix). Without this, the retire rule
        # preempts the widen path (unreachable when meaningful==0) and deletes a
        # guard that never had a chance to fire.
        if dominant_risk == "FN":
            return {"recommendation": "investigate",
                    "reason": f"Gate fired {total} times over window, all noops, "
                              f"but dominant_risk=FN. An all-noop FN-dominant gate "
                              f"is typically a working preventive guard whose rare "
                              f"guarded condition didn't occur in-window, NOT a dead "
                              f"gate. Investigate whether it is correctly scoped for "
                              f"a rare event (keep) or mis-wired (widen/fix) before "
                              f"retiring.",
                    "evidence": evidence}
        return {"recommendation": "retire",
                "reason": f"Gate fired {total} times over window, all noops "
                          f"(no trigger ever matched). Has never produced a "
                          f"pass/block/override decision. Delete it.",
                "evidence": evidence}
    if meaningful == 0 and not retirement_eligible:
        return {"recommendation": "keep",
                "reason": f"Gate fired {total}× (all noops) but is marked "
                          f"retirement_eligible: false (structural). Keep.",
                "evidence": evidence}

    # Tighten path: FP signal — override rate is high. Gated by
    # MIN_RATE_SAMPLES on (block + override), since the rate needs enough
    # samples to be stable independent of total firings.
    bo_total = block + override
    if bo_total > 0:
        override_rate = override / bo_total
        evidence["override_rate"] = round(override_rate, 3)
        if override_rate > TIGHTEN_THRESHOLD and bo_total >= MIN_RATE_SAMPLES:
            return {"recommendation": "tighten",
                    "reason": (
                        f"Override rate {override_rate:.0%} of "
                        f"({block} block + {override} override) = "
                        f"{bo_total} firings (MIN_RATE_SAMPLES="
                        f"{MIN_RATE_SAMPLES}) exceeds tighten threshold "
                        f"{TIGHTEN_THRESHOLD:.0%}. Caller bypasses more often "
                        f"than the gate blocks — trigger pattern is too "
                        f"generous. Tighten or remove patterns that fire "
                        f"on legitimate work."),
                    "evidence": evidence}

    # Widen path: gate almost never triggers, AND it's FN-dominant. Volume
    # guard is MIN_TOTAL_FIRINGS, enforced by the early return above —
    # widen and retire share the same `total` sample set.
    noop_rate = noop / total
    evidence["noop_rate"] = round(noop_rate, 3)
    if (noop_rate >= WIDEN_THRESHOLD
            and retirement_eligible
            and keyword_bias == "generous"):
        return {"recommendation": "widen",
                "reason": (
                    f"Noop rate {noop_rate:.0%} ≥ widen threshold "
                    f"{WIDEN_THRESHOLD:.0%} over {total} firings "
                    f"(MIN_TOTAL_FIRINGS={MIN_TOTAL_FIRINGS}) and gate is "
                    f"FN-dominant (keyword_bias=generous). Gate is missing "
                    f"real cases — add trigger patterns. Compare to "
                    f"gates.yaml fn_description for hints on what's escaping."),
                "evidence": evidence}

    return {"recommendation": "keep",
            "reason": "Gate behaving within expected envelope — neither "
                      "over-firing nor under-firing.",
            "evidence": evidence}


def _human_table(rows):
    """Compact human-readable summary."""
    lines = []
    by_rec = Counter(r["recommendation"] for r in rows)
    lines.append("=== Gate Retirement Recommendations ===")
    lines.append(f"Total gates evaluated: {len(rows)}")
    lines.append("Distribution:")
    for rec in ("retire", "tighten", "widen", "investigate", "keep",
                "insufficient_data", "uninstrumented"):
        if by_rec.get(rec):
            lines.append(f"  {by_rec[rec]:3d}  {rec}")
    lines.append("")
    lines.append(f"{'GATE ID':<32s} {'REC':<18s} {'TOTAL':>6s} {'NOOP':>6s} "
                 f"{'BLK':>5s} {'OVR':>5s} {'FAIL':>5s}")
    lines.append("-" * 84)
    for r in rows:
        ev = r["evidence"]
        lines.append(
            f"{r['gate_id']:<32s} {r['recommendation']:<18s} "
            f"{ev['total_firings']:>6d} {ev['noop']:>6d} "
            f"{ev['block']:>5d} {ev['override']:>5d} {ev['fail_open']:>5d}")
    lines.append("")
    # Surface the action items first.
    lines.append("Action items (anything not 'keep' / 'insufficient_data' / 'uninstrumented'):")
    actions = [r for r in rows if r["recommendation"] in
               ("retire", "tighten", "widen", "investigate")]
    if not actions:
        lines.append("  (none — all gates within expected envelope)")
    for r in actions:
        lines.append(f"  • {r['gate_id']} → {r['recommendation']}")
        lines.append(f"      {r['reason']}")
    return "\n".join(lines)


def _self_test():
    """Synthetic-input regression test for the recommendation rules.

    Covers every recommendation value (retire, tighten, widen, investigate,
    keep, insufficient_data, uninstrumented) plus the suppression paths that
    can flip one into another — the case count grows as new rules or gates
    land. Every branch in `_score_gate` must have at least one case that
    exercises it; see below for the exact branch-to-case mapping.

    Historical failure modes guarded:
      - retire false-positive on under-exercised gates (total < MIN_TOTAL_FIRINGS)
      - retire false-positive on FN-dominant all-noop preventive guards
        (dominant_risk=FN must route to investigate, not retire)
      - widen rule eaten by a meaningful-firings volume guard
      - MIN_RATE_SAMPLES collapsed into MIN_TOTAL_FIRINGS (tighten fires on
        total-volume instead of rate-sample-volume)
      - any future rule-ordering regression that flips a recommendation

    Exits 0 on all-pass, 1 on any failure (machine-readable for CI use).
    Prints a one-line summary per case so failures are diagnosable from
    stdout alone.
    """
    min_fires = 5
    cases = [
        # (label, gate dict, counts dict, expected_recommendation)
        ("retire-pure-noise",
         {"id": "_test", "instrumented": True, "retirement_eligible": True,
          "keyword_bias": "balanced"},
         Counter({"noop": 15}),
         "retire"),
        # FN-dominant retire false-positive guard (rb 2026-05-28): identical
        # counts to retire-pure-noise; only dominant_risk=FN flips the outcome
        # to investigate. An all-noop FN-dominant gate is a working preventive
        # guard (canonical: prose-verification-drift), not a dead gate. Without
        # the dominant_risk guard in _score_gate, the retire rule would delete it.
        ("fn-dominant-all-noop-investigate-not-retire",
         {"id": "_test", "instrumented": True, "retirement_eligible": True,
          "keyword_bias": "balanced", "dominant_risk": "FN"},
         Counter({"noop": 15}),
         "investigate"),
        ("retire-suppressed-by-low-volume",
         {"id": "_test", "instrumented": True, "retirement_eligible": True,
          "keyword_bias": "balanced"},
         Counter({"noop": 1}),
         "insufficient_data"),
        ("retire-suppressed-by-not-eligible",
         {"id": "_test", "instrumented": True, "retirement_eligible": False,
          "keyword_bias": "balanced"},
         Counter({"noop": 15}),
         "keep"),
        ("tighten-high-override-rate",
         {"id": "_test", "instrumented": True, "retirement_eligible": True,
          "keyword_bias": "strict"},
         Counter({"block": 5, "override": 12, "noop": 1}),
         "tighten"),
        # Regression guard: MIN_RATE_SAMPLES gates `bo_total` (block+override),
        # NOT `total`. If anyone collapses it back into MIN_TOTAL_FIRINGS
        # (e.g. checks `total >= min_fires` instead of `bo_total >= min_fires`
        # on the tighten path), this case flips from "keep" → "tighten"
        # because total=10 is above min_fires while bo_total=4 is not.
        ("tighten-needs-rate-samples-distinct-from-total",
         {"id": "_test", "instrumented": True, "retirement_eligible": True,
          "keyword_bias": "strict"},
         Counter({"noop": 6, "block": 1, "override": 3}),
         "keep"),
        ("widen-high-noop-rate-fn-dominant",
         {"id": "_test", "instrumented": True, "retirement_eligible": True,
          "keyword_bias": "generous"},
         Counter({"noop": 62, "block": 3}),
         "widen"),
        ("widen-suppressed-by-balanced-bias",
         {"id": "_test", "instrumented": True, "retirement_eligible": True,
          "keyword_bias": "balanced"},
         Counter({"noop": 62, "block": 3}),
         "keep"),
        ("investigate-on-fail-open",
         {"id": "_test", "instrumented": True, "retirement_eligible": True,
          "keyword_bias": "balanced"},
         Counter({"block": 1, "fail_open": 1}),
         "investigate"),
        ("keep-balanced-firings",
         {"id": "_test", "instrumented": True, "retirement_eligible": True,
          "keyword_bias": "balanced"},
         Counter({"noop": 22, "block": 23, "override": 1}),
         "keep"),
        ("insufficient-data-zero-firings",
         {"id": "_test", "instrumented": True, "retirement_eligible": True,
          "keyword_bias": "balanced"},
         Counter(),
         "insufficient_data"),
        ("uninstrumented-skipped",
         {"id": "_test", "instrumented": False, "retirement_eligible": True,
          "keyword_bias": "balanced"},
         Counter({"noop": 99}),
         "uninstrumented"),
    ]
    failures = 0
    for label, gate, counts, expected in cases:
        result = _score_gate(gate, counts, min_fires)
        actual = result["recommendation"]
        ok = actual == expected
        if not ok:
            failures += 1
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {label}: expected={expected!r} actual={actual!r}")
    print()
    if failures:
        print(f"FAILED: {failures} of {len(cases)} self-test case(s) failed.")
        return 1
    print(f"OK: all {len(cases)} self-test case(s) passed.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Evaluate gate firings and recommend retirement / "
                    "tightening / widening per gate.")
    ap.add_argument("--days", type=int, default=30,
                    help="Look-back window in days (default 30).")
    ap.add_argument("--min-fires", type=int, default=10,
                    help="Volume threshold applied to both named guards "
                         "(MIN_TOTAL_FIRINGS for retire/widen/insufficient_data, "
                         "MIN_RATE_SAMPLES for tighten). Default 10.")
    ap.add_argument("--gate", default=None,
                    help="Restrict output to a single gate id.")
    ap.add_argument("--output", default="json", choices=["json", "human"],
                    help="Output format (default json).")
    ap.add_argument("--self-test", action="store_true",
                    help="Run synthetic-input regression tests against the "
                         "recommendation rules and exit. No telemetry read, "
                         "no gates.yaml read. Exit 0 on all-pass, 1 on any "
                         "failure. Wired into /verify-learning.")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()

    gates = _load_gates()
    if args.gate:
        gates = [g for g in gates if g.get("id") == args.gate]
        if not gates:
            print(f"[gate-retirement-eval] no gate '{args.gate}' in gates.yaml",
                  file=sys.stderr)
            return 2

    since = datetime.now() - timedelta(days=args.days)
    counts_per_gate = {}
    for rec in _load_firings(since):
        gid = rec.get("gate_id")
        if not gid:
            continue
        if args.gate and gid != args.gate:
            continue
        # decision is guaranteed present and valid by _load_firings (see
        # _VALID_DECISIONS gate there). Any drift surfaces as a WARN skip,
        # not a silent "noop" miscount.
        counts_per_gate.setdefault(gid, Counter())[rec["decision"]] += 1

    rows = []
    for gate in gates:
        gid = gate["id"]
        counts = counts_per_gate.get(gid, Counter())
        scored = _score_gate(gate, counts, args.min_fires)
        scored["gate_id"] = gid
        rows.append(scored)

    # Sort: action items first, then keep / insufficient / uninstrumented.
    rec_order = {"retire": 0, "tighten": 1, "widen": 2, "investigate": 3,
                 "keep": 4, "insufficient_data": 5, "uninstrumented": 6}
    rows.sort(key=lambda r: (rec_order.get(r["recommendation"], 99),
                             -r["evidence"]["total_firings"]))

    if args.output == "human":
        print(_human_table(rows))
    else:
        print(json.dumps({
            "window_days": args.days,
            "min_fires": args.min_fires,
            "evaluated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "gates_evaluated": len(rows),
            "recommendations": rows,
        }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
