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
* guard-2193 — the catch-rate is a FLEET claim, so a corpus filled by one reviewer
  makes it a single-vantage reading. `report` therefore carries a second, independent
  guard on SCOPE (MAX_REVIEWER_SHARE) beside the one on sample size, and publishes a
  per-reviewer catch-rate split. Concentration QUALIFIES the number and never
  withholds it — unlike a thin n, a concentrated n is precise and merely mislabelled.
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
import re
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

# WHAT A CHALLENGE FOUND, which is a different question from what STANCE it took
# (). The two kinds carry OPPOSITE consequences for the pilot's
# adopt/drop decision, so summing them is not a rounding error:
#   wrong               -- the ENTRY is defective. Evidence FOR a standing review lane.
#   under-cross-linked  -- the entry is correct but its nearest neighbours on the
#                          MECHANISM axis are uncited. That is a corpus-wide
#                          citation-hygiene gap which ONE SWEEP fixes, and it is
#                          evidence AGAINST standing review as the remedy.
# `unclassified` is deliberately NOT a member: it is the STATE of a row written
# before this field existed, and keeping it out of the vocabulary is what stops
# it being recorded as a finding.
CHALLENGE_KINDS = ("wrong", "under-cross-linked")
# Pilot bounds, from the goal: "2 weeks or 100 reviewed entries, whichever first".
WINDOW_DAYS = 14
WINDOW_ENTRIES = 100
# Below this, a catch-rate is refused rather than reported — a ratio off a handful of
# entries chosen because the reviewer happened to hold evidence is worse than none.
MIN_REPORTABLE_N = 20
# Above this share held by ONE reviewer, the lane-level catch-rate is that reviewer's
# reading and not the lane's. This guards a DIFFERENT axis from MIN_REPORTABLE_N: that
# constant guards the DENOMINATOR (too few rows, so the ratio is noise), while this one
# guards the SCOPE (plenty of rows, ratio precise, but drawn from one vantage). The two
# come apart, and only the first was ever checked — measured on this lane 2026-09-07,
# n=46 passed the n>=20 gate comfortably while by_reviewer read {alpha: 42, echo: 3,
# bravo: 1}, i.e. 91.3% one reviewer. Three consecutive review passes flagged that in
# prose and none could make the instrument say it, so `report` kept publishing a
# fleet-labelled number an adopt/drop decision was to be encoded from. 0.60 is the line
# where one reviewer holds more than everyone else combined.
MAX_REVIEWER_SHARE = 0.60

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
    # WHAT the challenge found (). Optional by design: making it
    # required would refuse the very callers whose rows this split exists to
    # interpret, and an AGREE row has nothing to classify. An unset kind is
    # reported as `unclassified`, never guessed from the free-text `basis`.
    kind = getattr(args, "challenge_kind", None)
    if kind is not None and kind not in CHALLENGE_KINDS:
        raise SystemExit(
            "adjudication-lane: --challenge-kind must be one of %s (got %r)"
            % (CHALLENGE_KINDS, kind)
        )
    if kind is not None and stance not in CHALLENGE_STANCES:
        raise SystemExit(
            "adjudication-lane: --challenge-kind is meaningless on stance %s -- "
            "only %s are challenges" % (stance, CHALLENGE_STANCES)
        )
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
        "challenge_kind": kind,
        "board_msg": args.board_msg,
        "basis": args.basis,
        "review_turns": args.turns,
        "at": _now(),
        "box": os.uname().nodename if hasattr(os, "uname") else None,
    }
    _append(row)
    print(json.dumps({"recorded": True, **row}, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------------------
# ONE BOOLEAN, TWO OPPOSITE SUBJECTS (). `challenge_survived` is about
# the CHALLENGE; the calibration ledger's `verdict` is about the ENTRY, and
# core/config/conventions/confidence-calibration-ledger.md settles which is which
# in its first sentence: the ledger pairs "an entry's declared confidence with
# what later happened to ITS CLAIM", keyed on "the entry judged". So the two are
# INVERSES, and this lane used to pass the raw boolean straight through --
# `"survived" if survived else "refuted"` -- writing entry-survived on every row
# where the challenge was UPHELD, i.e. exactly the rows where the entry was wrong.
#
# Measured at the time of the fix: world/confidence-calibration-ledger.jsonl held
# 4 rows, ALL verdict=survived, including guard-5973 -- which the board records as
# RETIRED and superseded by guard-6019. A retired entry did not survive under
# either reading, so the inversion is decidable without resolving the ambiguity.
# A calibration curve over that store could only ever report perfect calibration.
#
# The two-state flag also collapses a three-state reality. _confidence_ledger
# already carries the vocabulary for it (VERDICTS = survived/refuted/revised/
# unknown), so this is a mapping, not a schema change:
#
#   challenge upheld + entry AMENDED               -> revised
#   challenge upheld + entry RETIRED / SUPERSEDED  -> refuted
#   challenge upheld + no verified artifact change -> unknown   <- see below
#   challenge did NOT survive (entry upheld)       -> survived
#
# The `unknown` row is the ACKNOWLEDGED-BUT-NOT-APPLIED state, and it is required
# rather than tidy: msg-20260904-204611-zeta-5272 records an acknowledgment that
# landed while the amendment did NOT (amended_fields {}, amended_at null) for
# twelve hours. A recorder that keys on an ack would have scored that as a
# resolution. Keying on the VERIFIED ARTIFACT CHANGE is what makes the third
# state countable instead of silently collapsing into survived or unresolved.
ARTIFACT_CHANGES = ("amended", "retired", "superseded", "none")


def entry_verdict(challenge_survived, artifact_change=None):
    """Map (challenge outcome, verified artifact change) -> the ENTRY's verdict.

    Pure and total: any unrecognised artifact_change degrades to "unknown" rather
    than guessing, because a wrong verdict here is indistinguishable downstream
    from a measured one and would manufacture the curve the ledger exists to
    measure (same posture as the convention's rule on `declared_confidence`).
    """
    if not challenge_survived:
        # The challenge did not survive re-verification, so the entry stood.
        return "survived"
    if artifact_change == "amended":
        return "revised"
    if artifact_change in ("retired", "superseded"):
        return "refuted"
    # challenge upheld but nothing verified on the artifact yet.
    return "unknown"


def _capture_confidence_truth_event(entry_id, verdict, evidence, rows=None):
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
            verdict,
            source=LANE_TAG,
            evidence_ref=evidence,
        )
    except Exception:
        return


def cmd_resolve(args) -> int:
    # artifact_change is what the ENTRY actually underwent, verified on the entry
    # itself -- never inferred from an acknowledgment post (see entry_verdict).
    artifact_change = getattr(args, "artifact_change", None)
    verdict = entry_verdict(args.survived, artifact_change)
    row = {
        "kind": "resolution",
        "entry_id": args.entry_id,
        "challenge_survived": args.survived,
        "artifact_change": artifact_change,
        "entry_verdict": verdict,
        "evidence": args.evidence,
        "resolved_by": args.agent or AGENT_NAME,
        "at": _now(),
    }
    _append(row)
    _capture_confidence_truth_event(args.entry_id, verdict, args.evidence)
    print(json.dumps({"recorded": True, **row}, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------------------
# AUTOMATED RESOLUTION SWEEP (, successor to ).
#
# The manual pass that repaired 4 inverted rows and drove UNRESOLVED 17 -> 7 is
# not repeatable by hand at window close: challenges keep arriving (11 unresolved
# when this landed, up from the 7 the goal description recorded), so the counters
# drift UNDERSTATED at exactly the adopt/drop decision the window exists to
# protect.
#
# THREE INDEPENDENT SIGNALS, validated on 17 entries by the predecessor and
# re-validated live on the 11 open challenges at implementation time. All
# applicable signals must agree before a row is written; any disagreement leaves
# the challenge unresolved for a human.
#
#   1. NON-EMPTY `amended_fields`, plus `status` for retired/superseded.
#      NEVER `amended_at`. Measured 2026-09-06 against both live stores: that
#      field is ABSENT ENTIRELY from reasoning_bank records and None on the
#      guardrail sampled, so keying on it reproduces the very undercount this
#      sweep exists to remove.
#   2. THE AMENDMENT MUST POSTDATE THE CHALLENGE. `amended_fields` VALUES are
#      ISO timestamps. Measured: 2 of the 4 live amended entries were amended
#      BEFORE their challenge; scoring them would have inflated resolutions
#      2 -> 4 (+100%), in the direction that flatters the lane.
#   3. THE ENTRY MUST CITE ITS CHALLENGE'S BOARD MESSAGE ID. This is what turns
#      ordering into causation. Measured on the live 11: both genuine responses
#      cite it, while both predating cases and all seven no-amendment cases cite
#      nothing — the same partition the 17-entry validation produced.
#
# WHAT THE SWEEP REFUSES TO DO, deliberately:
#   - It never auto-scores a no-amendment challenge `unknown`. That is the
#     ACKNOWLEDGED-BUT-NOT-APPLIED state, and asserting it requires evidence an
#     ack was posted — which this sweep does not read, because an ack is not a
#     resolution (the zeta/guard-5935 case).
#   - It never rewrites. The ledger is append-only and cmd_report dedupes by
#     entry_id last-row-wins, so repair is an append.
# ---------------------------------------------------------------------------


_MSG_TS = re.compile(r"^msg-(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})-")


def challenge_time(review):
    """The moment the critique was RAISED -> (iso, source). Pure and total.

    NOT the review row's `at`, which is when `cmd_record` ran — measured
    2026-09-06 across the live ledger, that lands 1-5 MINUTES AFTER the board
    post, because a reviewer posts the critique and then records the row. Keying
    the postdates test on `at` therefore mis-flags a genuine same-window response
    as predating: of the 4 amendments the first implementation flagged, 2
    (guard-6043 at 163s, rb-10223 at 65s) postdate their board post and were
    correctly hand-scored `revised` by the reviewer — a 50% false-withhold rate,
    in the direction that UNDERSTATES the lane.

    The board id is also the better key on independence grounds: it is stamped by
    the board at post time, by a different writer than the one appending this
    ledger row, so it cannot drift with how promptly the reviewer bookkeeps.
    Falls back to `at` when the id is absent or unparseable — a late proxy beats
    no ordering test at all, and the returned source says which was used.
    """
    m = _MSG_TS.match(str(review.get("board_msg") or ""))
    if m:
        return "%s-%s-%sT%s:%s:%s" % m.groups(), "board_msg"
    return review.get("at"), "ledger_at (board_msg absent/unparseable — late proxy)"


# The fields that carry an entry's PROSE, across both in-scope stores. The
# citation test searches THESE, not json.dumps(entry) — a whole-record substring
# match would also hit metadata (tags, source, a key name), and every such false
# hit pushes toward SCORING a resolution, which is the unsafe direction: it
# manufactures the very number the ledger exists to measure. Unknown//new fields
# are included by falling back to every string value ONLY when none of the named
# fields is present, so a store with a different body key degrades to the old
# behaviour rather than silently never matching.
_BODY_FIELDS = ("content", "rule", "action_hint", "description", "when_to_use",
                "title", "trigger_condition", "failure_lesson")


def _entry_body_text(entry):
    """Concatenated prose of an entry. Pure and total."""
    parts = [str(entry.get(f)) for f in _BODY_FIELDS if isinstance(entry.get(f), str)]
    if parts:
        return "\n".join(parts)
    return "\n".join(str(v) for v in entry.values() if isinstance(v, str))


def sweep_decision(entry, challenge_at, board_msg):
    """Pure and total: -> (artifact_change | None, reason).

    A non-None artifact_change means every applicable signal agreed and the pair
    is auto-resolvable. None means LEAVE IT UNRESOLVED, and `reason` names which
    signal withheld it — an unresolved challenge with a stated reason is the
    designed output, not a failure.
    """
    status = str(entry.get("status") or "").strip().lower()
    if status in ("retired", "superseded"):
        # A terminal artifact change is self-evidencing: the entry is gone as a
        # live claim, so ordering/citation cannot make it more or less true.
        return status, "status=%s (terminal artifact change)" % status

    amended = entry.get("amended_fields")
    if not isinstance(amended, dict) or not amended:
        return None, (
            "no-amendment: amended_fields empty, status=%s. NOT scored 'unknown' — "
            "that is the acknowledged-but-not-applied state and asserting it needs "
            "ack evidence this sweep deliberately does not read." % (status or "?")
        )

    stamps = sorted(v for v in amended.values() if isinstance(v, str) and v)
    if not stamps:
        return None, (
            "unorderable: amended_fields carries %d key(s) but no string timestamp "
            "value, so the postdates-challenge test cannot run" % len(amended)
        )

    latest = stamps[-1]
    if challenge_at and latest < challenge_at:
        return None, (
            "predates-challenge: latest amendment %s precedes challenge %s. Scoring "
            "it would inflate resolutions in the direction that flatters the lane."
            % (latest, challenge_at)
        )

    if board_msg and board_msg not in _entry_body_text(entry):
        return None, (
            "no-citation: entry does not cite its adjudication message %s, so the "
            "amendment's ordering is not established as causation" % board_msg
        )

    return "amended", (
        "amended_fields=%s latest=%s postdates challenge %s and entry cites %s"
        % (sorted(amended), latest, challenge_at, board_msg)
    )


STORE_READERS = {
    "reasoning_bank": "reasoning-bank-read.sh",
    "guardrails": "guardrails-read.sh",
}


def cmd_sweep(args) -> int:
    rows = _ledger_rows()
    reviews = [r for r in rows if r.get("kind") == "review"]
    challenged = [r for r in reviews if r.get("stance") in CHALLENGE_STANCES]
    # last-row-wins, matching cmd_report's dedupe so idempotency is judged against
    # the same row the report will read.
    latest_res = {
        r["entry_id"]: r
        for r in rows
        if r.get("kind") == "resolution" and r.get("entry_id")
    }

    cache, scored, withheld = {}, [], []
    for rev in challenged:
        eid, store = rev.get("entry_id"), rev.get("store")
        script = STORE_READERS.get(store)
        if not script:
            withheld.append({"entry_id": eid, "reason": "unknown store %r" % store})
            continue
        if eid not in cache:
            # _read_store raises SystemExit on a bad read — correct for the
            # single-entry commands, WRONG here. This loop runs unattended over
            # every open challenge and has already APPENDED rows for the ones
            # before it, so an abort mid-loop leaves a partial application and
            # prints no summary at all (the summary is after the loop). One
            # deleted or unreadable entry must cost that entry, not the sweep.
            # It is still never treated as an empty store: the entry is WITHHELD
            # with the failure text as its reason (guard-2298 — a failed read is
            # zero signals, not evidence of absence).
            try:
                recs = _read_store(script, ["--id", eid])
            except SystemExit as exc:
                withheld.append({
                    "entry_id": eid,
                    "reason": "store-read-failed: %s" % str(exc)[:200],
                })
                continue
            cache[eid] = recs[0] if recs else {}
        chal_at, chal_src = challenge_time(rev)
        change, reason = sweep_decision(cache[eid], chal_at, rev.get("board_msg"))
        if change is not None:
            reason = "%s [challenge time from %s]" % (reason, chal_src)
        if change is None:
            withheld.append({"entry_id": eid, "reason": reason})
            continue
        verdict = entry_verdict(True, change)
        prev = latest_res.get(eid)
        if prev is not None and prev.get("entry_verdict") == verdict:
            withheld.append({
                "entry_id": eid,
                "reason": "idempotent: latest resolution already entry_verdict=%s" % verdict,
            })
            continue
        row = {
            "kind": "resolution",
            "entry_id": eid,
            "challenge_survived": True,
            "artifact_change": change,
            "entry_verdict": verdict,
            "evidence": reason,
            "resolved_by": args.agent or AGENT_NAME,
            "resolved_via": "sweep",
            "challenge_board_msg": rev.get("board_msg"),
            "at": _now(),
        }
        if args.apply:
            _append(row)
            _capture_confidence_truth_event(eid, verdict, reason)
        scored.append(row)

    by_verdict = {}
    for r in scored:
        by_verdict[r["entry_verdict"]] = by_verdict.get(r["entry_verdict"], 0) + 1
    withheld_reasons = {}
    for w in withheld:
        key = w["reason"].split(":", 1)[0]
        withheld_reasons[key] = withheld_reasons.get(key, 0) + 1

    print(json.dumps({
        "applied": bool(args.apply),
        "challenged_total": len(challenged),
        "entries_read": len(cache),
        "scored_count": len(scored),
        "by_entry_verdict": by_verdict,
        "withheld_count": len(withheld),
        "withheld_by_reason": withheld_reasons,
        "scored": scored,
        "withheld": withheld,
    }, indent=2, ensure_ascii=False))
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


def reviewer_concentration(reviews):
    """Per-reviewer distribution, and whether one reviewer's rows dominate.

    REPORTED, NEVER REFUSED — and the asymmetry with MIN_REPORTABLE_N is the
    point. A thin `n` means the ratio is noise, so withholding it loses nothing.
    A concentrated `n` means the ratio is PRECISE and its SCOPE is narrower than
    its label: the number is real, it is just one reviewer's. Killing it would
    destroy a figure the window-close reader legitimately needs; the remedy is to
    relabel it and publish the per-reviewer split beside it, which is exactly what
    guard-2193 prescribes for a fleet-scoped condition read through a
    single-vantage instrument ("report the distribution, or state explicitly that
    the reading is one agent's vantage").

    `per_reviewer` carries each reviewer's OWN catch-rate because that is the
    discriminator the pilot actually needs: a second reviewer reproducing the
    first's challenge rate is the only thing separating "these entries need
    challenging" from "this reviewer challenges things". A lane-level mean cannot
    express that, however large n gets.

    Pure (list of review rows in, dict out) so it is testable without a ledger,
    matching this file's existing `entry_verdict` / `sweep_decision` idiom.
    """
    per = {}
    for r in reviews:
        cell = per.setdefault(r.get("reviewed_by"), {"reviewed": 0, "challenged": 0})
        cell["reviewed"] += 1
        if r.get("stance") in CHALLENGE_STANCES:
            cell["challenged"] += 1
    for cell in per.values():
        cell["catch_rate"] = round(cell["challenged"] / cell["reviewed"], 4)

    n = len(reviews)
    dominant, dominant_n = None, 0
    for who, cell in per.items():
        # Ties resolve to the first-seen reviewer, and which name wins is
        # immaterial: the caller branches on the SHARE, and a tie puts at least
        # two reviewers at the maximum, so the share is <= 0.5 and can never
        # cross a threshold at or above that. The name only ever appears inside
        # a warning that a tie cannot trigger.
        if cell["reviewed"] > dominant_n:
            dominant, dominant_n = who, cell["reviewed"]
    share = round(dominant_n / n, 4) if n else None

    return {
        "distinct_reviewers": len(per),
        "dominant_reviewer": dominant,
        "dominant_share": share,
        "threshold": MAX_REVIEWER_SHARE,
        "concentrated": share is not None and share > MAX_REVIEWER_SHARE,
        "per_reviewer": per,
        "note": (
            "Guards SCOPE, not sample size — MIN_REPORTABLE_N guards the denominator "
            "and is silent on whose rows fill it. Both can pass while the lane-level "
            "rate generalises to exactly one reviewer."
        ),
    }


def catch_rate_by_challenge_kind(reviews):
    """Split the headline catch_rate by WHAT the challenge found, not just how many.

    THE DEFECT (g-306-466; measured by bravo on cc-05 2026-09-07, n=5 stratified
    across all four non-self authors): every sampled entry drew the SAME challenge
    and no other -- its MECHANISM-axis neighbours were uncited while its
    SUBJECT-axis citations were correct. A reviewer finding five DIFFERENT problems
    is measuring entries; a reviewer finding the SAME problem five times is
    measuring how entries get WRITTEN. Summed into one catch_rate, that corpus-wide
    hygiene gap reads as an entry-quality problem, and the lane would adopt a
    standing review pass to fix something one sweep fixes. The pilot's adopt/drop
    decision (g-306-401) is made on exactly this number.

    THIRD KNOB ON ONE INSTRUMENT, and they guard different things:
      MIN_REPORTABLE_N       -- the DENOMINATOR (too few rows, the ratio is noise)
      reviewer_concentration -- the SCOPE       (whose rows fill it)
      this                   -- the MEANING     (what the numerator is counting)
    guard-5060: a share instrument typically has several population knobs, and
    every one belongs beside the number rather than in a reader's memory.

    REPORTED, NEVER REFUSED, and `unclassified` is a STATE, not a kind. All 53
    review rows in the ledger when this shipped predate the field, so a split that
    quietly dropped them would publish a confident rate over a tiny classified
    tail -- the same mislabelling this function exists to prevent, one level down.
    The unclassified count and `classified_share` therefore travel WITH the split
    (guard-4859: the denominator rides with the share). A caller reading
    `by_kind` without reading `classified_share` is reading a rate whose scope it
    has not checked.

    Pure (list of review rows in, dict out), matching this file's `entry_verdict`
    / `sweep_decision` / `reviewer_concentration` idiom, so it is testable without
    a ledger.
    """
    n = len(reviews)
    challenges = [r for r in reviews if r.get("stance") in CHALLENGE_STANCES]
    by_kind = {}
    unclassified = 0
    for r in challenges:
        k = r.get("challenge_kind")
        if k in CHALLENGE_KINDS:
            by_kind.setdefault(k, 0)
            by_kind[k] += 1
        else:
            unclassified += 1
    classified = len(challenges) - unclassified
    out = {}
    for k, count in by_kind.items():
        out[k] = {
            "count": count,
            # Against ALL reviews -- comparable to the headline catch_rate, which
            # shares that denominator. This is the number the headline splits into.
            "rate_of_all_reviews": round(count / n, 4) if n else None,
            # Against CLASSIFIED challenges only -- the composition question
            # ("of the challenges we can read, how many are this kind?"). Its
            # denominator is deliberately NOT the challenge total: including
            # unclassified rows would drag every share toward zero and read as
            # though those kinds were rarer than measured.
            "share_of_classified_challenges": (
                round(count / classified, 4) if classified else None
            ),
        }
    return {
        "total_reviews": n,
        "challenges": len(challenges),
        "classified": classified,
        "unclassified": unclassified,
        "classified_share": (
            round(classified / len(challenges), 4) if challenges else None
        ),
        "by_kind": out,
        "kinds_known": list(CHALLENGE_KINDS),
        "note": (
            "Splits the headline catch_rate by WHAT was found. `wrong` is evidence "
            "FOR a standing review lane; `under-cross-linked` is a corpus hygiene "
            "gap one sweep fixes, i.e. evidence AGAINST. Read `classified_share` "
            "before by_kind: rows predating the field are `unclassified`, never "
            "guessed, so a low classified_share means this split speaks for only "
            "that fraction of the challenges."
        ),
    }


def cmd_report(args) -> int:
    rows = _ledger_rows()
    reviews = [r for r in rows if r.get("kind") == "review"]
    resolutions = {r["entry_id"]: r for r in rows if r.get("kind") == "resolution"}
    opened = next((r for r in rows if r.get("kind") == "window" and r.get("event") == "opened"), None)

    challenged = [r for r in reviews if r.get("stance") in CHALLENGE_STANCES]
    resolved = [r for r in challenged if r["entry_id"] in resolutions]
    survived = [r for r in resolved if resolutions[r["entry_id"]].get("challenge_survived") is True]

    # Outcome 3 (): ACKNOWLEDGED-BUT-NOT-APPLIED is its own state, not a
    # rounding of resolved or unresolved -- a challenge upheld with nothing yet
    # verified on the entry. Reported so it cannot silently inflate either side.
    def _verdict_of(res):
        v = res.get("entry_verdict")
        if v:
            return v
        # Rows written before this fix carry neither field. Re-DERIVE from the
        # boolean; do NOT read the verdict the old code wrote to the calibration
        # ledger, which was inverted by construction.
        return entry_verdict(
            res.get("challenge_survived") is True, res.get("artifact_change")
        )

    by_entry_verdict = {}
    for r in resolved:
        v = _verdict_of(resolutions[r["entry_id"]])
        by_entry_verdict[v] = by_entry_verdict.get(v, 0) + 1

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

    # Qualify the number rather than withhold it; see reviewer_concentration().
    # Applied ONLY when a rate was actually computed — appending a scope caveat to
    # the REFUSED text would read as two independent objections to a figure that
    # does not exist.
    concentration = reviewer_concentration(reviews)
    if catch_rate is not None and concentration["concentrated"]:
        catch_rate_note += (
            " — SCOPE WARNING: %.1f%% of rows are %s's (threshold %.0f%%, %d distinct "
            "reviewer(s)), so this is ONE REVIEWER'S catch rate wearing a lane-level "
            "label. Read reviewer_concentration.per_reviewer before encoding an "
            "adopt/drop decision on it, and say which quantity you used (guard-2193)."
            % (
                concentration["dominant_share"] * 100,
                concentration["dominant_reviewer"],
                MAX_REVIEWER_SHARE * 100,
                concentration["distinct_reviewers"],
            )
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
            "acknowledged_not_applied": by_entry_verdict.get("unknown", 0),
            "challenges_UNRESOLVED": len(challenged) - len(resolved),
        },
        "by_stance": by_stance,
        "by_entry_verdict": by_entry_verdict,
        "by_reviewer": by_reviewer,
        "reviewer_concentration": concentration,
        "catch_rate_by_challenge_kind": catch_rate_by_challenge_kind(reviews),
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
    s.add_argument(
        "--challenge-kind",
        default=None,
        choices=list(CHALLENGE_KINDS),
        help=(
            "what the challenge FOUND (g-306-466): 'wrong' = the entry is "
            "defective; 'under-cross-linked' = the entry is right but its "
            "mechanism-axis neighbours are uncited. Optional; omitted rows report "
            "as `unclassified` rather than being guessed from --basis."
        ),
    )
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
    s.add_argument(
        "--artifact-change",
        dest="artifact_change",
        choices=list(ARTIFACT_CHANGES),
        default=None,
        help=(
            "what was VERIFIED on the entry itself (amended_fields/amended_at stamped, "
            "retired, or superseded). Read the entry -- never an acknowledgment post. "
            "Omit when nothing has been verified yet: the row then scores 'unknown', "
            "the acknowledged-but-not-applied state, rather than a resolution."
        ),
    )
    s.set_defaults(func=cmd_resolve)

    s = sub.add_parser("sweep", help="auto-resolve challenges whose entry carries a verified artifact change")
    s.add_argument("--apply", action="store_true", help="append resolution rows (default: dry-run)")
    s.set_defaults(func=cmd_sweep)

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
