#!/usr/bin/env python3
"""Stranded Claim Sweep — release in-progress claims orphaned by autocompact.

Canonical incident (bravo session-35, g-115-23, 2026-05-21): autocompact
fires AFTER aspirations-claim.sh succeeds but BEFORE Phase 4 execution
starts. The post-compaction session inherits a stranded in-progress claim
(status=in-progress, claimed_by=self, team-state.in_flight set) with no
execution-diary entry for that goal in the current session — the next
iteration's selector sees the goal as "owned" and skips it; the goal sits
frozen until /felt-sense-checkin Phase 2 happens to catch it on its
75-goal cadence.

Sweep logic per g-115-1044:

  for each goal where claimed_by == MIND_AGENT AND status == "in-progress":
      if execution-diary has an entry for this goal_id AFTER claimed_at:
          NOT stranded (work is in progress)
      elif (now - claimed_at) < stale_threshold_minutes (default 5):
          NOT stranded (fresh claim, race-condition window)
      elif claimed_by_sid is set AND != this process's MIND_SID
           AND age < foreign_sid_grace_minutes (default 120):
          NOT stranded (g-115-4004 — another live INSTANCE of this same
          agent holds it; see the foreign-session guard below)
      else:
          STRANDED

Foreign-session guard (g-115-4004): every other test above reads the execution
diary under agents/<agent>/session/ to judge a SHARED subject (a claim in
aspirations.jsonl).

CORRECTION 2026-08-03 (g-306-132-c) — this paragraph used to call that diary
BOX-LOCAL, "excluded by .gitignore's **/session/ and kept machine-local by
own-cloud". THE SECOND HALF IS FALSE, and it is load-bearing for every guard
below. .gitignore does exclude it from git, but own-cloud does NOT keep it
machine-local: session-manifest.yaml gives execution-diary.jsonl
`sync_tier: continuity`, and OwnCloudBackend._machine_local() returns False for
it (measured directly) — S3 is authoritative and the local tree is a
read-through cache. Contrast the sibling `execute-in-flight`, which IS
`sync_tier: machine_local`, chosen deliberately because "a goal mid-execution on
one machine is NOT in flight on another". The diary is keyed PER-AGENT and its
entries carry no session id at all, so once this box pulls, a peer INSTANCE's
entries are indistinguishable from this box's own.

What survives the correction: the guard's SHAPE is still right — local-ish
evidence, global subject — and a peer's entries are absent until a pull, so the
"reads as abandoned" failure is real, just not guaranteed. What does NOT survive
is any inference of the form "diary activity here proves THIS box did the work".
Comparing
claimed_by_sid (stamped by aspirations-claim.sh, g-115-3176) against this
process's MIND_SID makes "stranded" mean "no live INSTANCE holds this"
instead of "no local diary entry exists". The grace window keeps a dead
instance from freezing a goal forever (this module prefers a recoverable flip
over a permanent freeze — see _read_all_in_flight_goal_ids).

  with --apply:
      - POST /v1/aspirations/release           # strips claimed_by + claimed_at
      - POST /v1/aspirations/update-goal       # field=status, value="pending"
      - POST /v1/team-state/clear-in-flight    # if_goal=<id> — CAS, see below
      - POST /v1/board/post                    # WORLD source only — guard-1610,
                                               # see _announce_release. Added
                                               # : without it the
                                               # release was silent by
                                               # construction and the goal's
                                               # only board trace stayed an
                                               # unpaired --type claim post.

Output (JSON to stdout): {"scanned": K, "scanned_no_claim": J,
"stranded": [...], "released": N, "kept": M, "dry_run": bool,
"agent": "<name>", "now": "<iso>"}.

Second shape (g-115-1691): the claimed_by==MIND_AGENT query above is
structurally blind to agent-source goals that went in-progress WITHOUT a
claim. Agent-source goals skip aspirations-claim.sh (loop digest Phase 4
claims only IF source==world), and that wrapper is the sole writer of
claimed_by — so a stranded agent-source in-progress goal carries
claimed_by=unset and never matches the query. The sweep ALSO scans the
agent-source active aggregate for status==in-progress goals with no
claimed_by, using last_modified as the stale-age basis (no claimed_at
exists — claimed_by/claimed_at are written together by the claim wrapper).
For a genuinely stranded goal (no writes after it went in-progress)
last_modified == the in-progress-transition moment; the diary check carries
the primary detection weight regardless. A no-claim stranded goal has
nothing to release (no claim) and no team-state in_flight to clear
(in_flight is written at claim time): the operative action is the
status->pending flip that returns it to the selectable pool.

Third shape (g-115-2417): the no-claim scan originally covered ONLY the
agent source, on the premise "world goals always claim". Falsified
2026-07-16: felt-sense Phase 2 found 3 WORLD goals (g-115-2156, g-115-2243,
g-350-14) stuck status=in-progress with claimed_by=null and no live
activity — frozen for selection (the selector skips in-progress) yet
invisible to both scans above. Producing mechanisms: a release path strips
claimed_by/claimed_at without resetting status, or a session dies between
the two writes. The no-claim scan therefore runs for BOTH sources. The
world queue is shared, so the world pass carries one extra guard the
agent pass does not need: if ANY agent's team-state in_flight names the
goal, it is kept (a peer is live on it even though the claim record is
missing — flip would yank a goal mid-execution). Cross-box TZ skew
(g-115-2418: peer UTC stamps read up to 4h in the FUTURE on an EDT box)
makes age negative for fresh peer writes — negative age < stale threshold
lands on the KEEP side, the safe direction; a same-TZ box's sweep flips
the genuinely stale ones.

Exit codes:
  0 — sweep ran (dry-run or apply). Output is JSON.
  1 — fatal error (missing MIND_AGENT, daemon unreachable, etc.). Diagnostic
      on stderr.

Invoked by:
  - .claude/skills/aspirations/SKILL.md Phase -0.5c (after compact-restore-slots.sh)
  - User / debugging via the .sh wrapper

Implementation note: framework Python scripts on Windows cannot subprocess
to bash wrappers (rb-225/rb-247 — every bash invocation form fails or
hangs). The canonical Python -> daemon client is `_rt.py`.

CORRECTION 2026-08-03 (g-306-167) — this note used to continue "team-state
has no daemon endpoint so we invoke its CLI via sys.executable". THE PREMISE
WAS FALSE and had been for some time: `mind_api/src/world/team_state_write.py`
registers POST /v1/team-state/{update,in-flight,clear-in-flight,init,
retire-agent}, and this module was already reading team-state through _rt two
functions away. rb-225/rb-247 remains sound — it forbids Python -> BASH — but
it answers "how do I subprocess safely?" when the prior question, "do I need a
subprocess at all?", was never asked. guard-555 puts a typed _rt client FIRST
and names calling the underlying .py CLI directly as the forbidden fallback
(it writes behind the live daemon). So the team-state clear is now an _rt call.

What survives: `_has_pending_background_work` STILL invokes pending-agents.py /
background-jobs.py via sys.executable, and that is correct — neither registers
a daemon route (verified: zero matches in mind_api/src routes). The invocation
rule was never wrong; the endpoint-does-not-exist claim was, and only for
team-state.

Cross-references:
  - g-115-1044 — originating Idea goal
  - rb-428 — sentinel-lifecycle pattern (related)
  - .claude/rules/stop-hook-compliance.md — claim/in_flight invariants
  - core/scripts/_rt.py — canonical Python -> daemon client
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import _rt  # canonical Python -> daemon client  # noqa: E402
from _paths import agent_dir  # type: ignore  # noqa: E402

DEFAULT_STALE_MINUTES = 5
# Grace window for a claim held by a DIFFERENT session of this same agent
# (). 120m = 24x DEFAULT_STALE_MINUTES: long enough to cover a deep
# multi-hour goal on a peer box whose diary this box cannot read, short enough
# that a genuinely dead instance cannot freeze a shared world goal for a day.
DEFAULT_FOREIGN_SID_GRACE_MINUTES = 120


def _now_iso() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat()


def _parse_iso(s: str) -> Optional[dt.datetime]:
    try:
        return dt.datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _agent_name() -> str:
    name = (os.environ.get("MIND_AGENT") or "").strip()
    if not name:
        raise SystemExit(
            "MIND_AGENT not set — sweep cannot run without an agent binding"
        )
    return name


# Statuses a claim can be stranded IN. `in-progress` is the classic autocompact
# shape this module was written for. `pending` was added by  and is NOT
# a widening for its own sake — it closes a gap that had NO other authority:
#
#   the selector (goal-selector.py sibling_body predicate) deliberately SKIPS a
#   goal claimed by the same mind from a different Body, and its own comment
#   names this sweep as "the authority for actually releasing a dead Body's
#   claim". But both of this module's shapes filtered status == in-progress —
#   the claimed query here, and the no-claimed_by aggregate scan below — so a
#   pending+claimed goal matched NEITHER. The designated authority was
#   structurally unable to act on the case the delegator delegated to it.
#
# Measured specimen: , HIGH, user_directive, deadline 2026-08-07,
# claimed by a dead alpha ASSISTANT session, status=pending. goal-selector
# returned 804 candidates and it was absent from all of them; it would have
# stayed invisible for claim_timeout_hours (4h) on the day before an investor
# submission, with no partner covering it.
#
# Nothing downstream is relaxed. These goals flow through the SAME liveness
# gates as in-progress ones (foreign-SID grace, body-heartbeat, diary staleness),
# so a live sibling mid-claim is still protected; only the status PREFILTER
# changes. Queried as separate calls because the endpoint takes one
# `goal_status` value, not a set.
_STRANDABLE_STATUSES = ("in-progress", "pending")


def _query_claimed_goals(agent: str) -> List[Dict[str, Any]]:
    """Daemon: GET /v1/aspirations/query — claimed goals in any strandable status.

    Returns the union over _STRANDABLE_STATUSES, de-duplicated by goal id: a
    goal cannot hold two statuses at once, but the dedup keeps a mid-flight
    status transition between the two calls from yielding the same goal twice
    and releasing it twice.
    """
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for status in _STRANDABLE_STATUSES:
        try:
            raw = _rt.rt_call(
                "GET",
                "/v1/aspirations/query",
                query={
                    "goal_status": status,
                    "goal_field_name": "claimed_by",
                    "goal_field_value": agent,
                },
            )
        except _rt.RtError as e:
            raise SystemExit(f"aspirations query failed: {e}") from e
        try:
            data = json.loads(raw or "[]")
        except json.JSONDecodeError as e:
            raise SystemExit(f"aspirations query returned non-JSON: {raw!r}") from e
        for g in (data if isinstance(data, list) else []):
            gid = g.get("goal_id") or g.get("id") or ""
            if gid and gid in seen:
                continue
            if gid:
                seen.add(gid)
            out.append(g)
    return out


def _read_goal_claim_fields(
    asp_id: str, goal_id: str, source: str,
) -> Dict[str, Optional[str]]:
    """Pull claimed_at + claimed_by_sid from the live aspiration record.

    The query endpoint omits both (intentional — query is identity info
    only), so we hit the active aggregate read. Deliberately ONE pass
    returning BOTH fields: this read walks the whole active aggregate per
    claimed goal, so a second function for the sid would double that cost
    on the common path.

    `claimed_by_sid` is stamped by aspirations-claim.sh (g-115-3176) and is
    the per-SESSION — therefore per-INSTANCE — identity of whoever claimed.
    Returns {"claimed_at": str|None, "claimed_by_sid": str|None}; a missing
    or non-string value reads as None so legacy pre-g-115-3176 claims (and
    any test fixture that omits it) fall through to the pre-existing
    behavior unchanged.
    """
    out: Dict[str, Optional[str]] = {"claimed_at": None, "claimed_by_sid": None}
    try:
        raw = _rt.aspirations_read(source=source, active=True)
    except _rt.RtError:
        return out
    # raw can be aggregate JSON ({"aspirations": [...]}) or raw list.
    try:
        decoded = _rt.tolerant_decode_aggregate("active", raw)
    except Exception:
        return out
    asps = decoded.get("aspirations", []) if isinstance(decoded, dict) else decoded
    for asp in asps or []:
        if asp.get("id") != asp_id:
            continue
        for g in asp.get("goals", []):
            if g.get("id") == goal_id:
                ca = g.get("claimed_at")
                sid = g.get("claimed_by_sid")
                out["claimed_at"] = ca if isinstance(ca, str) and ca else None
                out["claimed_by_sid"] = sid if isinstance(sid, str) and sid else None
                return out
    return out


# --- completed-not-closed guard () -----------------------------
# WHY THIS EXISTS. Every gate above asks "is anyone WORKING on this?" and
# reads a liveness signal (diary, carrier, in_flight, bg jobs). None of them
# asks the prior question: "has the work already HAPPENED?" A worker Body
# finishes a goal, writes a full outcome_note, and deliberately leaves the
# record at in-progress so the reducer verifies and closes it — the Mind/Body
# hand-off. No liveness signal survives that, because the worker is correctly
# idle. So the sweep saw the claim correctly, applied its rule correctly, and
# released FINISHED work: the verdict was rendered over a population from
# which the answer had been silently excluded (guard-1242 / guard-2403 class).
#
# MEASURED 2026-08-06 (alpha, cc-04): one --apply run released 32 goals, of
# which 22 were completed-not-closed — notes 3,723-13,693 chars, 13 saying
# "Held at in-progress" verbatim, 14 naming a cc-07 worker Body, several
# naming MERGED PRs. Because the released rows carry fresh high-scoring
# metadata, the selector then ranks them FIRST: 10 of the top 12 candidates
# on that box came from this batch, and a partner re-claimed one and was
# about to re-run a 181MB/705k-line log analysis that had finished 6h earlier.
#
# THIS IS A KEEP PREDICATE, NEVER A CLOSE. The verdict `completed-not-closed`
# means STOP AND READ, not "this is done" — guard-2852(c) is explicit that
# LENGTH IS NOT VERDICT: of 6 phantom-pending goals measured the same day,
# five notes said DONE and one 5,701-char note opened "ACCEPTANCE NOT MET —
# do NOT close this." A reader who pattern-matches "big note => finished"
# retires real work silently, which is worse than the duplicate execution
# this guard prevents. So the record carries `note_head` (the note's FIRST
# LINE, which is where the verdict lives) precisely so the next reader judges
# the verdict rather than the size.
_NOTE_EVIDENCE_MIN_CHARS = 1000

# Recurring goals are EXCLUDED, and this is load-bearing rather than tidy: on
# a standing cadence a populated outcome_note is the PREVIOUS cycle's note and
# is not evidence about THIS cycle at all. Keeping them would freeze every
# recurring goal in the fleet permanently. Measured on the live world queue:
# 2 of the 42 note-bearing in-progress goals are recurring ( at
# 168h,  at 10.67h) — both would have been wrongly frozen.
_RECURRING_MARKERS = (
    "recurring", "interval_hours",
    "original_interval_hours", "recurring_interval_hours",
)

# Resolution-side back-references ONLY. The originating goal named exactly one
# field, `resolved_goal` — which is carried by 1 of 56 resolved pipeline
# records (the single specimen it was filed from), so a predicate keyed on it
# alone would have been ~2% effective and read as working. The union of the
# four resolution-side names covers 9.
#
# `source_goal` (48 records) and `origin_goal` are deliberately NOT here. They
# name the goal that FORMED the hypothesis, not the one that RESOLVED it —
# different questions, and including them would keep goals whose hypothesis
# was merely filed under them, freezing genuinely stranded work. The
# originating specimen proves the split by itself: hypothesis
# 2026-07-27_position-fix-moves-failure-downstream carries
# source_goal= but resolved_goal=, so a source_goal
# predicate would have kept the WRONG goal and still released the one this
# whole goal was filed about. In all 9 records carrying both, they differ.
_PIPELINE_RESOLUTION_FIELDS = (
    "resolved_goal", "resolution_goal", "resolved_in_goal", "resolved_by_goal",
)

# Caches: one aggregate read per source and one pipeline read per process,
# serving BOTH sweep branches. Strictly cheaper than the per-goal aggregate
# walk `_read_goal_claim_fields` already pays on the claimed path.
_NOTE_MAP_CACHE: Dict[str, Dict[str, Dict[str, Any]]] = {}
_RESOLVED_GOALS_CACHE: Optional[set] = None


def _is_recurring_goal(g: Dict[str, Any]) -> bool:
    """True if any recurring marker is set. Absent/False/0 all read as False."""
    return any(bool(g.get(k)) for k in _RECURRING_MARKERS)


def _note_evidence_map(source: str) -> Dict[str, Dict[str, Any]]:
    """{goal_id: {note_len, note_head, recurring}} for one queue source.

    Fail-open: any read/parse failure yields {}, so the sweep reverts to
    exactly its pre-g-115-5177 behavior. A missed keep costs one re-claimable
    goal; a crash here would break the whole sweep.
    """
    if source in _NOTE_MAP_CACHE:
        return _NOTE_MAP_CACHE[source]
    out: Dict[str, Dict[str, Any]] = {}
    try:
        raw = _rt.aspirations_read(source=source, active=True)
        decoded = _rt.tolerant_decode_aggregate("active", raw)
        asps = decoded.get("aspirations", []) if isinstance(decoded, dict) else decoded
        for asp in asps or []:
            for g in asp.get("goals", []) or []:
                gid = g.get("id")
                if not gid:
                    continue
                note = g.get("outcome_note")
                note = note if isinstance(note, str) else ""
                head = next((ln.strip() for ln in note.splitlines() if ln.strip()), "")
                out[gid] = {
                    "note_len": len(note),
                    "note_head": head[:220],
                    "recurring": _is_recurring_goal(g),
                }
    except Exception:
        return {}
    _NOTE_MAP_CACHE[source] = out
    return out


def _pipeline_resolved_goal_ids() -> set:
    """Goal ids named RESOLUTION-side by any resolved pipeline record.

    Fail-open to an empty set for the same reason as `_note_evidence_map`.
    """
    global _RESOLVED_GOALS_CACHE
    if _RESOLVED_GOALS_CACHE is not None:
        return _RESOLVED_GOALS_CACHE
    ids: set = set()
    try:
        raw = _rt.rt_call("GET", "/v1/pipeline/read", query={"stage": "resolved"})
        data = json.loads(raw or "[]")
        recs = data.get("records", data) if isinstance(data, dict) else data
        if isinstance(recs, dict):
            recs = list(recs.values())
        for r in recs or []:
            if not isinstance(r, dict):
                continue
            for f in _PIPELINE_RESOLUTION_FIELDS:
                v = r.get(f)
                if isinstance(v, str) and v:
                    ids.add(v)
    except Exception:
        _RESOLVED_GOALS_CACHE = set()
        return _RESOLVED_GOALS_CACHE
    _RESOLVED_GOALS_CACHE = ids
    return ids


def _completion_evidence(
    goal_id: str, source: str, min_chars: int,
) -> Optional[Dict[str, Any]]:
    """Evidence that this goal's WORK already happened, or None.

    Two predicates, checked in the order of their measured yield:
      1. a substantial outcome_note on the row already in hand (21 of 22)
      2. a resolution-side pipeline back-reference (the note-less case, 1 of 22)
    Predicate 2 is kept despite its small share precisely because it catches
    the goal whose note was never written, which predicate 1 cannot see.
    """
    info = _note_evidence_map(source).get(goal_id) or {}
    if info.get("recurring"):
        return None  # standing cadence — a stale note is the PREVIOUS cycle
    note_len = int(info.get("note_len") or 0)
    if note_len >= min_chars:
        return {
            "predicate": "outcome_note",
            "note_len": note_len,
            "note_head": info.get("note_head", ""),
        }
    if goal_id in _pipeline_resolved_goal_ids():
        return {"predicate": "pipeline_resolution_ref", "note_len": note_len}
    return None


def _apply_completion_guard(
    record: Dict[str, Any], summary: Dict[str, Any],
    goal_id: str, source: str, min_chars: int,
) -> bool:
    """Stamp the completed-not-closed verdict on `record` if evidence exists.

    Returns True when the caller must KEEP (and `continue`). One helper for
    both sweep branches so the verdict text cannot drift between two copies —
    the release is equally destructive on either path.
    """
    summary["completion_checks"] = summary.get("completion_checks", 0) + 1
    comp = _completion_evidence(goal_id, source, min_chars)
    if not comp:
        return False
    record["verdict"] = "completed-not-closed"
    record["completion_evidence"] = comp
    if comp["predicate"] == "outcome_note":
        record["reason"] = (
            f"COMPLETED-NOT-CLOSED — STOP AND READ, do not re-execute and do "
            f"not blind-close. This goal carries a {comp['note_len']}-char "
            f"outcome_note, so work under it already happened; a worker Body "
            f"holds a finished goal at in-progress ON PURPOSE so the reducer "
            f"verifies and closes it, and releasing it converts 'held for the "
            f"reducer' into 'available to anyone' — whereupon the scorer ranks "
            f"it FIRST because its metadata is fresh. LENGTH IS NOT VERDICT "
            f"(guard-2852c): read the note's own first line, quoted here, "
            f"before deciding — some long notes say ACCEPTANCE NOT MET. "
            f"First line: {comp['note_head']!r} (g-115-5177)."
        )
    else:
        record["reason"] = (
            f"COMPLETED-NOT-CLOSED — STOP AND READ. A resolved pipeline record "
            f"names this goal on the RESOLUTION side, so the hypothesis it "
            f"existed to settle is already resolved even though its "
            f"outcome_note was never written ({comp['note_len']} chars). Close "
            f"the record rather than re-running the work "
            f"(g-115-5177 / guard-2852)."
        )
    summary["kept"] += 1
    summary["kept_completed_not_closed"] += 1
    summary["stranded"].append(record)
    return True


def _local_sid() -> Optional[str]:
    """This process's Claude Code session id, or None.

    bash-agent-inject.py exports MIND_SID into every Bash call, and this
    script runs as a python child of one — verified present in-process.
    None (unset) disengages the foreign-session guard entirely, which is the
    fail-open direction: an unknown local identity must not start KEEPING
    claims it cannot compare, or the sweep would stop recovering anything.

    So the guard is BEST-EFFORT, not guaranteed: bash-agent-inject.py is what
    exports MIND_SID, and that hook FAILS OPEN ON TIMEOUT (CLAUDE.md, Python
    Invocation). On a timed-out injection this returns None and the sweep
    reverts to exactly its pre-g-115-4004 behavior — box-local diary evidence
    only. That is the right way to fail (a missed keep costs one re-claimable
    goal; a wrong keep would freeze work), but do NOT read a green run as proof
    the guard was engaged. `local_sid` is echoed in the summary for precisely
    this reason: a null there means the guard did not run, not that it passed.
    """
    v = os.environ.get("MIND_SID", "").strip()
    return v or None


def _scan_diary_text(text: str, goal_id: str, since_iso: str) -> bool:
    """True iff `text` (diary JSONL) has an entry for goal_id with ts >= since_iso.

    Shared by the box-local and store-of-record readers below so the two can
    never drift apart on parsing — the whole point of the pair is that they
    answer the SAME question over DIFFERENT bytes.
    """
    since_dt = _parse_iso(since_iso)
    if since_dt is None:
        return False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("goal_id") != goal_id:
            continue
        ts = _parse_iso(entry.get("timestamp", ""))
        if ts is None:
            continue
        if ts >= since_dt:
            return True
    return False


def _diary_has_entry_after(agent: str, goal_id: str, since_iso: str) -> bool:
    """True iff the BOX-LOCAL execution-diary.jsonl has an entry for goal_id.

    Deliberately still a local read, and that is not an oversight (g-306-193).
    The two directions are not symmetric: a local HIT is conclusive — the entry
    exists, so work happened — while a local MISS is worth nothing on an
    own-cloud box, because the diary is `sync_tier: continuity` and this tree is
    a read-through cache. So this stays the cheap fast-KEEP path, and only the
    destructive branch pays for `_diary_has_entry_after_authoritative` below.
    """
    diary_path = agent_dir(agent) / "session" / "execution-diary.jsonl"
    try:
        text = diary_path.read_text(encoding="utf-8")
    except OSError:
        return False
    return _scan_diary_text(text, goal_id, since_iso)


def _diary_has_entry_after_authoritative(
    agent: str, goal_id: str, since_iso: str
) -> tuple[bool, str]:
    """Store-of-record twin of `_diary_has_entry_after`. Returns (hit, provenance).

    WHY THIS EXISTS (g-306-193, guard-980). `_diary_has_entry_after` reads the
    LOCAL file, and its False is the sweep's licence to release a claim.
    `execution-diary.jsonl` is `sync_tier: continuity` and
    `OwnCloudBackend._machine_local()` returns False for it — measured again on
    cc-04 2026-08-05 — so S3 is authoritative and a peer Body's entries are
    simply ABSENT here until this box pulls. Cache-cold therefore reads as "no
    work is happening" and the sweep releases a LIVE worker's claim. That is the
    destructive direction, which is why the check is paid here and not skipped.

    ONLY A POSITIVE HIT IS LOAD-BEARING. `absent` (the store of record
    positively reports no diary), `local-mirror` and `error` all return False,
    so the caller falls through to exactly the behaviour that shipped before
    this function existed. Same idiom as `_body_carrier_verdict`: the probe can
    only ever make the sweep MORE conservative, never introduce a new way to
    freeze work. Provenance is returned so the RECORD can say which of those
    four cases produced the False — an unreadable store and an empty one are
    otherwise the same answer (rb-6650).
    """
    try:
        from _fleet_diary import read_agent_diary

        # `.parent` of PROJECT_ROOT/agents/<agent> is the agents-parent dir,
        # which is what read_agent_diary's `base` wants. This is the sanctioned
        # single-.parent use (CLAUDE.md Agent-dir Resolution), NOT the
        #  class where a .parent result is mistaken for PROJECT_ROOT.
        # Routing through agent_dir() also keeps the test seam: the suite
        # patches agent_dir, so this honours the tmp root without a second knob.
        base = agent_dir(agent).parent
        text, provenance = read_agent_diary(agent, base=base)
    except Exception:  # noqa: BLE001 — never let the probe abort the sweep
        return False, "error"
    if provenance != "authoritative" or not text:
        return False, provenance
    return _scan_diary_text(text, goal_id, since_iso), provenance


# Trust order for a MISS, worst first. A fleet probe reads N diaries and a miss
# is only as trustworthy as its WEAKEST read: `authoritative` and `absent` are
# real answers from the store of record, while `local-mirror` and `error` mean
# the store was never reached — and an unreadable store cannot authorise a
# destructive act (guard-980, rb-6650). So the provenance recorded for a fleet
# miss is the MINIMUM observed, never the best.
_PROVENANCE_RANK = {"error": 0, "local-mirror": 1, "absent": 2, "authoritative": 3}


def _authoritative_diary_probe(
    agent: str, goal_id: str, since_iso: str, fleet: bool
) -> tuple[bool, str, Dict[str, Any]]:
    """Store-of-record diary probe — self-scoped or FLEET-scoped ( F-002).

    WHY THE FLEET SCOPE EXISTS, AND WHY ONLY ON ONE BRANCH. The claimed path
    queries `claimed_by == agent`, so its population is by construction this
    agent's own claims and a self-scoped probe is exactly right (the
    two-Bodies-of-one-Mind case shares one diary path). The no-claim WORLD path
    is different: `_query_inprogress_no_claim` never filters by agent — the
    parameter is unused in its body — so it enumerates in-progress world goals
    the WHOLE fleet may be executing, which is precisely why the sibling
    in_flight guard directly above it is fleet-wide. A self-scoped diary probe
    on that population answers a question about the wrong agent.

    MEASURED, not assumed (4/4, 2026-08-06, alpha on cc-07). For four goals with
    a structured diary entry under a peer and none under alpha — g-335-847
    (bravo), g-335-898 (echo), g-350-120 (foxtrot), g-001-10 (zeta) — the
    self-scoped probe returned `hit=False, provenance=authoritative`: a
    CONFIDENT miss, the shape that licenses the destructive flip, on goals a
    partner was working that same day. The fleet probe hit the named owner every
    time. Negative control `g-999-99999` missed on every agent, so the probe
    discriminates rather than always-hitting.

    NOT `read_fleet_diaries()`, though that is the obvious helper and was the
    remedy this goal's own description prescribed. Its docstring is explicit
    that it discards provenance because "this iterator's callers analyse content
    and have no destructive branch", and names `read_agent_diary` as the entry
    point for callers that do. This IS the destructive branch, and provenance is
    the field that distinguishes "the store says no work is happening" from "I
    could not reach the store" — so it iterates `fleet_agent_names` and reuses
    the provenance-preserving single-agent probe above.

    Self is probed FIRST: it is the modal hit and short-circuits the rest.
    """
    if not fleet:
        hit, prov = _diary_has_entry_after_authoritative(agent, goal_id, since_iso)
        return hit, prov, {"scope": "self", "probed": [agent], "hit_agent":
                           agent if hit else None}
    try:
        from _fleet_diary import fleet_agent_names

        # Same sanctioned single-`.parent` + agent_dir() test seam as
        # `_diary_has_entry_after_authoritative` above — see its comment.
        names = list(fleet_agent_names(agent_dir(agent).parent))
    except Exception:  # noqa: BLE001 — never let the probe abort the sweep
        names = []
    names = [agent] + [n for n in names if n != agent]
    probed: List[str] = []
    worst: Optional[str] = None
    for name in names:
        hit, prov = _diary_has_entry_after_authoritative(name, goal_id, since_iso)
        probed.append(name)
        if hit:
            return True, prov, {"scope": "fleet", "probed": probed,
                                "hit_agent": name}
        if worst is None or _PROVENANCE_RANK.get(prov, 0) < _PROVENANCE_RANK.get(
                worst, 3):
            worst = prov
    return False, worst or "error", {"scope": "fleet", "probed": probed,
                                     "hit_agent": None}


def _record_auth_check(
    summary: Dict[str, Any], record: Dict[str, Any],
    hit: bool, prov: str, detail: Dict[str, Any],
) -> None:
    """Stamp one authoritative-diary check onto the record AND the summary.

    The arrival counter is the whole point (g-306-220 F-001). Eagerly
    initialising `kept_authoritative_diary` to 0 removed one ambiguity (is this
    an old build that lacks the counter?) and left the one that matters: a 0
    still cannot separate "the gate ran N times and stopped nothing" from "the
    gate was never reached" — two readings that imply OPPOSITE actions (trust
    the guard vs. go find out why it is dead), which is the guard-1419 shape the
    eager init was meant to remove. `authoritative_checks` is the count of times
    the gate was REACHED, so the pair reads unambiguously, and the provenance
    histogram sums to it by construction (one entry per check).
    """
    record["authoritative_diary"] = {"hit": hit, "provenance": prov, **detail}
    summary["authoritative_checks"] += 1
    hist = summary["authoritative_provenance"]
    hist[prov] = hist.get(prov, 0) + 1


DEFAULT_CARRIER_FRESH_MINUTES = 15

try:
    # SSOT for the reap threshold lives with the decision logic, not here — a
    # second literal would be a dual source of truth and would drift silently
    # (communication-clarity rule 5). Guarded so a missing sibling module can
    # never stop this sweep from doing its pre-existing job.
    from body_row_reaper import (  # type: ignore  # noqa: E402
        DEFAULT_REAP_STALE_MINUTES as _REAP_STALE_MINUTES_DEFAULT,
    )
except Exception:  # noqa: BLE001 — the reaper leg self-disables below
    _REAP_STALE_MINUTES_DEFAULT = 180.0


def _body_carrier_verdict(
    agent: str, sid: str, fresh_minutes: int
) -> tuple[str, Dict[str, Any]]:
    """Is the Body holding `sid` demonstrably alive on ANOTHER box?

    Returns (verdict, evidence). FIVE verdicts, not two, and the extra ones are
    the point:

        fresh-correct  carrier present, ts within window, and its embedded sid
                       MATCHES -> the holder is alive elsewhere. The ONLY
                       verdict that keeps a claim.
        fresh-wrong    carrier fresh but written by a DIFFERENT sid. guard-358:
                       "mtime alone cannot distinguish designated writer is
                       alive from wrong writer is touching". A carrier cannot
                       vouch for a body that did not write it.
        stale          carrier present, ts older than the window.
        absent         no carrier (never written, or not yet synced).
        unreadable     present but undecodable / no usable ts.

    Only `fresh-correct` is load-bearing; every other verdict falls through to
    the pre-existing flat grace, so this probe can only ever make the sweep MORE
    conservative than it already was. That is why `absent` and `unreadable` are
    reported SEPARATELY rather than folded into `stale` — they take the same
    action today, but collapsing them now would erase the signal that tells a
    future reader whether the carrier pipeline is working at all (guard-2418:
    the absent path and the legitimately-false path must not share one
    observable).

    READ IS STORE-ROUTED, and the absolute path is load-bearing rather than
    stylistic: OwnCloudBackend.read_authoritative_bytes derives its S3 key via
    _s3_key, which raises ValueError for a path outside a configured root — and
    the method CATCHES that and silently returns local-mirror bytes. A relative
    path is indistinguishable from a genuinely out-of-root one, so the caller
    gets cache bytes from the one method that promises never to read the cache.
    _fleet_diary.py's module docstring records this measured (a probe reported
    local==authoritative for 5 agents while 4 were diverged). Hence .resolve()
    and the assert, mirroring that helper exactly.
    """
    ev: Dict[str, Any] = {"sid": sid[:8], "fresh_minutes": fresh_minutes}
    path = (agent_dir(agent) / "session" / f"body-heartbeat-{sid}.json").resolve()

    raw: Optional[str] = None
    try:
        from storage_backend import get_backend  # noqa: PLC0415 — optional dep

        backend = get_backend()
        assert path.is_absolute(), f"non-absolute carrier path: {path}"
        raw = backend.read_authoritative_bytes(path).decode("utf-8", errors="replace")
        ev["read_via"] = "authoritative"
    except FileNotFoundError:
        return "absent", ev
    except SystemExit:
        # DELIBERATELY explicit, and not defensive boilerplate. zeta measured
        # (msg-20260804-150658-zeta-5293) that two sibling readers in THIS file
        # promise fail-open via `except Exception` while the aggregate decoder
        # they call signals a malformed body with sys.exit(1) — SystemExit is a
        # BaseException, so it escapes, and the sweep hard-exits every iteration
        # while its docstring says that cannot happen. Any read reached from a
        # CLI-shaped helper can do this; catching it here costs one clause and
        # keeps the fail-open promise this function actually makes.
        ev["read_via"] = "systemexit"
        raw = None
    except Exception:  # noqa: BLE001 — backend absent/erroring: try the mirror
        ev["read_via"] = "fallback-local"
        raw = None

    if raw is None:
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return "absent", ev
        except OSError:
            return "unreadable", ev

    try:
        doc = json.loads(raw)
        if not isinstance(doc, dict):
            raise ValueError("carrier is not an object")
    except (json.JSONDecodeError, ValueError):
        return "unreadable", ev

    ts = _parse_iso(str(doc.get("ts") or ""))
    if ts is None:
        return "unreadable", ev

    age_min = (dt.datetime.now() - ts).total_seconds() / 60.0
    ev["carrier_age_minutes"] = round(age_min, 1)
    ev["carrier_host"] = doc.get("host")
    fresh = age_min <= fresh_minutes

    if str(doc.get("sid") or "") != sid:
        ev["carrier_sid"] = str(doc.get("sid") or "")[:8]
        return ("fresh-wrong" if fresh else "stale"), ev
    return ("fresh-correct" if fresh else "stale"), ev


def _has_pending_background_work(agent: str) -> bool:
    """True iff ``agent`` has pending background work — mirrors stop-hook
    Gate 2.5, which checks BOTH ``pending-agents.sh has-pending`` (Claude
    sub-agents) AND ``background-jobs.sh has-pending`` (long-running OS jobs).
    g-115-1925.

    Used to SKIP releasing a claim that LOOKS stranded (no post-claim diary
    marker + age >= stale threshold) but is legitimately paused across a turn
    boundary awaiting REGISTERED background work. This is the complement to
    rb-1533's phase-4 diary-marker defense: rb-1533 keeps a claim whose Phase 4
    wrote a ``phase-4-execute --goal <id>`` diary entry (which covers the
    harness ``run_in_background`` Bash-task case, detected by
    ``_diary_has_entry_after``); THIS check covers the registered-bg-work case
    where no fresh diary marker exists but the agent is genuinely busy.

    Fail-SAFE toward RELEASING: on ANY error (wrapper missing, subprocess
    failure, timeout) returns False, so a probe failure never SUPPRESSES a
    legitimate release (that would strand the sweep itself). The diary-marker
    check remains the primary keep-signal.
    """
    core = Path(__file__).resolve().parent
    env = {**os.environ, "MIND_AGENT": agent}
    # Invoke the .py backends via sys.executable (Python on Python, never
    # through bash). The .sh wrappers just `exec python3 <the .py> "$@"`, and a
    # Python->bash subprocess fails/hangs on Windows (rb-225/rb-247, guard-580,
    # guard-581; see the module docstring). This is the LAST subprocess in the
    # module and it is the right call here: neither pending-agents nor
    # background-jobs registers a daemon route, so there is no _rt client to
    # prefer (guard-555's first branch). Contrast _clear_team_in_flight, which
    # cited the same rule to justify a CLI call the daemon DID expose an
    # endpoint for — a sound rule applied to an unasked question ().
    for backend in ("pending-agents.py", "background-jobs.py"):
        script = core / backend
        if not script.exists():
            continue
        try:
            proc = subprocess.run(
                [sys.executable, str(script), "has-pending"],
                capture_output=True,
                timeout=15,
                env=env,
            )
        except (OSError, subprocess.SubprocessError):
            continue  # probe failure -> "not pending" (fail toward release)
        if proc.returncode == 0:
            return True
    return False


def _decode_team_state_field(raw: str) -> Any:
    """Decode a /v1/team-state/read field response (fresh-eyes ).

    The daemon serializes dict-valued fields as YAML and IGNORES a
    format=json query param (live-probed 2026-07-17; the .sh wrapper's
    --json output is a wrapper-side conversion, not a daemon behavior).
    Scalar/absent fields come back as plain text / empty. Try JSON first
    (cheap, covers null/scalars), then YAML. Returns None on any failure.
    """
    raw = (raw or "").strip()
    if not raw or raw == "null":
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    try:
        import yaml  # deferred — only dict-valued reads pay the import
        return yaml.safe_load(raw)
    except Exception:
        return None


def _release_goal(goal_id: str, source: str) -> Dict[str, Any]:
    """Release claim (strip claimed_by/claimed_at) + flip status pending.

    Two separate daemon writes — both must succeed for the release to be
    complete. Partial-failure is possible (release succeeds, status update
    fails). Caller surfaces the breaking step in the JSON output.
    """
    try:
        _rt.rt_call(
            "POST",
            "/v1/aspirations/release",
            query={"id": goal_id, "source": source},
        )
    except _rt.RtError as e:
        return {
            "ok": False,
            "step": "aspirations-release",
            "error": str(e)[:400],
        }
    try:
        _rt.rt_call(
            "POST",
            "/v1/aspirations/update-goal",
            query={"id": goal_id, "field": "status", "source": source},
            body=json.dumps("pending"),
        )
    except _rt.RtError as e:
        return {
            "ok": False,
            "step": "aspirations-update-goal-status",
            "error": str(e)[:400],
        }
    return {"ok": True, "step": "released+pending"}


def _announce_release(goal_id: str, source: str, agent: str,
                      record: Dict[str, Any]) -> Dict[str, Any]:
    """Post the matching release to the coordination board (guard-1610).

    WHY THIS EXISTS. `_release_goal` / `_flip_pending_no_claim` above update
    the QUEUE only — neither touches the board, and neither does the
    `aspirations-release.sh` they mirror. So an automated release was SILENT
    BY CONSTRUCTION: the only board trace of the goal stayed the agent's
    original `--type claim` post, unpaired forever. guard-1610 states the
    obligation ("you MUST post the matching release") but is addressed to the
    AGENT, so it never reached this sweep — a correct, retrievable rule that
    nothing READS at the moment of the action.

    Measured specimen (g-306-169, 2026-08-04, zeta on cc-02): world goal
    g-315-518 was claimed by alpha at 14:28:22, its work landed on origin/main
    at 15:56:13Z (commit 66530d33, 2 files / 387 insertions), and this sweep
    released it at 18:53:22 with no board post. 22.3h later it was still
    `pending` and unclosed, and the only board event naming it inside 72h was
    the original claim. Work-done -> release was 2h57m09s; close was never.
    The handoff was not delayed, it was destroyed.

    SCOPE — world only. An agent-source queue is private (no partner can
    select from it), so a board post about one would be pure noise on the
    shared coordination channel. guard-1610 is scoped to world goals for the
    same reason.

    PAIRING — read `goal-pickup-coordination-check.supersede_released_claims`
    before changing the author. It pairs claim->release PER AUTHOR with
    latest-event-wins, and its docstring is explicit that "a release by zeta
    cannot clear a live claim by bravo". On the CLAIMED path that is exactly
    right: `_query_claimed_goals` filters `claimed_by == agent`, so the
    releasing author IS the prior holder and the pair closes. On the NO-CLAIM
    path the claim record is already gone, so no author can be established
    from the record; that post is INFORMATIONAL only (the pairing helper's own
    word for a release with no preceding claim) and clears nobody's lien. It
    is emitted anyway, because the lien is only half the defect — the other
    half is that nothing tells the fleet the goal became selectable again.

    `author` is passed EXPLICITLY rather than left to the endpoint's
    X-Mind-Agent fallback. The fallback would resolve to the same name today,
    but the pairing above depends on the author being exactly the prior
    holder, so that is a load-bearing argument, not a decorative one.

    _rt, not a subprocess to board-post.sh: `POST /v1/board/post` is a real
    daemon route (mind_api/src/endpoints/board_write.py), so guard-555's first
    branch applies. This is the same question g-306-167 found unasked for the
    team-state clear — "do I need a subprocess at all?" — and the answer here
    is no.

    FAIL-OPEN, unconditionally, catching broad. The queue writes have ALREADY
    landed and are NOT rolled back, so a board failure must never be reported
    as a failed release. The failure is returned explicitly on the record and
    printed to stderr by the caller — never swallowed into a plausible-looking
    default (guard-1534), because a silent announce-failure would recreate the
    exact defect this helper exists to fix.
    """
    if source != "world":
        return {"posted": False, "reason": "agent-source queue is private — "
                                           "no partner can select it"}

    parts = [
        f"RELEASING {goal_id} -- returned to the world queue as pending by the "
        f"automated stranded-claim sweep running as {agent}."
    ]
    reason = record.get("reason")
    if reason:
        parts.append(f"Reason: {reason}.")
    age = record.get("age_minutes")
    if age is not None:
        parts.append(f"Age at release: {age}m.")
    sid = record.get("claimed_by_sid")
    if sid:
        parts.append(f"Prior holder session: {str(sid)[:8]}.")
    if record.get("foreign_sid_grace_expired"):
        parts.append("The holder was a DIFFERENT session of this same agent "
                     "and its foreign-session grace had expired.")
    if record.get("shape") == "no-claim":
        parts.append("NOTE: the claim record was already absent, so this "
                     "release cannot be paired against any author's claim "
                     "post -- it is informational only.")
    parts.append("Any agent may claim it now. If you were working this goal, "
                 "your claim was released without your knowledge -- re-claim "
                 "before resuming.")
    text = " ".join(parts)

    try:
        _rt.rt_call(
            "POST",
            "/v1/board/post",
            query={
                "channel": "coordination",
                "type": "release",
                "author": agent,
                "tags": f"{goal_id},{agent}",
            },
            body=text,
        )
    except Exception as e:  # noqa: BLE001 — fail-open by contract, see docstring
        return {"posted": False, "error": str(e)[:300]}
    return {"posted": True, "channel": "coordination", "type": "release"}


def _clear_team_in_flight(agent: str, goal_id: str) -> Dict[str, Any]:
    """Clear team-state in_flight ONLY if the LIVE goal_id matches (server-side).

    The scoped semantics are DELIBERATE, not vestigial — this was re-derived
    for g-306-167 rather than assumed, because "a stranded row has no live
    claimant, so an unconditional clear is fine" is a plausible reading that
    the evidence does not support. `in_flight` is ONE slot per AGENT, not one
    per goal. This sweep's mandate is "goal_id is stranded"; it holds no
    mandate over whatever else that slot may name. And a concurrent writer to
    this exact field is a condition this module ALREADY models: the
    g-115-4004 foreign-SID grace exists precisely because another live
    INSTANCE of this same agent can hold and re-stamp the row — and the
    release path that reaches this function fires when that grace EXPIRED,
    i.e. when the holder is only "almost certainly" gone. So the comparison
    stays.

    What changed is WHERE it happens. It used to run HERE: read the row,
    compare in Python, then call a clear that blanked whatever was present.
    That is a check-then-act — a sibling claim landing between the read and
    the write is destroyed regardless of the check, and the surrounding
    docstring made it read as guarded. `if_goal` moves the comparison into
    the write's own lock (make_clear_in_flight_modifier under
    locked_modify_yaml, shared by the CLI and daemon twins per
    guard-2323/guard-547), so it is atomic against a concurrent
    POST /v1/team-state/in-flight on the same row file. The client-side
    pre-read is GONE rather than kept as a cheap early-out: keeping it would
    leave the misleading shape in place while the endpoint's response already
    reports the same outcomes, and names the goal that blocked the clear.

    Invocation re-derived, not inherited — see the module docstring's
    CORRECTION: the endpoint exists, so guard-555's "typed _rt client first"
    branch applies and the subprocess is removed rather than fixed.
    """
    try:
        raw = _rt.rt_call(
            "POST",
            "/v1/team-state/clear-in-flight",
            query={"agent": agent, "if_goal": goal_id},
        )
    except _rt.RtError as e:
        return {"cleared": False, "reason": str(e)[:400] or "clear-in-flight failed"}
    try:
        res = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        res = {}
    if not isinstance(res, dict):
        res = {}
    if res.get("cleared"):
        return {"cleared": True, "reason": "matched and cleared"}
    skipped = res.get("skipped_goal_id")
    if skipped:
        # The CAS declined: the row moved to another goal between the sweep's
        # verdict and this write. Naming it makes the decline auditable.
        return {"cleared": False,
                "reason": f"in_flight holds {skipped}, not {goal_id}"}
    if res.get("row_survived") is True:
        # Also a decline, but of a row carrying no goal_id to name — so the
        # branch above cannot see it and it read as "already absent"
        # (). A sweep reason is an audit record: reporting a standing
        # row as absent hides the row from whoever reads the sweep output.
        return {"cleared": False,
                "reason": "in_flight row present but carries no comparable "
                          f"goal_id (not {goal_id}); left alone unverified"}
    return {"cleared": False, "reason": "in_flight already absent"}


def _query_inprogress_no_claim(agent: str, source: str = "agent") -> List[Dict[str, Any]]:
    """ (+ ): in-progress goals with NO claimed_by.

    The claimed-by query path (_query_claimed_goals) cannot see these:
    aspirations-claim.sh is the only claimed_by writer, so a goal that went
    in-progress without a (surviving) claim never matches the
    claimed_by==agent filter. Agent-source goals skip the claim wrapper by
    design (g-115-1691); world-source goals reach this state through broken
    flows — a release that strips the claim without resetting status, or a
    session dying between the two writes (g-115-2417, 3 observed). Read the
    source's active aggregate directly and surface in-progress goals with no
    claim, carrying last_modified as the stale-age basis (no claimed_at
    exists). Fail-open: any read/decode error yields an empty list (the
    sweep degrades gracefully, never crashes).
    """
    try:
        raw = _rt.aspirations_read(source=source, active=True)
    except _rt.RtError:
        return []
    try:
        decoded = _rt.tolerant_decode_aggregate("active", raw)
    except Exception:
        return []
    asps = decoded.get("aspirations", []) if isinstance(decoded, dict) else decoded
    out: List[Dict[str, Any]] = []
    for asp in asps or []:
        asp_id = asp.get("id", "")
        for g in asp.get("goals", []) or []:
            if g.get("status") != "in-progress":
                continue
            if g.get("claimed_by"):  # has a claim — the claimed path handles it
                continue
            out.append({
                "goal_id": g.get("id", ""),
                "asp_id": asp_id,
                "source": source,
                "title": g.get("title", ""),
                "last_modified": g.get("last_modified"),
            })
    return out


def _read_all_in_flight_goal_ids() -> set:
    """Goal-ids ANY agent's team-state in_flight currently names ().

    Guard for the world-source no-claim scan: the world queue is shared, so a
    claim-less in-progress world goal MIGHT still be live on a peer whose
    claim record was lost (partial release). in_flight is written at claim
    time and cleared at verify — a live peer usually still carries it. Keep
    such goals instead of flipping them out from under the peer.

    Fail-open toward the scan (empty set on any error): an unreadable
    team-state must not suppress legitimate flips — the flip is recoverable
    (a live peer's next status write or re-claim restores it), a permanently
    frozen goal is not. Mirrors _has_pending_background_work's fail direction.
    """
    try:
        raw = _rt.rt_call(
            "GET",
            "/v1/team-state/read",
            query={"field": "agent_status"},
        )
    except _rt.RtError:
        return set()
    data = _decode_team_state_field(raw)
    if not isinstance(data, dict):
        return set()
    out = set()
    for st in data.values():
        if isinstance(st, dict):
            infl = st.get("in_flight")
            if isinstance(infl, dict) and infl.get("goal_id"):
                out.add(infl["goal_id"])
    return out


def _flip_pending_no_claim(goal_id: str, source: str) -> Dict[str, Any]:
    """Flip a stranded no-claim in-progress goal back to pending ().

    No claim to release (claimed_by was never set) and no team-state in_flight
    to clear (in_flight is written at claim time). The single operative action
    is the status->pending flip that returns the goal to the selectable pool.
    """
    try:
        _rt.rt_call(
            "POST",
            "/v1/aspirations/update-goal",
            query={"id": goal_id, "field": "status", "source": source},
            body=json.dumps("pending"),
        )
    except _rt.RtError as e:
        return {
            "ok": False,
            "step": "aspirations-update-goal-status",
            "error": str(e)[:400],
        }
    return {"ok": True, "step": "flipped-pending"}


def _reap_stale_body_rows(
    agent: str,
    self_sid: Optional[str],
    stale_minutes: float,
    apply_changes: bool,
) -> Dict[str, Any]:
    """Reap `in_flight_bodies` rows whose Body died without a clean close ().

    The body-keyed sibling of the reducer row this sweep already reclaims. The
    decision is pure and lives in `body_row_reaper`; this function is only the
    I/O around it — read rows, read the per-sid carrier verdict, read the claim
    map, then (under --apply) call the EXISTING `clear_body_row` primitive. No
    new write path is introduced.

    OWNING-AGENT ONLY, and that is load-bearing rather than tidy:
    `merge_team_state_shard` reconciles a diverged shard by whole-snapshot
    last-writer-wins, so a non-owner pruner's write loses to the owner's fresher
    state. It reads and writes `agent` — this process's own — and nothing else.

    Fail-open in every direction, like the rest of this file: it runs inside a
    per-iteration sweep and must never raise.
    """
    out: Dict[str, Any] = {
        # Paired counters, per this file's convention: an arrival count AND a
        # fired count, so 0 can never mean both "nothing to reap" (healthy) and
        # "never ran" (structurally dead) — the guard-1419 shape.
        "rows_examined": 0,
        "reap_candidates": 0,
        "reaped": 0,
        "verdict_counts": {},
        "decisions": [],
        "shard_provenance": None,
        "claims_via": None,
        "errors": [],
    }
    try:
        from _paths import WORLD_DIR  # noqa: PLC0415 — lazy, cycle-proof
        from _team_state import (  # noqa: PLC0415
            read_shard_authoritative_with_provenance,
        )
        from worker_stall import read_claims_union  # noqa: PLC0415
        import body_row_reaper as _reaper  # noqa: PLC0415
        from worker_close_in_flight_clear import clear_body_row  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 — optional deps; never break the sweep
        out["errors"].append(f"import: {type(exc).__name__}: {exc}")
        return out

    def _read_rows() -> "tuple[Dict[str, Any], Optional[str]]":
        try:
            row, prov = read_shard_authoritative_with_provenance(str(WORLD_DIR), agent)
        except Exception as exc:  # noqa: BLE001
            # None, NOT {} — guard-2418: an unreadable shard and a genuinely
            # empty one take the same action but mean opposite things, and
            # collapsing them would render a read FAILURE as
            # "vanished-before-write" below, i.e. as evidence the row is gone.
            out["errors"].append(f"shard-read: {type(exc).__name__}: {exc}")
            return None, None
        bodies = (row or {}).get("in_flight_bodies")
        return (bodies if isinstance(bodies, dict) else {}), prov

    rows, provenance = _read_rows()
    out["shard_provenance"] = provenance
    if rows is None:
        return out
    out["rows_examined"] = len(rows)
    if not rows:
        return out

    def _read_claims() -> "Optional[tuple[Dict[str, str], str]]":
        # BOTH queues a Body of THIS agent can claim into (). The
        # world-only read made an agent-queue-only claim read as NO claim, which
        # is the one condition under which body_row_reaper reaps — so the sweep's
        # own safety invariant ("the SID owns no non-terminal claim") was true of
        # a narrower store than the invariant states. Only the OWNING agent's
        # queue is unioned, not every agent's: the reaper is owning-agent-only
        # and a session of agent X cannot claim into agent Y's queue, so this is
        # one extra read rather than one per agent.
        try:
            claims, via = read_claims_union(
                Path(WORLD_DIR) / "aspirations.jsonl",
                agent_dir(agent) / "aspirations.jsonl",
            )
        except Exception as exc:  # noqa: BLE001
            # The CONDEMNING half of the join. Unreadable claims must not be
            # read as "no claim" — that would flip every stale row straight to
            # reapable — so signal failure rather than degrade to an empty map.
            out["errors"].append(f"claims-read: {type(exc).__name__}: {exc}")
            return None
        if via == "none":
            # The except above cannot reach this case, and the comment in it was
            # describing a protection that did not exist: read_claims does not
            # RAISE on an unreadable store, it returns ({}, "none"). So the
            # degrade-to-empty the comment forbids was happening silently, one
            # layer below. Provenance is the only signal that separates "read
            # fine, no live claims" from "no layer answered" — check it, or the
            # union above is hollow the moment either half is unreadable.
            out["errors"].append("claims-read: no layer answered (provenance=none)")
            return None
        return claims, via

    first = _read_claims()
    if first is None:
        return out
    claims, claims_via = first
    out["claims_via"] = claims_via

    def _verdicts(sids) -> Dict[str, Any]:
        got: Dict[str, Any] = {}
        for sid in sids:
            try:
                got[sid] = _body_carrier_verdict(agent, sid, int(stale_minutes))
            except Exception as exc:  # noqa: BLE001
                out["errors"].append(f"carrier[{sid[:8]}]: {type(exc).__name__}: {exc}")
                got[sid] = (None, {})  # unrecognised -> KEEP, never reap
        return got

    decision = _reaper.decide(rows, _verdicts(rows.keys()), claims, self_sid)
    out["verdict_counts"] = decision["verdict_counts"]
    out["decisions"] = decision["decisions"]
    out["reap_candidates"] = len(decision["reapable"])

    if not apply_changes:
        return out

    for cand in decision["reapable"]:
        sid = cand["sid"]
        # guard-3020: NEVER write from a snapshot captured earlier in the turn.
        # Re-read the shard AND re-derive the verdict immediately before the
        # delete — a Body resurrected between the scan and here must not be
        # reaped on the strength of a reading that is now historical.
        fresh_rows, _ = _read_rows()
        if fresh_rows is None:
            # Distinct from the vanish below on purpose (guard-2418): "I could
            # not look" is not "it is gone", even though both decline the reap.
            cand["apply_result"] = "shard-unreadable-at-write"
            continue
        if sid not in fresh_rows:
            cand["apply_result"] = "vanished-before-write"
            continue
        # The claim map is HALF the decision, so it gets the same treatment as
        # the rows and the carrier: re-read here, not reused from the scan. A
        # sid that acquired a live claim in between is no longer an orphan, and
        # deciding that from a snapshot taken earlier in the turn is exactly the
        # read-modify-write guard-3020 forbids. An unreadable map declines the
        # reap rather than degrading to "no claim" (which would reap everything).
        fresh_claims = _read_claims()
        if fresh_claims is None:
            cand["apply_result"] = "claims-unreadable-at-write"
            continue
        recheck = _reaper.decide(
            {sid: fresh_rows[sid]}, _verdicts([sid]), fresh_claims[0], self_sid
        )
        if not recheck["reapable"]:
            cand["apply_result"] = "recheck-declined"
            cand["recheck_verdict"] = recheck["decisions"][0]["verdict"]
            continue

        print(f"[stranded-claim-sweep] REAPING body row "
              f"{json.dumps(cand, ensure_ascii=False)}", file=sys.stderr)
        try:
            token = clear_body_row(agent, sid)
        except Exception as exc:  # noqa: BLE001
            cand["apply_result"] = f"error: {type(exc).__name__}: {exc}"
            continue
        cand["clear_token"] = token

        # guard-2305: a team-state write can report success over a no-op, so the
        # token is not evidence. Read the field back and let the STORE say.
        after, _ = _read_rows()
        if after is None:
            # The write may well have succeeded, but an unverifiable write is
            # not a verified one and must not be counted as such — the whole
            # point of guard-2305 is that the token is not evidence.
            cand["apply_result"] = "readback-unreadable"
            out["errors"].append(f"readback[{sid[:8]}]: shard unreadable after {token}")
            continue
        if sid in after:
            cand["apply_result"] = "verify-failed-row-still-present"
            out["errors"].append(f"readback[{sid[:8]}]: row survived {token}")
            continue
        cand["apply_result"] = "reaped"
        out["reaped"] += 1

    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Release stranded in-progress claims (post-autocompact recovery)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually release stranded claims. Without this flag, the sweep "
             "is dry-run (reports findings but mutates nothing).",
    )
    parser.add_argument(
        "--stale-minutes",
        type=int,
        default=DEFAULT_STALE_MINUTES,
        help=f"Minimum age (minutes) of claimed_at before a claim with no "
             f"diary activity is considered stranded. Default: "
             f"{DEFAULT_STALE_MINUTES} (race-condition guard — claims younger "
             f"than this may legitimately be mid-Phase-4 setup).",
    )
    parser.add_argument(
        "--foreign-sid-grace-minutes",
        type=int,
        default=DEFAULT_FOREIGN_SID_GRACE_MINUTES,
        help=f"How long to KEEP a claim stamped with a different session id "
             f"than this process's MIND_SID (i.e. held by another live "
             f"instance of this same agent, whose work is invisible in this "
             f"box's execution diary). Past this age the holder is presumed "
             f"dead and the claim falls through to the normal release path, so "
             f"a dead instance cannot freeze a goal forever. Default: "
             f"{DEFAULT_FOREIGN_SID_GRACE_MINUTES}.",
    )
    parser.add_argument(
        "--carrier-fresh-minutes",
        type=int,
        default=DEFAULT_CARRIER_FRESH_MINUTES,
        help=f"How recent the cross-box body carrier "
             f"(<agent>/session/body-heartbeat-<SID>.json) must be for its "
             f"holder to count as demonstrably ALIVE, which HOLDS the claim "
             f"past --foreign-sid-grace-minutes. Deliberately much shorter than "
             f"that grace: the carrier is rewritten every heartbeat tick, so a "
             f"live worker refreshes it continuously, while a dead one goes "
             f"stale inside this window and then falls through to the ordinary "
             f"grace. Raising it past the grace would make the grace "
             f"unreachable; lowering it only reverts toward pre-g-306-208 "
             f"behavior. Default: {DEFAULT_CARRIER_FRESH_MINUTES}.",
    )
    parser.add_argument(
        "--completion-note-min-chars",
        type=int,
        default=_NOTE_EVIDENCE_MIN_CHARS,
        help=f"outcome_note length at or above which a NON-RECURRING goal is "
             f"treated as completed-not-closed and KEPT rather than released "
             f"(g-115-5177). Not a completion judgement — it routes the goal to "
             f"a human/reducer read, because LENGTH IS NOT VERDICT "
             f"(guard-2852c). Measured on the live world queue: the 40 at-risk "
             f"in-progress goals carry notes of 3,579-15,360 chars while "
             f"genuinely note-less ones carry 0, so any threshold in that gap "
             f"separates them; the default sits well below the observed "
             f"minimum. Raise it to release more aggressively, set it "
             f"absurdly high to disable this predicate (the pipeline "
             f"resolution-reference predicate still applies). "
             f"Default: {_NOTE_EVIDENCE_MIN_CHARS}.",
    )
    parser.add_argument(
        "--reap-stale-minutes",
        type=float,
        default=_REAP_STALE_MINUTES_DEFAULT,
        help=f"How stale this agent's OWN body-heartbeat carrier must be before "
             f"its `in_flight_bodies.<sid>` row is reaped as an unclean-death "
             f"orphan (g-306-191). Deliberately far longer than "
             f"--carrier-fresh-minutes: that one decides whether to HOLD a "
             f"claim (reversible next sweep), this one decides whether to "
             f"DELETE a row (not reversible — rows are written at claim time, "
             f"not per tick, so a wrongly-reaped live Body stays invisible for "
             f"the rest of its goal). Only rows whose sid holds NO non-terminal "
             f"claim are ever reaped. Default: {_REAP_STALE_MINUTES_DEFAULT}.",
    )
    args = parser.parse_args()

    agent = _agent_name()
    now = dt.datetime.now().replace(microsecond=0)
    stale_threshold = dt.timedelta(minutes=args.stale_minutes)
    foreign_grace = dt.timedelta(minutes=args.foreign_sid_grace_minutes)
    local_sid = _local_sid()

    claimed = _query_claimed_goals(agent)
    summary: Dict[str, Any] = {
        "agent": agent,
        "now": now.isoformat(),
        "stale_minutes": args.stale_minutes,
        "dry_run": not args.apply,
        "local_sid": local_sid,
        "foreign_sid_grace_minutes": args.foreign_sid_grace_minutes,
        "carrier_fresh_minutes": args.carrier_fresh_minutes,
        # EACH GUARD REPORTS A PAIR: how many times it was REACHED, and how many
        # times it FIRED. Eager-0 on the fired-counter alone was not enough
        # ( F-001) — it separates "old build without this counter" from
        # "counter present", but leaves 0 meaning either "reached N times, fired
        # never" (guard healthy, nothing to stop) or "never reached at all"
        # (guard structurally dead). Those imply OPPOSITE actions, which is the
        # guard-1419 shape the eager init was written to remove. Only the
        # arrival counter closes it, so neither number is published alone.
        #
        # carrier_checks : times the body-carrier verdict was computed.
        # kept_live_carrier : times it kept a claim (verdict fresh-correct).
        "carrier_checks": 0,
        "kept_live_carrier": 0,
        # authoritative_checks : times the store-of-record diary gate was
        #   reached — i.e. release candidates that survived every cheaper guard.
        # kept_authoritative_diary : releases the LOCAL diary would have made
        #   and the AUTHORITATIVE one stopped ().
        # authoritative_provenance : histogram over read_agent_diary's four
        #   provenance values, one entry per check, so it sums to
        #   authoritative_checks by construction. A miss under `local-mirror` or
        #   `error` is a miss the store of record never actually confirmed, and
        #   the histogram is the only place that distinction survives into the
        #   summary (guard-980, rb-6650).
        "authoritative_checks": 0,
        "kept_authoritative_diary": 0,
        "authoritative_provenance": {
            "authoritative": 0, "absent": 0, "local-mirror": 0, "error": 0,
        },
        # completion_checks : times the completed-not-closed gate was REACHED
        #   (release candidates surviving every liveness guard, both branches).
        # kept_completed_not_closed : times it stopped a release because the
        #   work had already happened ().
        # Paired for the reason stated above: alone, kept=0 cannot separate
        # "no finished work is being released" (healthy) from "the gate never
        # runs" (dead) — and those imply opposite actions. If completion_checks
        # climbs while kept stays 0, the predicate is reaching real candidates
        # and declining them, which is the fail-open direction working; if
        # completion_checks itself is 0, nothing reached the gate at all.
        "completion_checks": 0,
        "kept_completed_not_closed": 0,
        "scanned": len(claimed),
        "stranded": [],
        "kept": 0,
        "released": 0,
        "skipped_bg": 0,
        "skipped_foreign_sid": 0,
        "possible_displacement": 0,
    }

    # : lazily computed on the first would-be release/flip so the
    # has-pending subprocess cost is paid only when a release is actually about
    # to happen (never on the common scanned=0 / all-kept path).
    bg_pending: Optional[bool] = None

    for entry in claimed:
        goal_id = entry.get("goal_id", "")
        asp_id = entry.get("asp_id", "")
        source = entry.get("source", "world")
        title = entry.get("title", "")
        if not goal_id or not asp_id:
            continue

        claim_fields = _read_goal_claim_fields(asp_id, goal_id, source)
        claimed_at_iso = claim_fields["claimed_at"]
        claimed_by_sid = claim_fields["claimed_by_sid"]

        if not claimed_at_iso:
            summary["stranded"].append({
                "goal_id": goal_id,
                "asp_id": asp_id,
                "source": source,
                "title": title,
                "verdict": "kept",
                "reason": "claimed_at missing/unreadable — release manually if persistent",
            })
            summary["kept"] += 1
            continue

        claimed_at = _parse_iso(claimed_at_iso)
        if claimed_at is None:
            summary["stranded"].append({
                "goal_id": goal_id,
                "asp_id": asp_id,
                "source": source,
                "title": title,
                "verdict": "kept",
                "reason": f"claimed_at unparseable ({claimed_at_iso!r})",
            })
            summary["kept"] += 1
            continue

        has_recent_diary = _diary_has_entry_after(agent, goal_id, claimed_at_iso)
        age = now - claimed_at

        # --- displaced-holder detection (-c) -------------------------
        # THE LOSER'S HALF of the same-agent claim conflict that
        # coordination_merge.py now registers. When two live instances of ONE
        # agent claim the same goal, the merge resolves to the older claimed_at
        # and rewrites claimed_by_sid to the winner's. Nothing told the loser: it
        # is mid-execution, so its OWN diary entries satisfy has_recent_diary
        # below and it early-returns "work is happening" every iteration,
        # forever. The foreign-session guard further down never sees it, because
        # that guard sits AFTER this early-return and only ever receives claims
        # with no local diary at all.
        #
        # WHAT THIS CANNOT TELL YOU, and it is the load-bearing caveat.
        # The first version of this block read "foreign sid AND local diary
        # activity => I am the displaced holder", on the premise stated ~50
        # lines below that the execution diary is box-local. THAT PREMISE IS
        # FALSE. Measured 2026-08-03: session-manifest.yaml gives
        # execution-diary.jsonl `sync_tier: continuity`, and
        # OwnCloudBackend._machine_local() returns False for it — S3 is
        # authoritative and the local tree is a read-through cache. (Contrast
        # `execute-in-flight`, which IS sync_tier: machine_local precisely
        # because "a goal mid-execution on one machine is NOT in flight on
        # another".) The diary is keyed PER-AGENT, not per-session, and its
        # entries carry no session id at all — only content/entry_type/
        # goal_id/phase/timestamp. So a peer INSTANCE's entries appear here
        # as soon as this box pulls, and "this box worked the goal" is not a
        # question the diary can answer.
        #
        # Both readings therefore fit the same evidence:
        #   (1) I claimed it, a peer won on older claimed_at, I was displaced.
        #   (2) A peer legitimately holds it and is working it; I never did.
        # Reading (2) is the ORDINARY peer-claim case, so this fires on it too.
        # The verdict is named `possible-displacement` and NOT `displaced` for
        # exactly that reason — do not read it as a conclusion, and do not wire
        # an automatic abort onto it ( carries that warning).
        #
        # A sound discriminator needs per-SESSION attribution, which no
        # box-local store currently provides; the coordination board is the
        # likeliest source (its claim posts are append-only, so BOTH claims
        # survive the overwrite) — tracked by .
        #
        # Kept rather than reverted because the case was previously INVISIBLE:
        # it early-returned "work is happening" every iteration with no signal
        # at all. An honestly-hedged advisory beats silence; a falsely
        # confident one does not.
        #
        # REPORT-ONLY, deliberately — and NOT what the originating goal's
        # description proposed ("the loser detects its sid is gone ... and
        # aborts/releases"). Releasing is WRONG here: after the merge the claim
        # legitimately belongs to the WINNER, so releasing would clear a live
        # holder's claim and re-open the goal to a third instance, converting a
        # detected conflict into a wider one. Aborting is the agent's decision on
        # reading this verdict, not a mutation this sweep may make — so this
        # branch never writes, and a false positive costs one advisory line.
        if (claimed_by_sid and local_sid and claimed_by_sid != local_sid
                and has_recent_diary):
            summary["stranded"].append({
                "goal_id": goal_id,
                "asp_id": asp_id,
                "source": source,
                "title": title,
                "claimed_at": claimed_at_iso,
                "claimed_by_sid": claimed_by_sid,
                "age_minutes": round(age.total_seconds() / 60.0, 2),
                "verdict": "possible-displacement",
                "ambiguous": True,
                "reason": (
                    f"POSSIBLE DISPLACEMENT (ambiguous — do NOT auto-act): the "
                    f"claim on {goal_id} carries claimed_by_sid="
                    f"{claimed_by_sid[:8]} != this session {local_sid[:8]}, and "
                    f"the agent's execution diary shows activity for it. Two "
                    f"readings fit: (1) you claimed it and another INSTANCE of "
                    f"this agent won on older claimed_at, or (2) a peer instance "
                    f"legitimately holds it and you never worked it. The diary "
                    f"CANNOT distinguish them — it is sync_tier: continuity "
                    f"(shared per-agent, S3-authoritative) and its entries carry "
                    f"no session id, so a peer's entries read as local ones. "
                    f"IF (1): abort your work — but do NOT release the claim, it "
                    f"is the winner's now. IF (2): ignore this. Confirm via the "
                    f"coordination board, whose claim posts are append-only so "
                    f"both claims survive (guard-1460). g-306-132-c / g-306-143."
                ),
            })
            summary["possible_displacement"] += 1
            continue

        if has_recent_diary:
            summary["kept"] += 1
            continue  # work is happening — not stranded

        if age < stale_threshold:
            summary["kept"] += 1
            continue  # too fresh — race window

        record: Dict[str, Any] = {
            "goal_id": goal_id,
            "asp_id": asp_id,
            "source": source,
            "title": title,
            "claimed_at": claimed_at_iso,
            "age_minutes": round(age.total_seconds() / 60.0, 2),
            "verdict": "stranded",
            "reason": "no diary entry after claimed_at AND age >= stale threshold",
        }
        record["claimed_by_sid"] = claimed_by_sid

        # --- foreign-session guard () -------------------------------
        # THE CORE FIX. Every check above decides "stranded" from a BOX-LOCAL
        # READ — the execution diary under agents/<agent>/session/, which
        # .gitignore excludes (**/session/). The SUBJECT of that verdict is a
        # claim in aspirations.jsonl, which is SHARED. So a second live instance
        # of THIS SAME AGENT on another box may leave zero diary entries visible
        # here, and its LIVE claim reads as abandoned. Restated: the read is
        # local, the subject is global.
        #
        # This comment used to say the diary was "kept machine-local by
        # own-cloud". IT IS NOT — that is the false premise the module docstring
        # corrects at length (-c), and this was its last surviving copy
        # in the file, still asserting it ~100 lines below the correction.
        # Re-measured on cc-04 2026-08-05: sync_tier `continuity`, and
        # `OwnCloudBackend._machine_local()` returns False for it while
        # returning True for the sibling `execute-in-flight`. The diary is
        # REMOTE-AUTHORITATIVE with a read-through local cache, which is why the
        # store-of-record check below () exists at all: "machine-local"
        # would have meant a local miss was conclusive, and it is not.
        #
        # guard-1460 names exactly this — a claim is not proof no one else is
        # working it when the other worker could be another SESSION of the same
        # agent, because aspirations-claim.sh keys exclusion on the AGENT NAME
        # alone, so both instances "succeed" and neither is warned.
        #
        # claimed_by_sid makes "stranded" mean "no LIVE INSTANCE holds this"
        # rather than "no local diary entry exists". It needs NO write-side
        # change: aspirations-claim.sh already stamps it ( slice 1),
        # and that script's own header explicitly prescribes NOT inlining a
        # runner-token path here (it would need a 6th AGENTS_PARENT_DIR copy).
        # Reusing the identity already on the record is the cheaper half of the
        # same idea the originating goal proposed via runner-token.
        #
        # NOT permanent, deliberately. A genuinely dead instance's claim would
        # otherwise freeze forever, and this module's stated fail-direction
        # (see _read_all_in_flight_goal_ids) prefers a recoverable flip over a
        # permanent freeze. Past the grace window the claim falls through to the
        # normal logic, so the worst case here is DELAY, never deadlock.
        if claimed_by_sid and local_sid and claimed_by_sid != local_sid:
            if age < foreign_grace:
                record["verdict"] = "kept"
                record["reason"] = (
                    f"foreign-session: claimed_by_sid={claimed_by_sid[:8]} != this "
                    f"session {local_sid[:8]} — another INSTANCE of this agent holds "
                    f"it and the box-local diary cannot see its work. Keeping until "
                    f"age >= {args.foreign_sid_grace_minutes}m "
                    f"(g-115-4004 / guard-1460)."
                )
                summary["kept"] += 1
                summary["skipped_foreign_sid"] += 1
                summary["stranded"].append(record)
                continue
            # Grace expired on the CLOCK. Before acting on that, ask whether
            # the holder is actually alive (). The flat grace is a
            # pure timer — it consults no liveness signal at all, so a
            # cross-box worker unit legitimately running longer than the window
            # gets its claim popped while it is still working, which is the
            #  class this goal exists to close.
            #
            # ONLY `fresh-correct` keeps the claim: carrier present, recent,
            # AND written by the very sid named on the claim. Every other
            # verdict — including a fresh carrier written by a DIFFERENT body
            # (guard-358 fresh-wrong) — falls through to exactly the behavior
            # that shipped before this block existed. So the worst case of a
            # broken/never-synced carrier is today's behavior, never worse.
            carrier_verdict, carrier_ev = _body_carrier_verdict(
                agent, claimed_by_sid, args.carrier_fresh_minutes
            )
            summary["carrier_checks"] += 1
            record["body_carrier"] = {"verdict": carrier_verdict, **carrier_ev}
            if carrier_verdict == "fresh-correct":
                record["verdict"] = "kept"
                record["reason"] = (
                    f"foreign-session HELD PAST GRACE: age "
                    f"{age.total_seconds() / 60.0:.0f}m >= "
                    f"{args.foreign_sid_grace_minutes}m, but the body carrier for "
                    f"sid={claimed_by_sid[:8]} is fresh "
                    f"({carrier_ev.get('carrier_age_minutes')}m <= "
                    f"{args.carrier_fresh_minutes}m) on host "
                    f"{carrier_ev.get('carrier_host')} — the holder is ALIVE on "
                    f"another box, so the clock was wrong, not the claim "
                    f"(g-306-208)."
                )
                summary["kept"] += 1
                summary["skipped_foreign_sid"] += 1
                summary["kept_live_carrier"] += 1
                summary["stranded"].append(record)
                continue
            # Not demonstrably alive — proceed exactly as before. The evidence
            # stays on the record so the release is explainable after the fact,
            # and so a reader can tell a genuinely-dead holder (stale) from a
            # carrier that never arrived (absent) without re-running anything.
            record["foreign_sid_grace_expired"] = True

        # : bg-pending guard (mirrors stop-hook Gate 2.5). A claim
        # that looks stranded may be legitimately paused awaiting REGISTERED
        # background work (OS jobs / Claude sub-agents). Skip the release; the
        # next sweep after the bg work completes re-evaluates. rb-1533's
        # phase-4 diary marker covers the harness-bg-task case separately.
        if bg_pending is None:
            bg_pending = _has_pending_background_work(agent)
        if bg_pending:
            record["verdict"] = "kept"
            record["reason"] = ("stranded-skip-bg: agent has pending background "
                                "work (pending-agents/background-jobs "
                                "has-pending) — g-115-1925")
            summary["kept"] += 1
            summary["skipped_bg"] += 1
            summary["stranded"].append(record)
            continue

        # --- store-of-record diary check () -------------------------
        # THE LAST GATE BEFORE RELEASE. Every check above decided "stranded"
        # partly on `has_recent_diary`, which is a BOX-LOCAL read — and
        # execution-diary.jsonl is `sync_tier: continuity`, so a peer Body's
        # entries live in S3 and are simply ABSENT here until this box pulls
        # (guard-980). Cache-cold therefore reads as "no work is happening" and
        # this branch releases a LIVE worker's claim.
        #
        # PAID HERE, ONCE, ON THE DESTRUCTIVE BRANCH ONLY. The population that
        # reaches this line is already tiny — local diary miss AND past the
        # stale threshold AND past the foreign-sid, carrier and bg guards — so
        # the cost is one authoritative read per actual release candidate, not
        # one per claimed goal per iteration.
        #
        # RUNS IN DRY-RUN TOO, deliberately. An agent reads dry-run output to
        # decide whether a release is safe; a dry run reporting "stranded"
        # where --apply would report "kept" is the wrong kind of disagreement.
        #
        # SELF-SCOPED, and that is correct here rather than an oversight: this
        # loop's population comes from `_query_claimed_goals(agent)`, so every
        # goal in it is claimed BY this agent. The live holder is this Mind on
        # another box, and both Bodies write the SAME per-agent diary path — so
        # this agent's diary IS the store of record for this population. The
        # no-claim world branch below is the one that needs a fleet scope, for
        # the opposite reason ( F-002).
        auth_hit, auth_prov, auth_detail = _authoritative_diary_probe(
            agent, goal_id, claimed_at_iso, fleet=False)
        _record_auth_check(summary, record, auth_hit, auth_prov, auth_detail)
        if auth_hit:
            record["verdict"] = "kept"
            record["reason"] = (
                f"CACHE-COLD FALSE STRAND: the box-local diary showed no entry "
                f"for {goal_id} after {claimed_at_iso}, but the STORE OF RECORD "
                f"does. A live worker — this agent on another box — holds this "
                f"claim and its entries had not synced here yet. Releasing a "
                f"live worker's claim is the destructive direction this check "
                f"exists to stop (g-306-193 / guard-980)."
            )
            summary["kept"] += 1
            summary["kept_authoritative_diary"] += 1
            summary["stranded"].append(record)
            continue

        # --- completed-not-closed guard () -------------------------
        # THE LAST GATE, deliberately after every liveness check above: those
        # ask "is anyone working on this NOW", this asks "did the work already
        # HAPPEN". A finished worker Body is correctly idle, so it passes every
        # liveness gate as abandoned. Runs in dry-run too, for the same reason
        # the authoritative diary check does — a dry run that disagrees with
        # --apply is the wrong kind of disagreement.
        if _apply_completion_guard(record, summary, goal_id, source,
                                   args.completion_note_min_chars):
            continue

        if args.apply:
            # Observability (): --apply is destructive, and a
            # follow-up dry-run reports 0 stranded — so WHICH claim was
            # released is unrecoverable from the sweep alone. Emit the
            # PRE-mutation record to stderr FIRST: stdout carries only the
            # post-hoc summary, and stderr still lands in the loop's captured
            # output even if the release dies mid-write.
            print(f"[stranded-claim-sweep] RELEASING "
                  f"{json.dumps(record, ensure_ascii=False)}", file=sys.stderr)
            rel = _release_goal(goal_id, source)
            record["release_result"] = rel
            if rel.get("ok"):
                clear = _clear_team_in_flight(agent, goal_id)
                record["team_state_clear"] = clear
                # guard-1610: the release is not complete until the fleet can
                # SEE it. Fires after the queue writes land, so a board failure
                # can never leave a goal released-but-still-claimed.
                ann = _announce_release(goal_id, source, agent, record)
                record["board_announce"] = ann
                if ann.get("error"):
                    print(f"[stranded-claim-sweep] ANNOUNCE-FAILED {goal_id}: "
                          f"{ann['error']} — the goal IS released; the board "
                          f"post is not. Post it by hand (guard-1610).",
                          file=sys.stderr)
                summary["released"] += 1
                record["verdict"] = "released"
            else:
                summary["kept"] += 1
                record["verdict"] = "release-failed"

        summary["stranded"].append(record)

    # : second shape — in-progress goals with NO claimed_by
    # (structurally invisible to the claimed_by==agent query above).
    # : third shape — the same scan over the WORLD source (a
    # release-without-status-reset or a death between the two writes leaves
    # world orphans too; 3 observed 2026-07-16). World entries carry one
    # extra guard: a goal named by ANY agent's team-state in_flight is kept.
    no_claim = _query_inprogress_no_claim(agent, "agent") \
        + _query_inprogress_no_claim(agent, "world")
    summary["scanned_no_claim"] = len(no_claim)
    # Lazily fetched on the first world-source candidate (cheap single read;
    # skipped entirely when no world orphans exist).
    live_in_flight: Optional[set] = None
    for entry in no_claim:
        goal_id = entry.get("goal_id", "")
        asp_id = entry.get("asp_id", "")
        source = entry.get("source", "agent")
        title = entry.get("title", "")
        lm_iso = entry.get("last_modified")
        if not goal_id or not asp_id:
            continue

        if not lm_iso or not isinstance(lm_iso, str):
            summary["stranded"].append({
                "goal_id": goal_id, "asp_id": asp_id, "source": source,
                "title": title, "shape": "no-claim", "verdict": "kept",
                "reason": "last_modified missing/unreadable — cannot age; "
                          "release manually if persistent",
            })
            summary["kept"] += 1
            continue

        lm = _parse_iso(lm_iso)
        if lm is None:
            summary["stranded"].append({
                "goal_id": goal_id, "asp_id": asp_id, "source": source,
                "title": title, "shape": "no-claim", "verdict": "kept",
                "reason": f"last_modified unparseable ({lm_iso!r})",
            })
            summary["kept"] += 1
            continue

        has_recent_diary = _diary_has_entry_after(agent, goal_id, lm_iso)
        age = now - lm

        if has_recent_diary:
            summary["kept"] += 1
            continue  # work is happening — not stranded

        if age < stale_threshold:
            # Also covers a NEGATIVE age from a cross-box future stamp
            # ( TZ skew) — keep is the safe direction there.
            summary["kept"] += 1
            continue  # too fresh — race / mid-transition window

        record = {
            "goal_id": goal_id, "asp_id": asp_id, "source": source,
            "title": title, "shape": "no-claim",
            "last_modified": lm_iso,
            "age_minutes": round(age.total_seconds() / 60.0, 2),
            "verdict": "stranded",
            "reason": "in-progress with no claimed_by, no diary entry after "
                      "last_modified AND age >= stale threshold",
        }

        # : shared-queue guard — a world goal a peer is live on
        # (in_flight names it) is kept even though its claim record is gone;
        # flipping would yank the goal mid-execution. Agent-source goals are
        # private (no peer can be live on them) — guard skipped.
        if source == "world":
            if live_in_flight is None:
                live_in_flight = _read_all_in_flight_goal_ids()
            if goal_id in live_in_flight:
                record["verdict"] = "kept"
                record["reason"] = ("no-claim but a live team-state in_flight "
                                    "names this goal — peer mid-execution, "
                                    "claim record lost (g-115-2417)")
                summary["kept"] += 1
                summary["stranded"].append(record)
                continue

        # : bg-pending guard (mirrors the claimed-path guard above /
        # stop-hook Gate 2.5). A no-claim in-progress goal can be bg-paused too.
        if bg_pending is None:
            bg_pending = _has_pending_background_work(agent)
        if bg_pending:
            record["verdict"] = "kept"
            record["reason"] = ("stranded-skip-bg: agent has pending background "
                                "work (pending-agents/background-jobs "
                                "has-pending) — g-115-1925")
            summary["kept"] += 1
            summary["skipped_bg"] += 1
            summary["stranded"].append(record)
            continue

        # --- store-of-record diary check () -------------------------
        # Same gate as the claimed path above, on this path's own timestamp
        # (last_modified rather than claimed_at). This shape has NO claimed_by,
        # so the foreign-sid and carrier guards never run for it — the local
        # diary read is an even larger share of the evidence here, which makes
        # the authoritative confirmation more load-bearing, not less.
        #
        # FLEET-SCOPED ON WORLD, SELF-SCOPED ON AGENT ( F-002), which
        # is the same split the in_flight guard 30 lines up already makes and
        # for the same reason. Agent-source goals are private, so this agent's
        # diary is the store of record for them. World-source goals are not:
        # `_query_inprogress_no_claim` ignores its `agent` argument entirely, so
        # this loop enumerates in-progress world goals ANY fleet member may be
        # mid-execution on, and asking only this agent's diary answers a
        # question about the wrong agent. Until this split existed the branch
        # probed only self while the comment above claimed the check was "more
        # load-bearing" here — measured 4/4 on live peer-held goals, the
        # self-scoped probe returned `hit=False, provenance=authoritative`: a
        # CONFIDENT miss licensing the flip of work a partner was doing.
        auth_hit, auth_prov, auth_detail = _authoritative_diary_probe(
            agent, goal_id, lm_iso, fleet=(source == "world"))
        _record_auth_check(summary, record, auth_hit, auth_prov, auth_detail)
        if auth_hit:
            record["verdict"] = "kept"
            record["reason"] = (
                f"CACHE-COLD FALSE STRAND: the box-local diary showed no entry "
                f"for {goal_id} after {lm_iso}, but the STORE OF RECORD does — "
                f"a live worker is mid-execution and its entries had not synced "
                f"to this box yet (g-306-193 / guard-980). Holder per the "
                f"store of record: {auth_detail.get('hit_agent') or 'unknown'} "
                f"(probe scope: {auth_detail.get('scope')})."
            )
            summary["kept"] += 1
            summary["kept_authoritative_diary"] += 1
            summary["stranded"].append(record)
            continue

        # --- completed-not-closed guard () -------------------------
        # Same gate as the claimed path, for the same reason. A goal whose
        # claim record was stripped is not less finished than one that kept it,
        # and this branch's flip is equally destructive: it rewrites
        # in-progress -> pending, which is exactly what promotes finished work
        # to the top of the scorer.
        if _apply_completion_guard(record, summary, goal_id, source,
                                   args.completion_note_min_chars):
            continue

        if args.apply:
            # Observability () — same rationale as the claimed path.
            print(f"[stranded-claim-sweep] FLIPPING "
                  f"{json.dumps(record, ensure_ascii=False)}", file=sys.stderr)
            res = _flip_pending_no_claim(goal_id, source)
            record["flip_result"] = res
            if res.get("ok"):
                # guard-1610 — same obligation, weaker pairing. The claim
                # record is gone, so this post is informational rather than
                # lien-clearing (see _announce_release "PAIRING"); the
                # visibility half is what it buys.
                ann = _announce_release(goal_id, source, agent, record)
                record["board_announce"] = ann
                if ann.get("error"):
                    print(f"[stranded-claim-sweep] ANNOUNCE-FAILED {goal_id}: "
                          f"{ann['error']} — the goal IS flipped to pending; "
                          f"the board post is not. Post it by hand "
                          f"(guard-1610).", file=sys.stderr)
                summary["released"] += 1
                record["verdict"] = "released"
            else:
                summary["kept"] += 1
                record["verdict"] = "release-failed"

        summary["stranded"].append(record)

    # --- body-row reaper () -------------------------------------
    # The body-keyed sibling of everything above. Runs unconditionally so its
    # paired counters are always published (a reader must be able to tell "no
    # orphans" from "leg never ran"); mutates only under --apply, exactly like
    # the claim legs. Wrapped because a sweep that already did its primary job
    # must not exit non-zero over an optional leg.
    try:
        summary["body_row_reaper"] = _reap_stale_body_rows(
            agent=agent,
            self_sid=local_sid,
            stale_minutes=args.reap_stale_minutes,
            apply_changes=args.apply,
        )
    except Exception as exc:  # noqa: BLE001
        summary["body_row_reaper"] = {
            "errors": [f"leg-failed: {type(exc).__name__}: {exc}"],
        }

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
