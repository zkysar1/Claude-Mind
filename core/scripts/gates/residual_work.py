"""Residual-work completion gate logic (Layer B of the  pattern).

Refuses `status=completed` when the goal's outcome_note names work that was
NOT done (deliberate scope narrowing) and no LIVE carrier goal exists for it.
Canonical incident: g-335-1176 (2026-08-12) completed as analyst scope with a
full implementation spec written onto the COMPLETED record — a terminal store
nothing selects — and the implementation was invisible until a fresh-eyes
review filed g-335-1186 a day later.

Layer map (4-layer enforcement pattern, capability-routing-enforcement node):
  A — Step 8.55 in aspirations-state-update/SKILL.md + guard-3601 (honor
      system; vocabulary mirrored here).
  B — THIS gate, at the completion chokepoint (cmd_update_goal / the daemon
      update-goal endpoint), same family as gates.uncommitted_work and
      gates.completion_artifact.
  D — auto-conversion: when this gate blocks, the CALLER files the suggested
      successor atomically under the live aspirations.jsonl lock (mirror of
      the defer-time Layer-D auto-Unblock, probe-before-defer.md), so the
      refusal never strands the agent at the same decision point.

Accept paths (any ONE lifts the block, mirroring Step 8.55):
  1. outcome_note cites a carrier goal id near carrier vocabulary AND that
     goal is LIVE (pending/in-progress) in the world queue or the agent
     queue — validated against queue state, never regex alone.
  1b. the goal being closed is ITSELF a gate-filed carrier (origin_signal
     "residual:<parent>") and that parent is cited within CARRIER_WINDOW
     chars of the marker — the note is explaining its own provenance, not
     naming new undone work (g-115-8775). Queue-verified like path 1, hence
     ranked with it. Windowed, NOT clause-scoped: notes here are hard-wrapped
     and _split_sentences breaks on newlines, so a "clause" is a line.
  2. --override-residual "<justification>" (daemon header
     X-Mind-Override-Residual) — audited to
     `<world_dir>/residual-work-overrides.jsonl`.
  3. outcome_note records an explicit owner decline.
  4. the residual CLAUSE is a provenance disclaimer, not undone work —
     attribution language plus a concrete artifact reference (PR / sha) in
     the same sentence that tripped the marker (g-115-6980).

PRECEDENCE IS LOAD-BEARING, AND IT USED TO BE WRONG (g-115-6254). Paths 2 and
3 were swapped, and every path returned EARLY, so an owner-decline INFERRED
FROM PROSE pre-empted an override the caller had EXPLICITLY supplied. Measured
on g-350-233: the note contained "The gate's other exit, an owner decline, is
equally inaccurate — nothing is being declined" — prose ARGUING AGAINST that
exit — which `OWNER_DECLINE_RE` matches. The close was accepted on the exact
exit its own note rejected; `--override-residual` reported success while
`override_applied` stayed None; and the audit row the refusal message promises
was NEVER WRITTEN. That is strictly worse than a block-marker false positive:
a block refuses loudly and leaves a visible carrier, whereas an accept-path
false positive lets the close through AND drops the audit trail, so nothing
anywhere records that a gate was bypassed. An EXPLICIT signal must therefore
always outrank an INFERRED one, and an override that was passed is ALWAYS
recorded and ALWAYS audited whichever path would otherwise have applied.

The marker list is deliberately CONSERVATIVE (goal text, g-115-6099): a
false positive costs one educational refusal with a working escape named in
the message; a false negative costs stranded work nothing re-surfaces.

Public API:
    evaluate(goal_id, outcome_note, override, items, other_items,
             world_dir, agent_name, goal_priority, goal_category) -> dict
    find_existing_successor(items, original_goal_id, other_items) -> dict|None
    build_successor_goal(original_goal_id, gate_result, new_goal_id) -> dict

`items` is the queue being WRITTEN (the --source target); `other_items` is
THE OTHER QUEUE — world when the target is agent, agent when the target is
world. The parameter was called `agent_items` until g-115-6254 and BOTH
callers populated it unconditionally from the AGENT queue, so on a
`--source agent` close the two arguments were the SAME queue and the world
queue was never loaded at all. Every world carrier then read `live: false,
status: null` — indistinguishable from a genuinely dead carrier — and the
gate auto-filed a duplicate successor for work that was already owned
(measured on g-001-80: three world-queue carriers, all `pending`, all
reported dead). The name is the fix as much as the population is: the old
one read like "the other queue" while being nothing of the kind, which is
why two independent callers made the identical mistake.

Output dict shape (evaluate):
    {
      "would_block": bool,
      "matched_markers": [str, ...],          # marker names, first-hit order
      "residual_clause": str | None,          # sentence behind the suggestion
      "carrier_refs_found": [{"goal_id": str, "live": bool, "status": str|None}],
      "own_provenance_found": bool,           # accept path 1b (marker-windowed)
      "owner_decline_found": bool,
      "provenance_found": bool,               # accept path 4 (clause-scoped)
      "successor_title": str | None,          # Layer-D suggestion
      "successor_description": str | None,
      "successor_priority": str,
      "successor_category": str,
      "goal_id": str,
      "override_applied": str | None,
      "skipped_reason": str | None,           # set when the scan never ran
    }

Daemon safety:
    - Reads no environment variables; every input is passed in.
    - No subprocess calls; pure regex + in-memory queue scans. The only
      side effect is the fail-open override-ledger append (same contract as
      gates.uncommitted_work / gates.completion_artifact).
"""
from __future__ import annotations

