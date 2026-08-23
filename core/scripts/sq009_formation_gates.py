#!/usr/bin/env python3
"""sq-009 formation gates: calibration cap + category saturation.

Mechanizes the two deterministic computations that every sq-009 hypothesis
formation must run before a confidence can be written (gap-071, resolved
satisfied-by-extension rather than forged — the union accuracy already exists
in the daemon's pipeline meta; what was missing is the per-arm split, the band
mapping, the aggregate caveat, and saturation).

Both computations have produced measured, silent, wrong answers in this fleet:

  * g-115-4005 tallied the `resolution` key instead of `outcome` and got
    total=2 / accuracy=0.0 — tightening the cap a FULL BAND. It did not return
    zero, so nothing looked broken.
  * g-001-10 parsed 0 rows across all three stages because the reader emits a
    pretty-printed JSON ARRAY, not JSONL, and a try/except laundered it.

Both are defended here, once, for every future caller.

OUTPUT CONTRACT (required, not a logging nicety): n_resolved and n_archived are
reported SEPARATELY, never as a single total. A single number cannot show
whether the union was actually read, so a silent regression to the
resolved-only fetch would print identically to a correct run — the per-arm n is
the only place that difference is visible (guard-2529 / guard-2273 / guard-2191).

KNOWN INHERITED DEFECT, DECLARED IN OUR OWN OUTPUT: an AGGREGATE accuracy
cannot denominate a PER-BAND ceiling. A ceiling asserts a realized frequency AT
a confidence level; the input averages across levels, and the two coincide only
if accuracy is flat across bands, which it is not. This tool emits
`cap_basis: "aggregate"` and a `caveat` string so the defect is visible at the
point of use rather than silently inherited. Tracked at g-115-4715.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _runtime_bash import bash_cmd  # noqa: E402

# Exact enum values. NEVER substring-match "correct": CORRECTED means the belief
# was WRONG yet contains the substring, so a substring test counts misses as hits
# and inverts the verdict (guard-654, measured 2026-05-26 in ).
OUTCOME_HIT = "CONFIRMED"
OUTCOME_MISS = "CORRECTED"
# UNRESOLVABLE is neither a hit nor a miss and is excluded from the denominator.

# Fixed bands, from the gap spec. accuracy -> confidence ceiling.
BANDS = ((0.40, 0.55), (0.60, 0.65), (0.80, 0.80))

DEFAULT_SATURATION_THRESHOLD = 3

CAVEAT = (
    "cap_basis is AGGREGATE: this ceiling is denominated by accuracy averaged "
    "across all confidence levels, but a ceiling asserts a realized frequency "
    "AT a level. The two coincide only if accuracy is flat across bands, which "
    "it is not. Do not read this as a per-band ceiling (g-115-4715)."
)


def _read_stage(stage: str) -> list[dict]:
    """Read one pipeline stage via the canonical daemon-aware wrapper.

    Shape-tolerant BY MEASUREMENT, not by hope: the reader emits a
    pretty-printed JSON array, so a line-wise JSONL parse silently yields 0
    rows (g-001-10). We try whole-document first, fall back to line-wise, and
    POSITIVE-CONTROL the result — a zero row count against non-zero bytes is
    self-refuting and raises rather than returning a confident empty
    (guard-2298: print the shape and the byte count beside the record count).
    """
    # bash_cmd, never a bare "bash" argv[0]: on win32 that resolves to the
    # System32 WSL stub and can hang forever (guard-580), and it passes the
    # path via as_posix() because bash strips the backslashes of a
    # str(WindowsPath) into a silently nonexistent path (guard-581).
    proc = subprocess.run(
        bash_cmd(SCRIPT_DIR / "pipeline-read.sh", "--stage", stage),
        capture_output=True,
        text=True,
    )
    raw = proc.stdout.strip()
    if proc.returncode != 0 and not raw:
        raise RuntimeError(
            f"pipeline-read.sh --stage {stage} failed rc={proc.returncode}: "
            f"{proc.stderr.strip()[:300]}"
        )

    records: list[dict] = []
    if raw:
        try:
            doc = json.loads(raw)
            records = doc if isinstance(doc, list) else doc.get("records", doc.get("hypotheses", []))
        except json.JSONDecodeError:
            records = [
                json.loads(ln) for ln in raw.splitlines() if ln.strip().startswith("{")
            ]

    # Positive control. A zero against a non-trivial payload means the parse
    # missed the shape — never report it as an empty stage.
    if not records and len(raw) > 2:
        raise RuntimeError(
            f"stage={stage}: parsed 0 records from {len(raw)} bytes — shape "
            f"mismatch, not an empty stage. First 200 bytes: {raw[:200]!r}"
        )
    return [r for r in records if isinstance(r, dict)]


def _tally(records: list[dict]) -> tuple[int, int]:
    """Return (confirmed, corrected) by EXACT match on the outcome enum."""
    outcomes = Counter(r.get("outcome") for r in records)
    return outcomes[OUTCOME_HIT], outcomes[OUTCOME_MISS]


def cap_for(accuracy: float) -> float | None:
    """Map an accuracy in [0,1] to its confidence ceiling, or None for no cap."""
    for threshold, cap in BANDS:
        if accuracy < threshold:
            return cap
    return None


def calibration_gate() -> dict:
    resolved = _read_stage("resolved")
    archived = _read_stage("archived")

    # Reconcile against `pipeline-read.sh --meta`, which the gap spec names as
    # the cheapest cross-check for the OVERALL arm. It counts ANY record with a
    # terminal outcome regardless of stage, while this gate reads the
    # resolved+archived UNION the spec mandates. The two therefore differ by
    # records that are resolved-but-not-yet-`pipeline-move`d, and that gap is
    # real: measured 2026-08-21, 852 here vs 856 there, all 4 sitting in
    # stage=active with a terminal outcome. Reporting the stray count makes the
    # cross-check reconcile instead of reading as a regression in this tool.
    strays = sum(
        len([r for r in _read_stage(st) if r.get("outcome") in (OUTCOME_HIT, OUTCOME_MISS)])
        for st in ("discovered", "active", "measurement-pending")
    )

    r_hit, r_miss = _tally(resolved)
    a_hit, a_miss = _tally(archived)

    n_resolved = r_hit + r_miss
    n_archived = a_hit + a_miss
    confirmed, corrected = r_hit + a_hit, r_miss + a_miss
    total = confirmed + corrected
    accuracy = (confirmed / total) if total else 0.0

    return {
        # Both arms, always, separately. This is the output contract.
        "n_resolved": n_resolved,
        "n_archived": n_archived,
        "n_total": total,
        "confirmed": confirmed,
        "corrected": corrected,
        "accuracy": round(accuracy, 4),
        "accuracy_pct": round(accuracy * 100, 1),
        "cap": cap_for(accuracy) if total else None,
        "cap_basis": "aggregate",
        "caveat": CAVEAT,
        "gate_skipped": total == 0,  # no track record yet -> caller skips the gate
        # Reconciliation with `pipeline-read.sh --meta`: meta_total == n_total +
        # n_terminal_outside_union. A non-zero stray count is a store condition
        # (records awaiting pipeline-move), NOT a defect in this gate.
        "n_terminal_outside_union": strays,
        "meta_crosscheck_expected": total + strays,
    }


def saturation_gate(threshold: int = DEFAULT_SATURATION_THRESHOLD) -> dict:
    """Count in-flight hypotheses per category so formation can steer away."""
    records = _read_stage("active") + _read_stage("discovered")
    counts = Counter(r.get("category") for r in records if r.get("category"))
    saturated = sorted(
        (c for c, n in counts.items() if n >= threshold),
        key=lambda c: (-counts[c], c),
    )
    return {
        "threshold": threshold,
        "n_in_flight": len(records),
        "by_category": dict(counts.most_common()),
        "saturated": saturated,
        "steer_away_from": saturated,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", help="emit JSON (default: human)")
    ap.add_argument(
        "--saturation-threshold",
        type=int,
        default=DEFAULT_SATURATION_THRESHOLD,
        help=f"categories at or above this count are flagged (default {DEFAULT_SATURATION_THRESHOLD})",
    )
    args = ap.parse_args()

    result = {
        "calibration": calibration_gate(),
        "saturation": saturation_gate(args.saturation_threshold),
    }

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    c, s = result["calibration"], result["saturation"]
    cap = "none" if c["cap"] is None else f"{c['cap']:.2f}"
    print(
        f"calibration: {c['n_total']} scoreable over resolved+archived "
        f"(resolved {c['n_resolved']} / archived {c['n_archived']}), "
        f"{c['accuracy_pct']}% accurate -> cap {cap}"
    )
    print(f"  basis: {c['cap_basis']} — {c['caveat']}")
    if c["gate_skipped"]:
        print("  NOTE: no track record yet (n=0) — SKIP the calibration gate.")
    print(
        f"saturation: {s['n_in_flight']} in flight; "
        f"{len(s['saturated'])} categor(ies) at/above {s['threshold']}"
    )
    for cat in s["saturated"]:
        print(f"  steer away: {cat} ({s['by_category'][cat]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
