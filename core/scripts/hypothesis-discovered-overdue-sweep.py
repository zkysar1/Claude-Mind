#!/usr/bin/env python3
"""Sweep discovered-stage hypotheses orphaned past their deadline (;
formed_date+horizon fallback g-115-1981).

review-hypotheses/SKILL.md Mode 1 Step 1 loads `--stage active` +
`--stage measurement-pending` ONLY, so hypothesis records orphaned in
stage=discovered past their resolves_by are never resolved -- invisible to the
review path, never feed accuracy stats (only resolved records do), silently
distorting calibration. At filing (2026-06-24): 63 of 193 discovered records
were overdue, the oldest >2 months stale.

The deadline is resolves_by when present; when ABSENT (the common case -- most
discovered hypotheses are drafted without one) it falls back to formed_date +
the horizon's expected resolution window (g-115-1981). Before that fallback,
resolves_by-absent records were skipped and never swept, accumulating silently
until a manual pass (g-115-1976 hand-triaged 165 such records) drained them.

This sweep (rb-428 sweep family; sibling of recurring-precondition-sweep.py /
stranded-claim-sweep.py) classifies each OVERDUE discovered record and acts
only where the action is mechanically safe:

  - EXPIRE  -> archived w/ outcome=UNRESOLVABLE: short/session-horizon records
              overdue past the expiry window (their observation window closed
              long ago, so the prediction can never be settled now). The daemon
              move-to-archived path is EXEMPT from validate_formation_quality
              (only active/resolved targets are gated -- pipeline_write.move
              L448), so under-formed records CAN be archived mechanically.
              UNRESOLVABLE is exempt from accuracy stats, so this drains the
              orphaned bulk without polluting calibration.
  - PROMOTE -> active: records that PASS validate_formation_quality for the
              active stage (claim>=20 + resolution method + [short] measurement
              channel -- guard-798). The review-hypotheses loop then resolves
              them with judgment THIS run. Promotion reuses the existing gate
              (no field synthesis), so a record that cannot pass is never
              force-promoted.
A FOURTH lane, ELIGIBLE (g-115-4721), covers the interval BEFORE overdue
begins -- past the eligibility floor, deadline still in the future -- which
until 2026-08-26 was watched by nothing at all and whose `0 overdue` reading was
repeatedly recorded as "pipeline clean". See classify_eligible.

  - NEEDS_JUDGMENT (no mechanical action): recently-overdue but under-formed
              records (bare position / missing resolution fields). Surfaced for
              LLM claim synthesis -- NEVER auto-resolved. This is the explicit
              "do NOT blindly auto-resolve all of them" boundary: only the LLM
              can synthesize a claim from a bare position (guard-798 gotcha).

Conservative, idempotent (a moved record leaves the discovered stage, so a
re-run skips it), and fail-open (any daemon/move error is logged to stderr and
the sweep continues; exit is always 0). The classifier (`classify_overdue`) is
PURE and unit-tested; `main()` does the file read + daemon writes.

Usage:
    py -3 core/scripts/hypothesis-discovered-overdue-sweep.py [--apply]
        [--output json|text] [--expire-days-short N] [--expire-days-long N]

Dry-run by default (reports what WOULD move). `--apply` performs the moves via
the daemon (_rt.rt_call POST /v1/pipeline/move -- python-native, no bash
subprocess per rb-225/rb-247). Invoked from review-hypotheses Mode 1 Step 1 as
a pre-scan before the active-stage load. Wired: g-115-1629.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
import _paths  # noqa: E402
from pipeline import validate_formation_quality  # noqa: E402  (gate mirror)

DEFAULT_EXPIRE_DAYS_SHORT = 30
DEFAULT_EXPIRE_DAYS_LONG = 90

# : when a discovered record has NO parseable resolves_by, fall back to
# formed_date + the horizon's expected resolution window to synthesize an
# effective deadline. Without this fallback, resolves_by-absent records -- the
# majority, since most discovered hypotheses are drafted without a resolves_by --
# were skipped entirely and never swept, silently accumulating in the discovered
# stage (the manual cleanup that motivated this:  triaged 165 such
# records by hand). Windows are "time from formation until the prediction should
# be settleable": a micro hypothesis resolves within a day, a long one over months.
HORIZON_WINDOW_DAYS = {"micro": 1, "session": 3, "short": 14, "long": 90}

_ID_DATE_RX = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def _parse_iso(s):
    """Parse an ISO date/datetime string; None on any failure (e.g. the
    session-horizon 'session_end' sentinel, which is never date-overdue)."""
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "")[:19])
    except (ValueError, TypeError):
        return None


def _formed_date(rec):
    """Formation date of a record: an explicit formed/created field if present,
    else the ID-date prefix (pipeline IDs are `YYYY-MM-DD_slug`). None if neither
    is parseable. Used only as the fallback basis when resolves_by is absent."""
    for k in ("formed_date", "created", "discovered_at", "created_at", "date"):
        d = _parse_iso(rec.get(k))
        if d is not None:
            return d
    m = _ID_DATE_RX.match(str(rec.get("id") or ""))
    return _parse_iso(m.group(1)) if m else None


def effective_deadline(rec):
    """The date past which a discovered record counts as overdue.

    resolves_by when present and parseable (unchanged behavior); otherwise, when
    resolves_by is truly ABSENT, formed_date + the horizon's HORIZON_WINDOW_DAYS
    window (g-115-1981 fallback). Returns (deadline, basis) with basis in
    {'resolves_by', 'formed+horizon'}, or (None, None) when no deadline can be
    derived.

    A resolves_by that is PRESENT but not a parseable date -- the deliberate
    'session_end' sentinel, or any other non-date marker -- is NOT treated as
    absent: it stays skipped (pre-g-115-1981 behavior), because it was set on
    purpose to mean "resolves at session end, not on a calendar date." Only a
    MISSING / None / empty resolves_by triggers the formed_date+horizon fallback."""
    raw = rec.get("resolves_by")
    rb = _parse_iso(raw)
    if rb is not None:
        return rb, "resolves_by"
    if raw is not None and str(raw).strip():
        # Present-but-non-date sentinel (e.g. 'session_end') -> intentional, skip.
        return None, None
    formed = _formed_date(rec)
    if formed is None:
        return None, None
    horizon = (rec.get("horizon") or "short").strip().lower()
    window = HORIZON_WINDOW_DAYS.get(horizon, HORIZON_WINDOW_DAYS["short"])
    return formed + timedelta(days=window), "formed+horizon"


def passes_active_formation(rec):
    """True iff the record would pass validate_formation_quality at stage=active.
    Reuses the existing gate (the daemon mirror) so PROMOTE never force-moves a
    record the daemon would reject. Any exception => not promotable (fail-safe)."""
    test = dict(rec)
    test["stage"] = "active"
    try:
        validate_formation_quality(test)
        return True
    except Exception:
        return False


def classify_overdue(records, now, expire_days_short=DEFAULT_EXPIRE_DAYS_SHORT,
                     expire_days_long=DEFAULT_EXPIRE_DAYS_LONG):
    """Pure classifier. `records`: list of pipeline record dicts (any stage).

    Returns {scanned, overdue, expire[], promote[], needs_judgment[]}. Only
    stage=='discovered' records with a PAST effective deadline are considered
    overdue -- resolves_by when present, else formed_date + horizon window
    (g-115-1981); records with neither a resolves_by nor a parseable formation
    date are ignored.

    PAST means strictly EARLIER CALENDAR DAY than `now`, not earlier instant: a
    deadline falling on today has not passed yet, because a date-only deadline
    denotes the END of its day (guard-2073). `now` is still a datetime -- only
    the comparison is date-granular.
    """
    expire, promote, needs = [], [], []
    overdue = 0
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if rec.get("stage") != "discovered":
            continue
        deadline, _basis = effective_deadline(rec)
        # DATE granularity, strict '<' (guard-2073). A date-only resolves_by --
        # 93.6% of live non-terminal records, measured 2026-07-31 -- parses to
        # MIDNIGHT at the START of its day, so the naive `deadline < now` marked a
        # deadline with up to ~24h still remaining as already past. This matches
        # the pinned authority pipeline_write._is_stale_unactivated
        # (`date.fromisoformat(str(rb)[:10]) < today`), so the two sweeps reading
        # this same field cannot disagree on the boundary day again;
        # cadence_signals._resolvable_hypotheses_present agrees generously
        # (<= today). Truncating a datetime resolves_by to its date too (rather
        # than branching on granularity) is deliberate: it keeps ONE predicate
        # shared with the sibling, and every bucket below thresholds in whole
        # days, so sub-day precision buys nothing and a later boundary is the
        # fail-safe direction for a sweep whose expire[] bucket is one-way.
        if deadline is None or deadline.date() >= now.date():
            continue  # no derivable deadline, or not yet overdue
        overdue += 1
        overdue_days = (now.date() - deadline.date()).days
        horizon = (rec.get("horizon") or "short").strip().lower()
        # Long-horizon windows are months; everything else (short/session/unset)
        # uses the short threshold.
        thresh = expire_days_long if horizon == "long" else expire_days_short
        if overdue_days > thresh:
            expire.append(rec)            # window closed -> UNRESOLVABLE
        elif passes_active_formation(rec):
            promote.append(rec)           # recent + well-formed -> review path
        else:
            needs.append(rec)             # recent + under-formed -> LLM judgment
    return {
        "scanned": len(records),
        "overdue": overdue,
        "expire": expire,
        "promote": promote,
        "needs_judgment": needs,
    }


def classify_eligible(records, now):
    """Pure classifier for the ELIGIBLE lane (): discovered records
    past their eligibility floor but NOT yet overdue.

    Deliberately DISJOINT from classify_overdue over the same population:
    that one takes `deadline.date() < now.date()`, this one takes
    `deadline.date() >= now.date()`, so no record is classified by both and
    nothing is ever moved twice. Same date-granular boundary (guard-2073), so
    the two lanes cannot disagree about the boundary day.

    WHY THIS LANE EXISTS. Before it, a discovered record that was eligible to
    resolve but not yet late was reachable by NOTHING: review-hypotheses Mode 1
    loads active + measurement-pending only, and classify_overdue gates on the
    deadline having PASSED. That interval is precisely when a hypothesis is most
    resolvable -- in-window, measurement channel live, evidence still cheap --
    so the system reliably looked at these records only once they could no
    longer be settled, at which point classify_overdue's one-way expire[] lane
    archives them UNRESOLVABLE. Measured 2026-08-26 on the live store: 26 of 44
    discovered records sat in this interval, 16 already passing the
    active-formation gate, the oldest eligible for 99 days.

    THE ZERO IT PRODUCED READ AS HEALTH, which is why the gap survived two prior
    goals aimed at this same store. The sweep reported only `overdue`, so
    sessions ran it, saw `0 overdue`, and recorded "pipeline clean / stores
    healthy" (bravo 2026-07-09 and 2026-07-22, foxtrot 2026-08-17) while this
    population sat unsurfaced. A gated detector's clean verdict says nothing
    about what its gate excluded.

    PARKING IS RESPECTED, and that is why the remedy is a sweep rather than
    promote-at-creation. A FUTURE resolves_no_earlier_than is a deliberate
    eligibility floor -- measured as systematic 7d and 30d offsets across 12
    live records -- and it OUTRANKS the deadline here exactly as it does in
    precheck-eval.py's hypothesis-health gate. Parked records are skipped, not
    promoted.

    Returns {scanned, eligible, promote[], needs_judgment[]}. Under-formed
    records are SURFACED only: never force-promoted (same passes_active_formation
    gate the overdue lane uses, so a record the daemon would reject is never
    moved) and never expired, because an eligible record's observation window is
    by definition still open.
    """
    promote, needs = [], []
    eligible = 0
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if rec.get("stage") != "discovered":
            continue
        deadline, _basis = effective_deadline(rec)
        if deadline is None or deadline.date() < now.date():
            continue  # no derivable deadline, or overdue -> classify_overdue's lane
        rne = _parse_iso(rec.get("resolves_no_earlier_than"))
        if rne is not None and rne.date() > now.date():
            continue  # still parked behind a deliberate eligibility floor
        eligible += 1
        (promote if passes_active_formation(rec) else needs).append(rec)
    return {
        "scanned": len(records),
        "eligible": eligible,
        "promote": promote,
        "needs_judgment": needs,
    }


def _read_discovered():
    """Read stage=='discovered' records directly from the live pipeline file.
    Direct read mirrors recurring-precondition-sweep.py (the file is the source
    of truth); writes still route through the daemon (single-writer)."""
    path = _paths.WORLD_DIR / "pipeline.jsonl"
    out = []
    if not path.exists():
        return out
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(r, dict) and r.get("stage") == "discovered":
                    out.append(r)
    except OSError as e:
        sys.stderr.write(f"discovered-overdue-sweep: read pipeline.jsonl failed: {e}\n")
    return out


def _move(rec_id, stage, merge, apply):
    """Move a record via the daemon (python-native; no bash subprocess).
    Returns (ok: bool, detail: str). Fail-open: errors logged, never raised."""
    if not apply:
        return True, "dry-run"
    try:
        import _rt  # noqa: E402
        body = json.dumps(merge) if merge else "{}"
        _rt.rt_call("POST", "/v1/pipeline/move",
                    query="id=%s&stage=%s" % (_rt._q(rec_id), _rt._q(stage)),
                    body=body)
        return True, "ok"
    except Exception as e:  # RtError or any transport failure -> fail-open
        sys.stderr.write(
            f"discovered-overdue-sweep: move {rec_id} -> {stage} failed: {e}\n")
        return False, str(e)[:160]


def main() -> int:
    ap = argparse.ArgumentParser(description="Sweep overdue discovered-stage hypotheses (g-115-1629)")
    ap.add_argument("--apply", action="store_true",
                    help="Perform the moves (default: dry-run report only).")
    ap.add_argument("--output", choices=["json", "text"], default="text")
    ap.add_argument("--expire-days-short", type=int, default=DEFAULT_EXPIRE_DAYS_SHORT,
                    help="short/session-horizon overdue days past which a record is EXPIRED UNRESOLVABLE")
    ap.add_argument("--expire-days-long", type=int, default=DEFAULT_EXPIRE_DAYS_LONG,
                    help="long-horizon overdue days past which a record is EXPIRED UNRESOLVABLE")
    ap.add_argument("--no-eligible", action="store_true",
                    help="skip the ELIGIBLE lane (past eligibility floor, not yet overdue). "
                         "The lane is ON by default on purpose: an opt-in flag nobody passes "
                         "is a sweep that never runs, which is the gap this lane closes.")
    args = ap.parse_args()

    now = datetime.now()
    records = _read_discovered()
    c = classify_overdue(records, now, args.expire_days_short, args.expire_days_long)

    now_iso = now.replace(microsecond=0).isoformat()
    expired_ids, promoted_ids, failed = [], [], []

    for rec in c["expire"]:
        rid = rec.get("id")
        deadline, basis = effective_deadline(rec)
        # Date-granular, matching classify_overdue's predicate above — the note
        # below is the human-readable account of the same decision, so the two
        # must not report different day counts.
        days = (now.date() - deadline.date()).days if deadline else "?"
        if basis == "formed+horizon":
            window_desc = (
                f"derived deadline (formed_date + {(rec.get('horizon') or 'short')}"
                f" horizon window; no resolves_by)"
            )
        else:
            window_desc = f"resolves_by {rec.get('resolves_by')}"
        merge = {
            "outcome": "UNRESOLVABLE",
            "outcome_note": (
                f"discovered-overdue-sweep (g-115-1629): {window_desc} was "
                f"{days}d past; observation window closed -- prediction can no "
                f"longer be settled. Swept {now_iso}."
            ),
        }
        ok, _ = _move(rid, "archived", merge, args.apply)
        (expired_ids if ok else failed).append(rid)

    for rec in c["promote"]:
        rid = rec.get("id")
        ok, _ = _move(rid, "active", None, args.apply)
        (promoted_ids if ok else failed).append(rid)

    # ELIGIBLE lane (). Runs over the SAME `records` list and is
    # disjoint from the overdue lane by predicate, so a record reached here was
    # not touched above.
    e = {"eligible": 0, "promote": [], "needs_judgment": []}
    eligible_promoted = []
    if not args.no_eligible:
        e = classify_eligible(records, now)
        for rec in e["promote"]:
            rid = rec.get("id")
            ok, _ = _move(rid, "active", None, args.apply)
            (eligible_promoted if ok else failed).append(rid)

    result = {
        "scanned": c["scanned"],
        "overdue": c["overdue"],
        "expired": expired_ids,
        # UNION of both lanes, deliberately. review-hypotheses Step 1.0 documents
        # that `promoted` records "are now stage=active and WILL appear in the
        # active load below" -- true of eligible-lane promotions too, and having
        # them resolved THIS run is the entire point of surfacing them early.
        "promoted": promoted_ids + eligible_promoted,
        "needs_judgment": [r.get("id") for r in c["needs_judgment"]],
        # SEPARATE key, deliberately NOT merged into needs_judgment: the caller's
        # documented handling for that key ends in "resolve/expire with
        # judgment", and EXPIRING an eligible record would close an observation
        # window that is still open -- the exact harm this lane exists to prevent.
        "eligible": e["eligible"],
        "eligible_promoted": eligible_promoted,
        "eligible_needs_judgment": [r.get("id") for r in e["needs_judgment"]],
        "failed": failed,
        "applied": args.apply,
    }

    if args.output == "json":
        print(json.dumps(result))
    else:
        print(
            f"[discovered-overdue-sweep] scanned={result['scanned']} "
            f"overdue={result['overdue']} eligible={result['eligible']} "
            f"{'APPLIED' if args.apply else 'DRY-RUN'}: expired={len(expired_ids)} "
            f"promoted={len(result['promoted'])} (overdue {len(promoted_ids)} + "
            f"eligible {len(eligible_promoted)}) "
            f"needs_judgment={len(result['needs_judgment'])} "
            f"eligible_needs_judgment={len(result['eligible_needs_judgment'])} "
            f"failed={len(failed)}"
        )
        for rid in result["needs_judgment"][:10]:
            print(f"  needs-judgment (synthesize claim from position, then resolve/expire): {rid}")
        for rid in result["eligible_needs_judgment"][:10]:
            print(f"  eligible-needs-judgment (in-window, under-formed -- synthesize a claim; do NOT expire): {rid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