import datetime as dt
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from _fileops import locked_append_jsonl  # type: ignore

# Statuses under which a cited carrier (or a dedup-found successor) counts as
# LIVE. Same set the defer-time Layer-D dedup uses for Unblocks.
ACTIVE_STATUSES = ("pending", "in-progress")

# Conservative residual markers — Step 8.55 vocabulary, tightened to phrases
# that rarely appear in a fully-executed goal's outcome. Bare "follow-up" /
# "remainder" / "successor" mostly occur while NAMING the filed carrier, in
# which case accept path 1 lifts the block.
#
# THE LOOKBEHINDS BELOW BELONG TO `follow_up` ALONE. This comment used to say
# "the negative lookbehinds strip the common benign negations" without naming a
# marker, which reads as a property of all three — and `remainder`/`successor`
# had NOTHING. That sentence is how the gap survived review ().
# Those two are now guarded differently and for a different reason, by
# `_noun_marker_survives` below: they are bare common NOUNS, not phrases, so
# negation was never their main defect (guard-1923, guard-1024).
RESIDUAL_MARKERS: List[Tuple[str, re.Pattern]] = [
    ("no_code_written",
     re.compile(r"\bno\s+(?:product\s+|new\s+)?code\s+(?:was\s+)?written\b",
                re.IGNORECASE)),
    ("spec_only",
     re.compile(r"\b(?:spec(?:ification)?|criteria|spec\s*/\s*criteria)\s+only\b",
                re.IGNORECASE)),
    ("drafted_not_sent",
     re.compile(r"\bdrafted,?\s+(?:but\s+)?not\s+sent\b", re.IGNORECASE)),
    ("out_of_scope_this_pass",
     re.compile(r"\bout\s+of\s+scope\s+(?:for\s+)?this\s+pass\b",
                re.IGNORECASE)),
    ("deferred_to",
     re.compile(r"\bdeferred\s+to\b", re.IGNORECASE)),
    ("follow_up",
     re.compile(r"(?<!\bno\s)(?<!without\s)\bfollow-up\b", re.IGNORECASE)),
    ("remainder",
     re.compile(r"\bremainder\b", re.IGNORECASE)),
    ("successor",
     re.compile(r"\bsuccessor\b", re.IGNORECASE)),
]

