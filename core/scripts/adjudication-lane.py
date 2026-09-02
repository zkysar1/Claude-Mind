#!/usr/bin/env python3
"""adjudication-lane.py — sampler + ledger for the  knowledge-adjudication pilot.

WHAT THIS IS FOR
----------------
arXiv:2607.19592 (Knowledge-Centric Self-Improvement) reports its gains coming from
knowledge being EVIDENCE ADJUDICATED THROUGH DISCUSSION, not from single-agent
abstraction. Our shared stores (reasoning bank, guardrails) admit entries on the
ENCODING agent's own evidence discipline: write-time gates check structure and
duplication, never truth. This lane is the bounded pilot that measures whether a
post-commit, cross-agent, stance-taking review pass catches wrong entries at a rate
worth its cost. An evidenced DROP is a fully acceptable outcome.

DESIGN CONSTRAINTS THIS SCRIPT ENCODES (each was retrieved, not invented)
------------------------------------------------------------------------
* HARD CONSTRAINT from the goal: the encode path must NOT block on review. This
  script only READS the stores and appends to its own ledger — it is post-commit by
  construction, so the constraint holds structurally, not by discipline.
* guard-2770 — a cadence ritual whose action is "post a finding" has NO READ-BACK, so
  it re-derives the same finding forever and never notices. THE LEDGER IS THAT
  READ-BACK: `sample` excludes every entry_id already carrying a review row. This is
  the single most load-bearing line in the file.
* guard-718 / guard-5058 — a cadence over a SHARED resource must consult a SHARED
  last-fire stamp, or all five agents independently re-review the same entries.
  `stamp` writes team-state `shared_cadences.knowledge_adjudication`; the ledger's
  cross-agent read-back is the second (stronger) layer, because it dedups by ENTRY
  rather than by time.
* guard-4688 — a recurring goal's achievedCount counts FIRINGS, not MEASUREMENTS. The
  pilot window is therefore counted from LEDGER ROWS, never from achievedCount.
* Survivorship denominator (goal outcome 3) — `report` never folds UNRESOLVED
  challenges into "did not survive". Reviewed / challenged / resolved / survived are
  four separate raw counts and the ratio is refused below MIN_REPORTABLE_N.
* Self-exclusion — an agent reviewing its own entry reproduces exactly the bias the
  lane exists to correct, so `sample` drops rows whose `encoded_by` is the reviewer.

WHY ONLY TWO STORES
-------------------
Scope names tree/rb/guardrails. Only the reasoning bank and guardrails carry a
reliable `encoded_by`; tree nodes do not, so self-exclusion there would be silently
unreliable — worse than an excluded store. Tree is OUT OF SCOPE for the pilot, and
that limitation is written into the window-open ledger row so the final report carries
it rather than losing it. This is a stated scope, not a discovered enumeration
(guard-1969 concerns predicates that age behind a population; this one is the pilot's
own boundary and is reported every run).

REVIEWER COST is not measurable from here — a script cannot see its caller's tokens.
`record --turns N` accepts a reviewer-supplied count; when absent, `report` says cost
is UNMEASURED rather than inventing a proxy.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import WORLD_DIR, AGENT_NAME, PROJECT_ROOT, assert_world_dir  # noqa: E402

LEDGER = Path(WORLD_DIR) / "telemetry" / "adjudication-lane-ledger.jsonl"
CADENCE_SLOT = "knowledge_adjudication"
LANE_TAG = "adjudication-lane"
STANCES = ("AGREE", "DISAGREE", "SYNTHESIZE")
CHALLENGE_STANCES = ("DISAGREE", "SYNTHESIZE")
# Pilot bounds, from the goal: "2 weeks or 100 reviewed entries, whichever first".
WINDOW_DAYS = 14
WINDOW_ENTRIES = 100
# Below this, a catch-rate is refused rather than reported — a ratio off a handful of
# entries chosen because the reviewer happened to hold evidence is worse than none.
MIN_REPORTABLE_N = 20

SCOPE_STORES = ("reasoning_bank", "guardrails")
# LANE-PROVENANCE EXCLUSION (guard-2019 — caught by zeta on this very goal, 2026-09-01,
# and confirmed live before fixing: rb-9888, an entry THIS LANE produced 20 minutes
# earlier, was offered back to a peer as a review candidate). A ritual must not count
# its own mandated receipts as input signal: if a review's durable lesson is encoded as
# an rb/guardrail entry, the next cycle reviews it, and the lane feeds on its own output
# indefinitely — with volume that reads as productivity.
#
# The filter keys on a WRITE-TIME TAG, never on phrasing. A phrase match would be an
# ownership predicate relaxed into a pattern (guard-2860) and would silently swallow
# unrelated entries that merely mention the lane. Consequence, and it is a REQUIREMENT
# on the reviewer rather than on this script: any store entry a review produces MUST
# carry LANE_TAG at write time, or this exclusion cannot see it and the lane re-reviews
# its own output. That obligation is stated here, in the instrument, because a rule kept
# only in a goal description is not read by whoever runs the next pass (rb-7613).
OUT_OF_SCOPE = {"knowledge_tree": "no reliable encoded_by field, so self-exclusion would be silent"}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _bash() -> str:
    # guard-580: never a bare "bash" in an ad-hoc argv.
    return shutil.which("bash") or "/bin/bash"


def _read_store(script: str, args: list) -> list:
    """Read a store through its framework script — never by parsing the JSONL."""
    path = Path(PROJECT_ROOT) / "core" / "scripts" / script
    if not path.exists():
        raise SystemExit("adjudication-lane: missing store reader %s" % path)
    proc = subprocess.run(
        [_bash(), str(path)] + args,
        capture_output=True, text=True, cwd=str(PROJECT_ROOT),
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        # A silently-empty read is ZERO signals, not an empty store (guard-2298).
        raise SystemExit(
            "adjudication-lane: %s %s returned rc=%d, %d bytes — refusing to treat "
            "that as an empty store. stderr: %s"
            % (script, " ".join(args), proc.returncode, len(proc.stdout), proc.stderr.strip()[:300])
        )
    data = json.loads(proc.stdout)
    return data if isinstance(data, list) else [data]


def _ledger_rows() -> list:
    if not LEDGER.exists():
        return []
    rows = []
    with LEDGER.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _append(row: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _reviewed_ids(rows: list) -> set:
    return {r["entry_id"] for r in rows if r.get("kind") == "review" and r.get("entry_id")}


def cmd_sample(args) -> int:
    me = args.agent or AGENT_NAME
    seen = _reviewed_ids(_ledger_rows())
    pool = []
    for entry in _read_store("reasoning-bank-read.sh", ["--recent", str(args.scan)]):
        entry["_store"] = "reasoning_bank"
        pool.append(entry)
    guards = _read_store("guardrails-read.sh", ["--active"])
    guards.sort(key=lambda e: str(e.get("created") or ""), reverse=True)
    for entry in guards[: args.scan]:
        entry["_store"] = "guardrails"
        pool.append(entry)

    def _is_lane_output(e):
        return LANE_TAG in (e.get("tags") or [])

    scanned = len(pool)
    self_authored = sum(1 for e in pool if str(e.get("encoded_by") or "") == me)
    already = sum(1 for e in pool if e.get("id") in seen)
    lane_output = sum(1 for e in pool if _is_lane_output(e))
    candidates = [
        e for e in pool
        if str(e.get("encoded_by") or "") != me
        and e.get("id") not in seen
        and e.get("encoded_by")
        and not _is_lane_output(e)
    ]
    candidates.sort(key=lambda e: str(e.get("created") or ""), reverse=True)
    # STRATIFY BY AUTHOR — recency alone is not a fleet sample. Measured on the first
    # live run of this script: sorting by `created` desc returned 6 of 6 entries from a
    # single author, because one agent happened to be encoding heavily that hour. The
    # pilot would then have measured ONE agent's encoding discipline and reported it as
    # the fleet's catch-rate. That is the survivorship-denominator defect the report
    # guards against, occurring one stage earlier where the report cannot see it.
    # Round-robin over authors, newest-first within each.
    by_author = {}
    for e in candidates:
        by_author.setdefault(e.get("encoded_by"), []).append(e)
    picked = []
    while len(picked) < args.n and any(by_author.values()):
        progressed = False
        for author in sorted(by_author):
            if len(picked) >= args.n:
                break
            if by_author[author]:
                picked.append(by_author[author].pop(0))
                progressed = True
        if not progressed:
            break

    out = {
        "reviewer": me,
        "scanned": scanned,
        "excluded_self_authored": self_authored,
        "excluded_already_reviewed": already,
        "excluded_lane_output": lane_output,
        "candidates": len(candidates),
        "returned": len(picked),
        "authors_available": sorted({str(e.get("encoded_by")) for e in candidates}),
        "authors_returned": sorted({str(e.get("encoded_by")) for e in picked}),
        "stores_in_scope": list(SCOPE_STORES),
        "stores_out_of_scope": OUT_OF_SCOPE,
        "entries": [
            {
                "entry_id": e.get("id"),
                "store": e.get("_store"),
                "encoded_by": e.get("encoded_by"),
                "created": e.get("created"),
                "category": e.get("category"),
                "headline": (e.get("title") or e.get("rule") or e.get("description") or "")[:220],
            }
            for e in picked
        ],
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def cmd_record(args) -> int:
    stance = args.stance.upper()
    if stance not in STANCES:
        raise SystemExit("adjudication-lane: stance must be one of %s" % (STANCES,))
    rows = _ledger_rows()
    if args.entry_id in _reviewed_ids(rows):
        # Not an error: the read-back working is the point (guard-2770).
        print(json.dumps({"skipped": True, "reason": "already reviewed", "entry_id": args.entry_id}))
        return 0
    row = {
        "kind": "review",
        "entry_id": args.entry_id,
        "store": args.store,
        "authored_by": args.authored_by,
        "reviewed_by": args.agent or AGENT_NAME,
        "stance": stance,
        "board_msg": args.board_msg,
        "basis": args.basis,
        "review_turns": args.turns,
        "at": _now(),
        "box": os.uname().nodename if hasattr(os, "uname") else None,
    }
    _append(row)
    print(json.dumps({"recorded": True, **row}, ensure_ascii=False))
    return 0


def _capture_confidence_truth_event(entry_id, survived, evidence, rows=None):
    """ — join this verdict to the entry's DECLARED CONFIDENCE.

    A resolution is a truth event: a recorded claim just met evidence. The pair
    (declared confidence, verdict) is the only input a calibration curve has, and it
    exists for exactly this instant — once the entry is edited the confidence it was
    CARRYING at judgement time is unrecoverable. So capture here or never.

    Best-effort by contract: this is audit, not the lane's job. An import failure, a
    missing store or a write error must never fail a resolution the reviewer already
    performed, so everything is swallowed.

    MEASURED, and the reason most rows here will carry a NULL confidence: this lane's
    SCOPE_STORES are reasoning_bank and guardrails, which almost never carry the field
    (63/9466 = 0.67% and 3/5434 = 0.06%), while tree nodes — 530/1551 = 34% — are
    deliberately OUT OF SCOPE for the pilot because they lack a reliable `encoded_by`
    for self-exclusion (see the module docstring). Both constraints are individually
    correct; together they mean this surface supplies verdicts far more often than it
    supplies the confidence to score them against. The rows are still worth capturing
    (the verdict and evidence are real, and a null is honest data about the STORES),
    but whoever builds the calibration table must bucket on
    `declared_confidence is not None` first and report the null count — otherwise the
    denominator is a fiction.
    """
    try:
        from _confidence_ledger import record_truth_event
    except Exception:
        return
    store = None
    try:
        for r in (rows if rows is not None else _ledger_rows()):
            if r.get("kind") == "review" and r.get("entry_id") == entry_id:
                store = r.get("store")
                break
    except Exception:
        store = None
    try:
        record_truth_event(
            entry_id,
            store or "unknown",
            "survived" if survived else "refuted",
            source=LANE_TAG,
            evidence_ref=evidence,
        )
    except Exception:
        return


def cmd_resolve(args) -> int:
    row = {
        "kind": "resolution",
        "entry_id": args.entry_id,
        "challenge_survived": args.survived,
        "evidence": args.evidence,
        "resolved_by": args.agent or AGENT_NAME,
        "at": _now(),
    }
    _append(row)
    _capture_confidence_truth_event(args.entry_id, args.survived, args.evidence)
    print(json.dumps({"recorded": True, **row}, ensure_ascii=False))
    return 0


def cmd_open_window(args) -> int:
    rows = _ledger_rows()
    if any(r.get("kind") == "window" and r.get("event") == "opened" for r in rows):
        print(json.dumps({"skipped": True, "reason": "window already opened"}))
        return 0
    row = {
        "kind": "window",
        "event": "opened",
        "at": _now(),
        "target_days": WINDOW_DAYS,
        "target_entries": WINDOW_ENTRIES,
        "opened_by": args.agent or AGENT_NAME,
        "goal": "g-306-395",
        "stores_in_scope": list(SCOPE_STORES),
        "stores_out_of_scope": OUT_OF_SCOPE,
        "min_reportable_n": MIN_REPORTABLE_N,
    }
    _append(row)
    print(json.dumps({"recorded": True, **row}, ensure_ascii=False))
    return 0


def cmd_stamp(args) -> int:
    """Write the SHARED last-fire stamp (guard-718). Fail-open: never block the lane."""
    payload = json.dumps({"at": _now(), "fired_by": args.agent or AGENT_NAME, "goal": "g-306-395"})
    script = Path(PROJECT_ROOT) / "core" / "scripts" / "team-state-update.sh"
    proc = subprocess.run(
        [_bash(), str(script), "--field", "shared_cadences.%s" % CADENCE_SLOT, "--value", payload],
        capture_output=True, text=True, cwd=str(PROJECT_ROOT),
    )
    print(json.dumps({
        "stamped": proc.returncode == 0,
        "slot": "shared_cadences.%s" % CADENCE_SLOT,
        "rc": proc.returncode,
        "stderr": proc.stderr.strip()[:200],
    }))
    return 0


def cmd_report(args) -> int:
    rows = _ledger_rows()
    reviews = [r for r in rows if r.get("kind") == "review"]
    resolutions = {r["entry_id"]: r for r in rows if r.get("kind") == "resolution"}
    opened = next((r for r in rows if r.get("kind") == "window" and r.get("event") == "opened"), None)

    challenged = [r for r in reviews if r.get("stance") in CHALLENGE_STANCES]
    resolved = [r for r in challenged if r["entry_id"] in resolutions]
    survived = [r for r in resolved if resolutions[r["entry_id"]].get("challenge_survived") is True]

    days_elapsed = None
    if opened:
        days_elapsed = round(
            (datetime.now() - datetime.strptime(opened["at"], "%Y-%m-%dT%H:%M:%S")).total_seconds() / 86400.0, 2
        )

    by_stance = {}
    for r in reviews:
        by_stance[r.get("stance")] = by_stance.get(r.get("stance"), 0) + 1
    by_reviewer = {}
    for r in reviews:
        by_reviewer[r.get("reviewed_by")] = by_reviewer.get(r.get("reviewed_by"), 0) + 1

    n = len(reviews)
    if n >= MIN_REPORTABLE_N:
        catch_rate = round(len(challenged) / n, 4)
        catch_rate_note = "challenged / reviewed"
    else:
        catch_rate = None
        catch_rate_note = (
            "REFUSED: n=%d < MIN_REPORTABLE_N=%d. A ratio off this few entries reflects "
            "which entries the reviewer happened to hold evidence about, not the lane."
            % (n, MIN_REPORTABLE_N)
        )

    turns = [r.get("review_turns") for r in reviews if r.get("review_turns")]
    window_closed = bool(opened) and (
        n >= WINDOW_ENTRIES or (days_elapsed is not None and days_elapsed >= WINDOW_DAYS)
    )

    out = {
        "goal": "g-306-395",
        "ledger": str(LEDGER),
        "ledger_exists": LEDGER.exists(),
        "window": {
            "opened_at": opened.get("at") if opened else None,
            "days_elapsed": days_elapsed,
            "target_days": WINDOW_DAYS,
            "entries_reviewed": n,
            "target_entries": WINDOW_ENTRIES,
            "closed": window_closed,
            "note": "counted from LEDGER ROWS, never from achievedCount (guard-4688)",
        },
        "raw_counts": {
            "reviewed": n,
            "challenged": len(challenged),
            "challenges_resolved": len(resolved),
            "challenges_survived": len(survived),
            "challenges_UNRESOLVED": len(challenged) - len(resolved),
        },
        "by_stance": by_stance,
        "by_reviewer": by_reviewer,
        "catch_rate": catch_rate,
        "catch_rate_note": catch_rate_note,
        "survivorship_note": (
            "UNRESOLVED challenges are reported separately and are NOT counted as "
            "'did not survive'. A survival ratio computed over resolved-only is a "
            "different quantity from one over all challenges — say which."
        ),
        "reviewer_cost": (
            {"measured": True, "turns_total": sum(turns), "rows_with_turns": len(turns), "rows": n}
            if turns else
            {"measured": False, "reason": "no reviewer supplied --turns; a script cannot see its caller's tokens"}
        ),
        "stores_in_scope": list(SCOPE_STORES),
        "stores_out_of_scope": OUT_OF_SCOPE,
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    assert_world_dir("adjudication-lane")
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--agent", default=None, help="reviewer identity (default: bound agent)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sample", help="candidate entries to review (self-authored + already-reviewed excluded)")
    s.add_argument("--n", type=int, default=5)
    s.add_argument("--scan", type=int, default=60, help="how many recent entries per store to consider")
    s.set_defaults(func=cmd_sample)

    s = sub.add_parser("record", help="append a stance to the ledger")
    s.add_argument("--entry-id", required=True)
    s.add_argument("--store", required=True, choices=list(SCOPE_STORES))
    s.add_argument("--authored-by", required=True)
    s.add_argument("--stance", required=True)
    s.add_argument("--board-msg", required=True, help="board message id carrying the critique")
    s.add_argument("--basis", default=None, help="one line: what evidence the stance rests on")
    s.add_argument("--turns", type=int, default=None, help="reviewer-supplied cost in turns")
    s.set_defaults(func=cmd_record)

    s = sub.add_parser("resolve", help="record whether a challenge survived re-verification")
    s.add_argument("--entry-id", required=True)
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--survived", dest="survived", action="store_true")
    g.add_argument("--not-survived", dest="survived", action="store_false")
    s.add_argument("--evidence", default=None)
    s.set_defaults(func=cmd_resolve)

    s = sub.add_parser("open-window", help="stamp the pilot window open (idempotent)")
    s.set_defaults(func=cmd_open_window)

    s = sub.add_parser("stamp", help="write the SHARED cadence stamp (guard-718)")
    s.set_defaults(func=cmd_stamp)

    s = sub.add_parser("report", help="window status + raw counts + catch-rate")
    s.set_defaults(func=cmd_report)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
