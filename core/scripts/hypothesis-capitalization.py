"""Report BOTH hypothesis-pipeline health axes: ACCURACY and CAPITALIZATION ().

THE GAP THIS CLOSES. The pipeline has two independent health axes and only one was
instrumented:
  ACCURACY       — of hypotheses that got a verdict, how many were right.
                   `pipeline-read.sh --accuracy` reports this (56.6%).
  CAPITALIZATION — of hypotheses that TERMINATED, how many got a verdict at all.
                   Nothing reported this.
`--accuracy` cannot cover the second: its `total_resolved: 760` is exactly
CONFIRMED(430) + CORRECTED(330), so EXPIRED, UNRESOLVABLE and null-outcome records are
in neither its numerator nor its denominator. Measured on cc-07 2026-08-15: 831 of 1591
records (52.2%) sit outside it, and no flag includes them. Accuracy could hold at 57%
while every newly-formed hypothesis expired unresolved and that instrument would not
move. This is the guard-1700 class (a status-filtered denominator inflates
one-directionally) with no re-run-with-terminal-statuses remedy available.

Both axes are reported together, always, per the Decision Rule in
world/knowledge/tree/performance/agent-performance/hypothesis-calibration.md: they
DIVERGED across July (completion rose while accuracy stayed flat), so a single "how are
the hypotheses doing" number is unanswerable and reporting one alone invites the reader
to treat it as the whole picture.

=============================================================================
WHY THIS DOES **NOT** WRITE meta/audit-baselines.yaml — READ BEFORE WIRING IT UP
=============================================================================
The goal that commissioned this asked for a `hypothesis_capitalization` entry in
meta/audit-baselines.yaml. That was measured and REFUSED on three independent grounds.
Any one of them is sufficient; do not re-derive them by shipping the ratchet.

1. THE RATCHET DIRECTION IS INVERTED FOR A RATE. Verified in code, not inferred, at
   temp-citation-ratchet.py L228-L258 (the family's reference shape): `cur > prior` ->
   "regressed" (WARN); `cur < prior` -> "ratcheted" ("OK ... Baseline lowered"), and the
   lowering is ONE-WAY. Every one of the 8 live baselines is a lower-is-better drift
   COUNT. Capitalization is higher-is-better, so a collapse 78% -> 60% would report
   verdict "ratcheted", message "OK", exit 0, and permanently move the bar down to 60 —
   while a genuine improvement to 85% would WARN. The instrument would report the
   failure it exists to detect as a success, and each successive decline would erase the
   evidence of the last.

2. THE CONVENTION FORBIDS IT, TWICE AND EXPLICITLY.
   core/config/conventions/audit-baselines.md, "When to use": "If the metric is a ratio,
   latency, or anything continuous — use a different mechanism (gates, thresholds,
   alerts). Not this file." And under Anti-patterns: "Baselining a ratio or continuous
   metric (wrong tool — use a gate)."

3. THE PROPOSED SEED VALUE IS MEASURED ON A BIASED SUBSAMPLE. `outcome_date` is present
   on only 82.8% of terminal records, so every dated trend — including the 78.0% seed —
   silently excludes a fifth of the population. Measured cc-07 2026-08-15: the excluded
   cohort capitalizes at 41.4% against the dated cohort's 75.1%, a 33.7-point gap, and
   is systematically OLD (193 of 249 formed 2026-04/05, three in 2026-08). So part of
   the "58% -> 78% rise" is the exclusion of a badly-performing old cohort rather than
   improvement. The commissioning goal's own build note says "Do not seed a ratchet
   FLOOR before the measurement is stable"; by its own standard it is not.
   This report therefore ALWAYS prints the undated sub-population beside the headline
   (guard-3524: a clean aggregate is not a clean population).

If a ratchet is genuinely wanted later, the shape that survives objections 1 and 2 is a
windowed non-negative COUNT of terminated-without-verdict records — but note that a bare
loss count rises with hypothesis VOLUME at constant quality, so it needs its denominator
carried (guard-3542). The honest reading of the convention is that this metric wants a
threshold/alert, not this file. That is a decision for the reducer, not a worker Body.

CORPUS PRECEDENCE, FIXED DELIBERATELY. Records are read from BOTH pipeline.jsonl and
pipeline-archive.jsonl and deduped by id (guard-3523 — a naive line-union double-counts;
337 ids appear in both files). Live wins: measured, exactly 3 overlapping ids disagree on
outcome and in 2 of the 3 the live store holds the more-advanced value. The choice moves
the headline by 0.02pp, so it is immaterial to the number — but leaving it to incidental
read order would make the result depend on the order the files happen to be listed in,
which is not a measurement.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import WORLD_DIR  # noqa: E402

# Archive FIRST so the live file overwrites on collision — see CORPUS PRECEDENCE above.
STORE_FILES = ("pipeline-archive.jsonl", "pipeline.jsonl")

LEARNED = ("CONFIRMED", "CORRECTED")
LOST = ("EXPIRED", "UNRESOLVABLE")
TERMINAL = LEARNED + LOST


def load_corpus(world_dir=None):
    """Deduped id -> record across both store files, plus per-file diagnostics.

    Returns (records, diagnostics). A store that is missing or unreadable is NAMED in
    diagnostics rather than silently contributing zero — losing one file drops the
    denominator and would read as a capitalization shift that never happened.
    """
    base = Path(world_dir) if world_dir is not None else (
        Path(WORLD_DIR) if WORLD_DIR else None)
    diag = {"files": [], "missing": [], "malformed_lines": 0}
    records = {}
    if base is None:
        diag["missing"].append("<WORLD_DIR unresolved>")
        return records, diag
    for name in STORE_FILES:
        path = base / name
        if not path.is_file():
            diag["missing"].append(name)
            continue
        rows = 0
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                diag["malformed_lines"] += 1
                continue
            if isinstance(rec, dict) and rec.get("id"):
                records[rec["id"]] = rec
                rows += 1
        diag["files"].append({"name": name, "rows": rows})
    return records, diag


def _split(rows):
    learned = sum(1 for r in rows if r.get("outcome") in LEARNED)
    return learned, len(rows) - learned


def _pct(num, den):
    return round(100.0 * num / den, 1) if den else None


def compute(records):
    terminal = [r for r in records.values() if r.get("outcome") in TERMINAL]
    dated = [r for r in terminal if r.get("outcome_date")]
    undated = [r for r in terminal if not r.get("outcome_date")]

    learned, lost = _split(terminal)
    d_learned, d_lost = _split(dated)
    u_learned, u_lost = _split(undated)

    # ACCURACY: of records that got a VERDICT, how many were right. Deliberately the
    # same denominator pipeline-read.sh --accuracy uses (CONFIRMED+CORRECTED) so the two
    # numbers are comparable; widening it here would silently change the meaning of
    # accuracy_pct for every existing consumer and every recorded historical value.
    confirmed = sum(1 for r in records.values() if r.get("outcome") == "CONFIRMED")
    corrected = sum(1 for r in records.values() if r.get("outcome") == "CORRECTED")

    by_month = collections.defaultdict(lambda: [0, 0])
    for r in dated:
        month = str(r["outcome_date"])[:7]
        by_month[month][0 if r.get("outcome") in LEARNED else 1] += 1

    return {
        "corpus_total": len(records),
        "accuracy": {
            "verdicted": confirmed + corrected,
            "confirmed": confirmed,
            "corrected": corrected,
            "accuracy_pct": _pct(confirmed, confirmed + corrected),
        },
        "capitalization": {
            "terminal": len(terminal),
            "learned": learned,
            "lost": lost,
            "capitalization_pct": _pct(learned, learned + lost),
        },
        # The whole point of guard-3524: the headline above is computed over `terminal`,
        # but every DATED trend silently drops `undated`. Both are reported so a reader
        # cannot see the trend without also seeing what it excludes.
        "subpopulations": {
            "dated": {"n": len(dated), "learned": d_learned, "lost": d_lost,
                      "capitalization_pct": _pct(d_learned, d_learned + d_lost)},
            "undated": {"n": len(undated), "learned": u_learned, "lost": u_lost,
                        "capitalization_pct": _pct(u_learned, u_learned + u_lost)},
            "undated_share_pct": _pct(len(undated), len(terminal)),
        },
        # Every bucket carries its own n (guard-3542): a rate trended without its
        # denominator cannot distinguish improvement from a collapsing sample.
        "by_month": {
            m: {"n": lo + ls, "learned": lo, "lost": ls,
                "capitalization_pct": _pct(lo, lo + ls)}
            for m, (lo, ls) in sorted(by_month.items())
        },
    }


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Report both hypothesis-pipeline health axes (accuracy + "
                    "capitalization). Read-only; writes nothing.")
    ap.add_argument("--json", action="store_true", help="emit JSON only")
    ap.add_argument("--world-dir", default=None, help="override WORLD_DIR (tests)")
    args = ap.parse_args(argv)

    records, diag = load_corpus(args.world_dir)

    # Vacuous-zero guard (rb-245 / guard-1641): an empty corpus makes every rate
    # undefined, and "0%" would read as a catastrophic capitalization collapse rather
    # than as a failed read. Report unmeasurable and say why.
    if not records:
        payload = {"verdict": "unmeasurable", "diagnostics": diag,
                   "message": "no pipeline records loaded — capitalization is "
                              "UNDEFINED, not 0. Check WORLD_DIR resolution and the "
                              "store files named in diagnostics."}
        print(json.dumps(payload, indent=2))
        print("[hypothesis-capitalization] UNMEASURABLE: %s" % payload["message"],
              file=sys.stderr)
        return 0

    result = compute(records)
    result["verdict"] = "measured"
    result["diagnostics"] = diag

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    acc, cap, sub = result["accuracy"], result["capitalization"], result["subpopulations"]
    print(json.dumps(result, indent=2))
    print("", file=sys.stderr)
    print("[hypothesis-capitalization] BOTH AXES (corpus %d records)"
          % result["corpus_total"], file=sys.stderr)
    print("  ACCURACY       %s%% of %d verdicted (%d confirmed / %d corrected)"
          % (acc["accuracy_pct"], acc["verdicted"], acc["confirmed"], acc["corrected"]),
          file=sys.stderr)
    print("  CAPITALIZATION %s%% of %d terminated (%d learned / %d lost)"
          % (cap["capitalization_pct"], cap["terminal"], cap["learned"], cap["lost"]),
          file=sys.stderr)
    print("  CAVEAT: %s%% of terminal records carry no outcome_date and are INVISIBLE "
          "to every dated trend; that cohort capitalizes at %s%% against the dated "
          "cohort's %s%%. Do not read a dated trend as the population."
          % (sub["undated_share_pct"], sub["undated"]["capitalization_pct"],
             sub["dated"]["capitalization_pct"]), file=sys.stderr)
    if diag["missing"]:
        print("  WARNING: store file(s) missing: %s — the denominator is incomplete "
              "and the rates above are NOT comparable to a full run."
              % ", ".join(diag["missing"]), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