# --- Bare-noun marker guard () -----------------------------------
# `remainder` and `successor` are bare common English nouns. Unlike every other
# entry above — all of which are PHRASES that assert incompleteness ("no code
# written", "deferred to") — the noun alone asserts nothing, so it matches
# ordinary prose about an unrelated subject. Measured over the whole override
# ledger (37 rows, 2026-08-30): of the 16 rows these two markers produced, ~7
# were the noun in a non-work sense and ~4 were an author saying they
# DELIBERATELY DID NOT file one. guard-1923 prescribes the remedy for a
# polysemous token: do not drop it, NARROW it to the referent form.
#
# TWO DISTINCT DEFECTS, and conflating them is why one guard cannot fix both:
#
#   class A — the noun with a non-work referent: "its successor
#     LogCustomEvent", "the remainder of that function", "carried into the
#     successor comment". There is NO negation anywhere in these clauses, so a
#     negation guard is structurally incapable of touching them. They need an
#     ANCHOR to work vocabulary (_WORK_CONTEXT_RE).
#
#   class B — a filing DECLINE: "Did not file a successor", "No successor goal
#     filed", "Filing a successor would re-open finished work". These need
#     negation/subjunctive detection (_FILING_DECLINE_RE).
#
# THE NEGATION MUST SCOPE TO THE FILING VERB, NEVER TO THE WORK VERB. This is
# the trap that makes a blanket negation guard wrong: "The remainder was not
# attempted" is a NEGATED clause that asserts residual work EXISTS, and it is a
# standing positive control in the gate's own tests. So _FILING_DECLINE_RE
# matches negation adjacent to file/create/open, and deliberately does not fire
# on "not attempted".
#
# SCOPE HONESTLY STATED: this narrows, it does not eliminate. Two class-A rows
# in the ledger survive by construction because their clauses genuinely contain
# work vocabulary — "were carried into the successor comment" (carrier verb,
# code-comment referent) and prose ABOUT successor goals as a concept. Per
# guard-1892 the residue is a WORDING problem that no match-time parsing
# repairs; the remaining lever is the marker vocabulary, not more machinery.
_NOUN_MARKERS = ("remainder", "successor")

# A DECLINE to file a carrier, in the three shapes the ledger actually shows:
# negation before the noun, negation of the filing verb after it, and the
# subjunctive "filing one would ..." form.
_FILING_DECLINE_RE = re.compile(
    r"\b(?:no|not|never)\b[^.;\n]{0,40}?\b(?:successor|remainder)\b"
    r"|\b(?:successor|remainder)\b[^.;\n]{0,40}?"
    r"\b(?:not|never)\s+(?:been\s+)?(?:filed|created|opened|raised)\b"
    r"|\b(?:filing|file)\b[^.;\n]{0,40}?\bwould\b",
    re.IGNORECASE,
)

# Vocabulary that makes the noun refer to WORK rather than to an API, a code
# comment, a data field, or a lane label. Deliberately narrow: an unanchored
# noun is the defect being fixed, so absence of this vocabulary suppresses.
#
# `\b` IS NOT AN IDENTIFIER BOUNDARY, and that gap cost a spurious HIGH goal
# (). `-`, `.` and `/` are all non-word characters, so `\bgoal\b`
# matches the HEAD of `goal-eligible`, `goal-selector.py`, `world/pending/x`.
# An identifier is machine text, not the author asserting that work remains,
# so anchoring on one lets any clause quoting a command read as work context.
# MEASURED (, 2026-09-05): its note says "`skill` is argparse
# REMAINDER ... `goal-eligible --role reducer`" — a sentence announcing NEW
# TEST COVERAGE. `remainder` fired three times; `_noun_marker_survives`
# correctly suppressed two, and the third survived on `goal` inside
# `goal-eligible`. The gate refused the close and Layer-D auto-filed
#  HIGH, which sat at rank 3 of 1,881 candidates until it was closed
# by hand as moot.
#
# THE TRAILING GUARD EXCLUDES ONLY THE IDENTIFIER **HEAD** — deliberately, and
# the asymmetry is the whole design. A leading guard would also drop `filed` in
# "re-filed", `deferred` in "auto-deferred", `carried` in "hand-carried": those
# are ordinary English work prose, and losing them costs a FALSE NEGATIVE,
# which this module ranks as strictly worse than a false positive (see the
# marker-list note above). guard-1901 governs the direction — this predicate is
# a REQUIREMENT for blocking, so narrowing it weakens the gate — and its
# mandated question ("what NEW input now passes?") has an enumerated answer: a
# clause whose ONLY work word is an identifier head, e.g. "The remainder is in
# the deferred-work lane." Measured over 438 real outcome_notes (guard-2201,
# one snapshot, both patterns): exactly ONE verdict changes,  itself.
# Zero collateral. Controls live in test_residual_work_gate.py.
_WORK_CONTEXT_RE = re.compile(
    r"\b(?:goal|goals|task|tasks|carrier|file|filed|files|filing|carry|"
    r"carried|carries|attempted|unfinished|undone|remains|remaining|"
    r"outstanding|pending|deferred|executed|implemented|addressed|"
    # declin* is work context, not a decline-to-file: "Remainder declined by
    # the owner" asserts a remainder EXISTS and routes it to accept path 3.
    # Safe against class B because _FILING_DECLINE_RE runs FIRST and has
    # already returned on "did not file a successor" and friends.
    r"declined|declines|decline)\b(?!-|[./]\w)",
    re.IGNORECASE,
)


GOAL_ID_RE = re.compile(r"\bg-\d{1,4}-\d{1,4}\b")

# Vocabulary that marks a nearby goal id as a CARRIER citation rather than an
# incidental cross-reference. Window below is ±120 chars — wider than the
# dedup proximity (80) because citations often carry a parenthetical between
# the verb and the id.
CARRIER_VOCAB_RE = re.compile(
    r"\b(?:carried|carrier|carries|filed|files|successor|follow-up|"
    r"remainder|deferred|tracked|routed|owns)\b",
    re.IGNORECASE,
)
CARRIER_WINDOW = 120

OWNER_DECLINE_RE = re.compile(
    r"(?:\bowner\b.{0,40}?\bdeclin\w+|\bdeclined\s+by\s+(?:the\s+)?"
    r"(?:owner|user)\b|\buser\s+declined\b)",
    re.IGNORECASE | re.DOTALL,
)

# --- Accept path 4: PROVENANCE () ---------------------------------
# A residual marker and an ATTRIBUTION DISCLAIMER are lexically identical:
# "No code was written" reads the same whether it means "the work is undone"
# or "someone else already did it". The canonical clause () is
# BOTH at once — it trips `no_code_written` and, in the same breath, names
# the artifact that refutes it.
#
# Two conjuncts are required, and each covers the other's failure mode:
#   ATTRIBUTION_RE  — credits the work to somewhere else. Action-shaped
#                     phrases ONLY: guard-1892 forbids matcher text that is
#                     the NAME of a rule/policy, because such a phrase fires
#                     on the rule's READERSHIP rather than on the work.
#   ARTIFACT_REF_RE — a concrete, checkable referent (PR number or commit
#                     sha). Attribution prose alone is unfalsifiable; a
#                     named artifact is something a reader can go check.
#
# PRESENCE IS NOT REACHABILITY (guard-3398, guard-4556). The right proof is
# the remote — `gh pr view <N>` reporting MERGED, or `git merge-base
# --is-ancestor <sha> origin/main`. This gate CANNOT run either: the module
# contract above is "reads no environment variables; no subprocess calls",
# and it executes inside the daemon's write lock. So this path is INFERRED,
# is ranked LAST with the other inferred signal, and is reported separately
# as `provenance_found` so an auditor can tell which path lifted the block.
# Do NOT promote it above the queue-verified carrier or the explicit
# override — an inferred signal must never pre-empt an explicit one, which
# is the precedence bug  already paid for once.
ATTRIBUTION_RE = re.compile(
    r"\b(?:landed\s+in|shipped\s+in|merged\s+(?:in|as|at)|"
    r"was\s+already\s+(?:shipped|done|merged|implemented|fixed|landed)|"
    r"already\s+(?:shipped|done|merged|implemented|fixed|landed)|"
    r"(?:work|fix|change)\s+(?:was\s+)?(?:done|written|implemented)\s+by|"
    r"implemented\s+in)\b",
    re.IGNORECASE,
)

# Hex-sha shape with at least one digit — the digit requirement keeps
# ordinary a-f words ("defaced", "deadbeef") from reading as commits.
ARTIFACT_REF_RE = re.compile(
    r"(?:\bPR\s*#?\d+\b|\bpull/\d+\b|\b(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b)",
    re.IGNORECASE,
)

LEDGER_BASENAME = "residual-work-overrides.jsonl"


def _split_sentences(text: str) -> List[str]:
    """Cheap sentence split — good enough to lift a residual clause for the
    successor title. Newlines and .!? all terminate."""
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [p.strip() for p in parts if p.strip()]


def _residual_clause(text: str, first_match_start: int) -> str:
    """The sentence containing the first matched marker."""
    pos = 0
    for sent in _split_sentences(text):
        idx = text.find(sent, pos)
        if idx < 0:
            idx = pos
        if idx <= first_match_start < idx + len(sent):
            return sent
        pos = idx + len(sent)
    # Fallback: a window around the match.
    lo = max(0, first_match_start - 60)
    return text[lo:first_match_start + 90].strip()


def _noun_marker_survives(text: str, start: int) -> bool:
    """True when a bare-noun marker hit is a REAL claim that work remains.

    Scoped to the SENTENCE containing the hit, not the whole note: a long
    outcome_note routinely discusses successors and remainders in several
    unrelated senses, and widening the window would let any one work-shaped
    sentence license every other match in the note.
    """
    clause = _residual_clause(text, start)
    if _FILING_DECLINE_RE.search(clause):
        return False
    return bool(_WORK_CONTEXT_RE.search(clause))


def _extract_carrier_candidates(text: str) -> List[str]:
    """Goal ids within CARRIER_WINDOW chars of carrier vocabulary."""
    vocab_spans = [m.start() for m in CARRIER_VOCAB_RE.finditer(text)]
    out: List[str] = []
    for m in GOAL_ID_RE.finditer(text):
        if any(abs(m.start() - v) <= CARRIER_WINDOW for v in vocab_spans):
            if m.group(0) not in out:
                out.append(m.group(0))
    return out


def _lookup_goal_record(gid: str,
                        items: Optional[List[Dict[str, Any]]],
                        other_items: Optional[List[Dict[str, Any]]]
                        ) -> Optional[Dict[str, Any]]:
    """The goal record for `gid` across the target queue and THE OTHER queue,
    or None when not found. First hit wins (ids are globally unique by
    convention). Both arguments must be genuinely different queues — see the
    module docstring; passing the same queue twice silently halves the search
    space and reports live carriers as dead."""
    for source in (items, other_items):
        if not source:
            continue
        for asp in source:
            for g in asp.get("goals", []) or []:
                if g.get("id") == gid:
                    return g
    return None


def _lookup_goal_status(gid: str,
                        items: Optional[List[Dict[str, Any]]],
                        other_items: Optional[List[Dict[str, Any]]]
                        ) -> Optional[str]:
    """Status of goal `gid`, or None when not found. Thin wrapper over
    `_lookup_goal_record` so the two-queue scan has ONE implementation."""
    rec = _lookup_goal_record(gid, items, other_items)
    return rec.get("status") if rec else None


def _append_override_ledger(world_dir: Optional[Path], record: Dict[str, Any]
                            ) -> None:
    """Fail-open audit append — same contract as the sibling gates."""
    if world_dir is None:
        return
    try:
        locked_append_jsonl(str(Path(world_dir) / LEDGER_BASENAME), record)
    except Exception as exc:  # noqa: BLE001 — audit must never block the gate
        print(f"[residual-work-gate] override ledger append failed: {exc}",
              file=sys.stderr)


def evaluate(goal_id: str,
             outcome_note: str,
             override: Optional[str],
             items: Optional[List[Dict[str, Any]]],
             other_items: Optional[List[Dict[str, Any]]],
             world_dir: Optional[Path],
             agent_name: str = "",
             goal_priority: Optional[str] = None,
             goal_category: Optional[str] = None) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "would_block": False,
        "matched_markers": [],
        "residual_clause": None,
        "carrier_refs_found": [],
        "own_provenance_found": False,
        "owner_decline_found": False,
        "provenance_found": False,
        "successor_title": None,
        "successor_description": None,
        "successor_priority": (goal_priority if goal_priority in
                               ("HIGH", "MEDIUM", "LOW") else "MEDIUM"),
        "successor_category": goal_category or "framework-maintenance",
        "goal_id": goal_id,
        "override_applied": None,
        "skipped_reason": None,
    }

    text = (outcome_note or "").strip()
    if not text:
        # Layer A owns narrative discipline; an empty note has nothing to
        # scan and blocking on absence would refuse every legacy close.
        result["skipped_reason"] = "empty_outcome_note"
        return result

    first_start: Optional[int] = None
    for name, rx in RESIDUAL_MARKERS:
        # Bare-noun markers scan EVERY occurrence and keep the first one that
        # survives the guard. `search` would stop at the first hit, so a note
        # whose opening prose mentions "its successor" in an API sense would
        # mask a genuine residual later in the same note.
        if name in _NOUN_MARKERS:
            m = next((h for h in rx.finditer(text)
                      if _noun_marker_survives(text, h.start())), None)
        else:
            m = rx.search(text)
        if m:
            result["matched_markers"].append(name)
            if first_start is None or m.start() < first_start:
                first_start = m.start()
    if not result["matched_markers"]:
        return result

    # Accept signals are computed WITHOUT early return (), then
    # ranked by how much we trust them. Early-returning is what let a signal
    # INFERRED from prose pre-empt one the caller stated EXPLICITLY — see the
    # PRECEDENCE note in the module docstring for the measured incident.
    live_found = False
    for gid in _extract_carrier_candidates(text):
        if gid == goal_id:
            continue  # self-citation is not a carrier
        status = _lookup_goal_status(gid, items, other_items)
        live = status in ACTIVE_STATUSES
        result["carrier_refs_found"].append(
            {"goal_id": gid, "live": live, "status": status})
        if live:
            live_found = True
    result["owner_decline_found"] = bool(OWNER_DECLINE_RE.search(text))

    # The clause behind the first marker — computed ONCE here because three
    # separate consumers need it below (the override audit row, the
    # provenance scan, and the Layer-D successor title).
    clause = _residual_clause(text, first_start or 0)

    # Provenance is scanned against the CLAUSE, never the whole note. A long
    # outcome_note routinely cites some PR and says "already done" about a
    # DIFFERENT matter than the one that tripped the marker; scanning the
    # whole text would let any unrelated citation suppress a real residual.
    # Requiring both conjuncts inside the one sentence that tripped the
    # marker is what keeps this from becoming a blanket bypass.
    result["provenance_found"] = bool(
        ATTRIBUTION_RE.search(clause) and ARTIFACT_REF_RE.search(clause))

    # --- Accept path 1b: OWN-PROVENANCE citation () ---------------
    # A goal this gate itself auto-filed carries `origin_signal`
    # "residual:<parent>". When such a carrier is executed, the honest
    # outcome_note explains where it came from — and that explanation must
    # quote the parent's residual language to be intelligible. The marker then
    # fires on the QUOTATION, and the gate files a fresh successor for the
    # very work the note is reporting as DONE.
    #
    # MEASURED (, 2026-09-03): closing the carrier for 
    # filed phantom  (origin_signal residual:, since
    # skipped by hand), off the sentence "the previous unit ( ...)
    # closed while its outcome_note named the merge as follow-up work with no
    # live carrier". This is guard-2096's shape exactly — a text detector run
    # over a corpus that DOCUMENTS ITS OWN FINDINGS re-flags every correction
    # it causes — and here it does not merely warn, it WRITES A GOAL, so the
    # queue grows by one phantom per honest post-mortem.
    #
    # WHY THIS SIGNAL AND NOT "the cited carrier is completed": that broader
    # rule was tried first and is WRONG. It breaks
    # test_completed_carrier_does_not_lift, which pins a genuinely different
    # shape — a FORWARD claim ("residual carried by g-X") whose carrier has
    # completed is ambiguous about ORDERING, because g-X may have completed
    # before this residual ever arose. The shape fixed here is a BACKWARD
    # provenance reference, and `origin_signal` states the parent relationship
    # as stored queue fact rather than inferring it from prose.
    #
    # SCOPED TO A CHARACTER WINDOW AROUND THE MARKER — deliberately NOT to
    # `clause`, and this is the half that was measured wrong first.
    # `_split_sentences` terminates on `\n+` as well as on `.!?`, so in this
    # corpus — where every outcome_note is hard-wrapped at ~78 columns — a
    # "clause" is a LINE, not a sentence. Replaying the real 3,671-char
    #  note proved it: the parent id sits on the physical line
    # BEFORE the marker, so the clause was the bare fragment "outcome_note
    # named the merge as follow-up work with no live carrier." and the id fell
    # outside it. The first cut of this path scoped to `clause`, passed its own
    # tests, and did nothing at all to the input it was written for — guard-920
    # exactly (a fixture that reproduced the prose but not the line breaks).
    #
    # CARRIER_WINDOW is REUSED rather than duplicated under a new name: it
    # already means "a goal id this far from the anchor is still plausibly
    # about it", which is the identical question here, and there is no measured
    # reason for the two to differ. Measured distance in the real note: -103.
    #
    # The carrier-VOCABULARY conjunct that _extract_carrier_candidates applies
    # is deliberately NOT reused here, because it tests the wrong relationship.
    # That helper asks "is this id named as the thing CARRYING the residual";
    # path 1b asks "is this id my own recorded PARENT". A provenance sentence
    # need carry no carrier vocabulary at all ("filed by the residual gate off
    # g-X's outcome_note"); in the  note the word "carrier" happens to
    # be nearby only because that sentence is about carriers. Keeping the
    # conjunct would make the path fire on an accident of wording.
    #
    # LIMIT, STATED RATHER THAN PAPERED OVER: a note that BOTH explains
    # provenance AND asserts genuinely new undone work within the same ~240
    # characters will be suppressed. That is guard-1892's residue — a WORDING
    # problem no match-time parsing repairs — and narrowing further would mean
    # parsing unbounded prose, which is the machinery this module already
    # refuses to add. The exposure is bounded by the window and, far more
    # tightly, by the requirement that the cited id be THIS goal's own recorded
    # parent — stored queue state, not an inference from prose.
    own_parent = ""
    own_record = _lookup_goal_record(goal_id, items, other_items)
    if own_record:
        signal = str(own_record.get("origin_signal") or "")
        if signal.startswith("residual:"):
            own_parent = signal.split(":", 1)[1].strip()
    if own_parent:
        anchor = first_start or 0
        window = text[max(0, anchor - CARRIER_WINDOW):anchor + CARRIER_WINDOW]
        result["own_provenance_found"] = bool(
            re.search(r"\b" + re.escape(own_parent) + r"\b", window))

    # Accept path 2 — the audited override. Ranked ABOVE the owner-decline
    # inference and evaluated even when a live carrier was found, because an
    # override that was PASSED must always be recorded and always audited:
    # the silent-bypass failure was `override_applied` reading None with an
    # empty ledger while the caller reported success.
    if override:
        result["residual_clause"] = clause
        result["override_applied"] = override
        _append_override_ledger(world_dir, {
            "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
            "goal_id": goal_id,
            "agent": agent_name or None,
            "matched_markers": result["matched_markers"],
            "residual_clause": clause[:300],
            "justification": override,
        })
        return result

    # Accept path 1: live carrier citation (factual, verified against queue
    # state — needs no audit row because nothing was bypassed).
    if live_found:
        return result

    # Accept path 1b: own-provenance citation — VERIFIED (origin_signal is
    # stored queue state), so it is ranked here with its live sibling rather
    # than below with the two prose inferences. Reported as its own field so
    # an auditor can tell which path lifted the block (`provenance_found`
    # precedent).
    if result["own_provenance_found"]:
        return result

    # Accept path 3: owner decline, INFERRED from prose. Ranked below the
    # verifiable paths because it cannot be checked against anything.
    if result["owner_decline_found"]:
        return result

    # Accept path 4: PROVENANCE, INFERRED from prose (). The clause
    # credits the work to a named artifact instead of naming work still to do.
    #
    # The gate's original cost model said a false positive is cheap — "one
    # educational refusal with a working escape named in the message". That
    # was MEASURED FALSE on 2026-08-20: closing  filed TWO spurious
    # HIGH successors (the gate auto-files on every attempt, and an override
    # permits the close without suppressing the Layer-D file), and one of them
    # ranked #1 of 1325 candidates and consumed a full iteration of a DIFFERENT
    # agent on a DIFFERENT box before being recognised as spurious.
    #
    # It is also selective against the population the framework is actively
    # draining: completed-not-closed goals can ONLY be closed honestly by
    # saying the work was already done by someone else, so the drain lanes
    # (precheck 0.5g.6/0.5g.7) manufacture exactly the sentence this marker
    # punishes. Left unfixed, the cheapest way past the gate is to DELETE the
    # attribution — evidence-laundering that trains false credit.
    if result["provenance_found"]:
        return result

    result["residual_clause"] = clause

    # Block + Layer-D suggestion. Title from the residual clause (the
    # defer-gate precedent: the title tells the executor what to DO).
    short = clause if len(clause) <= 90 else clause[:87].rstrip() + "..."
    result["would_block"] = True
    result["successor_title"] = f"Residual: {short} (from {goal_id})"
    result["successor_description"] = (
        f"Residual-work carrier auto-filed by the residual-work gate "
        f"(g-115-6099 Layer B/D): completing {goal_id} named undone work "
        f"with no live carrier. Residual clause: \"{clause[:400]}\". Read "
        f"{goal_id}'s outcome_note for full context, and copy any "
        f"verification criteria or spec it carries INTO this goal's "
        f"verification field before executing — criteria on a completed "
        f"record are invisible to every selector (g-335-1186 reference "
        f"shape)."
    )
    return result


def find_existing_successor(items: Optional[List[Dict[str, Any]]],
                            original_goal_id: str,
                            other_items: Optional[List[Dict[str, Any]]] = None
                            ) -> Optional[Dict[str, Any]]:
    """Cross-queue dedup for Layer-D filing — 3 OR-ed strategies mirroring
    the defer-gate's `_find_existing_unblock_for` (rb-574):
      (a) origin_signal == 'residual:{original_goal_id}'
      (b) title starts 'Residual:' AND contains the original goal id
      (c) description holds 'residual' within 80 chars of the original id
    Only ACTIVE_STATUSES count — a resolved/skipped successor never blocks
    re-filing."""
    expected_origin = f"residual:{original_goal_id}"
    gid_lo = original_goal_id.lower()

    def _scan(source, label):
        if not source:
            return None
        for asp in source:
            for g in asp.get("goals", []) or []:
                if g.get("status") not in ACTIVE_STATUSES:
                    continue
                if g.get("origin_signal") == expected_origin:
                    return {**g, "_aspiration_id": asp.get("id", ""),
                            "_source": label,
                            "_match_strategy": "origin_signal"}
                title = (g.get("title", "") or "")
                if (title.startswith("Residual:")
                        and gid_lo in title.lower()):
                    return {**g, "_aspiration_id": asp.get("id", ""),
                            "_source": label,
                            "_match_strategy": "title_prefix"}
                desc = (g.get("description", "") or "").lower()
                r_idx = desc.find("residual")
                g_idx = desc.find(gid_lo)
                if r_idx >= 0 and g_idx >= 0 and abs(r_idx - g_idx) <= 80:
                    return {**g, "_aspiration_id": asp.get("id", ""),
                            "_source": label,
                            "_match_strategy": "description_proximity"}
        return None

    # Labels are "target"/"other", NOT "world"/"agent": which concrete queue
    # each argument holds depends on --source, and calling the second one
    # "agent" unconditionally is precisely the misreading that produced
    # . The caller knows its own --source and can name it.
    return _scan(items, "target") or _scan(other_items, "other")


def build_successor_goal(original_goal_id: str,
                         gate_result: Dict[str, Any],
                         new_goal_id: str) -> Dict[str, Any]:
    """The successor record for Layer-D filing. The CALLER allocates
    `new_goal_id` under its chosen aspiration, appends, and persists under
    the lock it already holds — this function never writes (rb-403
    single-writer)."""
    return {
        "id": new_goal_id,
        "title": (gate_result.get("successor_title")
                  or f"Residual: carrier for {original_goal_id}"),
        "description": (gate_result.get("successor_description")
                        or f"Residual work from {original_goal_id} "
                           f"(residual-work gate)."),
        "type": "idea",
        "category": gate_result.get("successor_category",
                                    "framework-maintenance"),
        "priority": gate_result.get("successor_priority", "MEDIUM"),
        "participants": ["agent"],
        "status": "pending",
        "blocked_by": [],
        "origin_signal": f"residual:{original_goal_id}",
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        # alloc_nonce (): this lane appends directly and never
        # passes through add_goal()'s setdefault — mint our own.
        "alloc_nonce": uuid.uuid4().hex,
        "tags": ["residual", "residual-gate-routed"],
        "verification": {"outcomes": [], "checks": [], "preconditions": []},
    }
