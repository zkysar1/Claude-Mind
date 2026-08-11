#!/usr/bin/env python3
"""Blocker Recheck — re-examine aged blockers against the capability gate.

For every blocker older than --max-age-hours that routed to `[user]` (or
`[agent, user]`), re-run capability-gate.py against the original failure_reason.
If the gate now matches an agent-provisionable capability that was overlooked
at creation time, this script can auto-clear the blocker and write an Investigate
goal so the retrieval lapse is learned from instead of buried.

Called by aspirations-precheck Phase 0.5b.0.5 (Capability Recheck Sweep).
Reads working memory via _rt.wm_read (daemon client); writes via wm.py set
(still alive). Investigate goals filed via _rt.aspirations_add_goal (daemon
client). Dry-run by default; pass --apply to actually clear blockers and
create Investigate goals.

TWO POPULATIONS, ONLY ONE OF WHICH IS MUTABLE HERE (g-115-4328)
---------------------------------------------------------------
Blockers live in two places, and until 2026-08-01 this sweep read only the
first:

  (1) the `known_blockers` WORKING-MEMORY slot — per-agent, per-box, EPHEMERAL;
  (2) the `blocker_ref` field on the GOAL RECORD — shared, fleet-wide, DURABLE.

`create-blocker.py` writes BOTH: the WM entry first, then `_set_goal_blocker_ref`
mirrors it onto the goal, calling the WM entry "the authoritative record" and the
goal copy "a redundancy for the gate's structural check". That relationship is
inverted with respect to DURABILITY, and the measurement shows which one survives:
on 2026-08-01 all five fleet agents read `known_blockers=null` while SIX
non-terminal goals carried a live `blocker_ref`. The ephemeral "authoritative"
store had lost everything; the durable "redundancy" was the only surviving record.

A slot-only read therefore reported `total_blockers: 0` — a number indistinguishable
from a genuinely clean queue (guard-1802 / rb-5650: a zero-result run and a clean
queue produce identical output). That is the `enumerator-all-clear-boundary` class:
the count was honest about the population it enumerated and silent about the one
the reader cared about.

WHY THE GOAL-SOURCED HALF IS REPORT-ONLY, NOT AUTO-CLEARED. Widening the COUNT
fixes the vacuous all-clear. Widening the CLEAR PATH would be a different and
much riskier change, refused here for three measured reasons:
  * guard-1978: this script consults NO probe — it decides purely on a
    capability-gate keyword match against blocker narrative prose. Extending
    that keyword-match clear path over a new population extends a known
    false-positive surface (the streak-roblox-studio clear of a verified-live
    outage is the canonical instance).
  * Clearing a goal-sourced blocker means mutating the GOAL, not a WM slot.
    `blocked-signal-resolution-check.py` (precheck Phase 0.5b.12) already owns
    that population for the "is this block resolved?" question and is
    deliberately DETECTIVE-ONLY, because most such goals are lane-owned by
    another agent and unblocking one appropriates its owner's queue.
  * The two sweeps ask different questions and both are needed: 0.5b.12 asks
    "have this goal's block signals resolved?"; this asks "was an
    agent-provisionable capability OVERLOOKED at creation time?".

So goal-sourced entries are counted, aged, structurally filtered, and gate-checked
for VISIBILITY, and their `action` is always report-only. They are also excluded
from the `known_blockers` write-back — see the partition in main().

Exit codes: always 0 (reporting tool). Use the JSON output's `actions_taken`
field to determine what changed.
"""

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from _dt import parse_naive_iso  # noqa: E402  (shared tzinfo-stripping naive-ISO parse, )
import _rt  # canonical Python -> daemon client (post-cutover; see _rt.py)

# Blocker types that are STRUCTURALLY ineligible for auto-clearance by this
# script. No keyword-match score can substitute for the human action these
# represent — SSH trust rotation, credential issuance, and physical hardware
# actions cannot be agent-provisioned regardless of how strongly a capability
# keyword appears to match. See guard-146 (security-trust exclusion) and
# session-47 post-mortem for the incident that surfaced this gap.
HUMAN_ONLY_BLOCKER_TYPES = {
    "security-trust",
    "credentials-required",
    "physical-hardware",
    "user_action",
}

# Blockers whose lifecycle is owned by the PRODUCER that emits them, not by
# this recheck. These are auto-generated from a live probe signal and are
# re-derived from scratch on every producer sync, so the producer already
# clears them the moment the underlying condition recovers
# (infra-health.py::_sync_known_blockers: "Removes any pre-existing streak-*
# entries before re-adding — single sync pass = idempotent. Recovery is
# automatic"). This script clearing them is therefore never NEEDED, and is
# actively harmful when the condition is still live.
#
# The deeper reason is a category error, not a tuning problem. The capability
# recheck asks "was an agent-provisionable capability OVERLOOKED at blocker
# creation time?" — a question about a decision CREATE_BLOCKER's Step 2.5 made.
# A producer-emitted blocker never went through CREATE_BLOCKER at all, so there
# is no creation-time capability decision to have been wrong. That is why the
# false Investigate this path filed () asked why Step 2.5 "missed"
# a capability that was never applicable: the question is unanswerable because
# it presupposes a step that never ran.
#
# These blockers also evade BOTH existing structural filters, which is why a
# third is needed rather than an entry in one of them: they carry no `type`
# (so the HUMAN_ONLY_BLOCKER_TYPES test above sees None) and no `participants`
# (so the is_user_routed test below treats them as user-routed by the legacy
# allowance). Measured 2026-07-30 on the live streak-roblox-studio blocker:
# match_count=10, top_match=access-roblox-studio, matched_keyword='connect' —
# cleared for real at 14:23:26 while the canonical probe reported
# doctor_verdict=relay-dead with 28 consecutive failures. Owning the
# access-roblox-studio skill does not repair a black-holed localhost relay.
#
# Cost of a wrong clear is asymmetric and is what makes this fail-closed:
# `resolution` is set on known_blockers, and BOTH the Phase 0.5b re-probe loop
# AND the proactive-escalation path iterate known_blockers — so a live outage
# goes invisible to each, plus a false Investigate is filed. Cost of a wrong
# NON-clear is nil: the producer re-derives and drops the entry on recovery.
PRODUCER_MANAGED_SOURCE_PREFIXES = ("infra-health.",)


def _is_producer_managed(b) -> bool:
    """True when a blocker's lifecycle is owned by its emitting producer.

    Two independent signals, OR-ed, because they are written at the same
    construction site and either alone is sufficient identification:
      - `source` (e.g. "infra-health.streak-alert") names the producer;
      - `blocker_id` prefix "streak-" is what infra-health's own `_is_streak`
        predicate keys on when it re-derives the entries.

    Both tests are isinstance-guarded before `.startswith`. A bare-string or
    non-dict record must return False rather than raise — the same read-side
    discipline `blocker_ref` handling requires, where a naive `.get` on a
    string raises AttributeError and a bare try/except reads it as absent.
    """
    if not isinstance(b, dict):
        return False
    src = b.get("source")
    if isinstance(src, str) and src.startswith(PRODUCER_MANAGED_SOURCE_PREFIXES):
        return True
    bid = b.get("blocker_id")
    return isinstance(bid, str) and bid.startswith("streak-")


def _run(argv, input_text=None) -> tuple:
    """Run a subprocess. Return (returncode, stdout, stderr)."""
    result = subprocess.run(
        argv,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode, result.stdout, result.stderr


def _py(args: list, input_text=None) -> tuple:
    """Run a core/scripts/*.py helper via the CURRENT Python interpreter.

    INVARIANT: uses sys.executable directly, not a bash subprocess. On Windows
    the parent process is reached via the python3 shim which execs `py`; the
    resulting interpreter's sys.executable points at the real Python binary,
    so child subprocesses bypass the shim cleanly. Shelling through bash for
    these helpers was unreliable because `bash` from Python can resolve to
    WSL bash.exe (different PATH, no python3 shim). Do not reintroduce.
    """
    return _run([sys.executable] + args, input_text=input_text)


def _tolerant_decode(slot, raw):
    """-tolerant decode for the wm_read('known_blockers') body.

    Thin wrapper around `_rt.tolerant_decode_aggregate` (extracted via
    g-115-949; 5th-site migration g-115-1057). Sister sites:
    consolidation-health.py, defer-recheck.py, parent-supersession-sweep.py,
    precondition-defer-recheck.py — all delegate to the same shared helper.

    One pre-delegation early-return preserves wm-slot-specific semantics:
    `wm_read` returns the literal string `"null"` when the slot was set to
    JSON null. That is the canonical empty-slot serialization (NOT a source
    error or a corrupt aggregate), and must map to None so the caller's
    `if data is None: return []` collapses it to an empty blocker list.
    `_rt.tolerant_decode_aggregate` parses "null" as Python None and
    correctly classifies it as a non-dict-and-non-list aggregate (fatal);
    the early-return below short-circuits that path for THIS slot only,
    keeping the daemon-aggregator's strict contract intact for all other
    bodies.

    See _rt.tolerant_decode_aggregate for the full guard-383 contract
    (raw_decode recovery, JSONDecodeError fatal, non-aggregate fatal). The
    fail-open boundary is the caller's shell wrapper (rb-347), never
    inside this aggregator.
    """
    if (raw or "").lstrip() == "null":
        return None  # canonical wm-slot empty-state literal
    return _rt.tolerant_decode_aggregate(f"blocker-recheck: {slot}", raw)


def _wm_read_blockers() -> list:
    """Read the known_blockers slot from working memory.

    Uses the daemon via _rt (wm.py read CLI was deleted in the
    2026-05-14 cutover; _rt.wm_read is the canonical Python -> daemon
    client). Daemon-only: no CLI fallback.

    Parse path is g-115-766-tolerant via `_tolerant_decode` — see that
    helper for the contract. Applied via g-115-797-A2 (bravo audit
    catalog row 3) — replaces the prior silent-collapse
    `except json.JSONDecodeError: return []` that would hide corruption
    behind a "no blockers to recheck" no-op and freeze every aged blocker
    indefinitely (blocker recovery loop never re-evaluates).

    RtError handling — guard-383 fatal symmetry (rb-987):
    `_wm_read_blockers()` feeds the blocker-recheck loop at line 184.
    Per guard-383, a silent `return []` on per-source error writes a
    complete-looking lie to consumers (zero blockers instead of "wm
    unreachable"). The exemplar consolidation-health.py corrected this
    in commit 28a3b7a; A3 sibling (precondition-defer-recheck.py) and
    A4 (parent-supersession-sweep.py) followed the corrected pattern.
    A2 matches the corrected exemplar / A3 / A4 — NOT A1
    (defer-recheck.py)'s pre-correction silent-return.
    The single fail-open boundary is the caller's shell wrapper
    `|| echo WARN` (rb-347), never inside this reader.
    """
    try:
        out = _rt.wm_read(slot="known_blockers", as_json=True)
    except _rt.RtError as e:
        print(f"[blocker-recheck] known_blockers wm_read failed: {e.body or e}",
              file=sys.stderr)
        sys.exit(1)  # guard-383: source error fatal — single fail-open boundary is wrapper
    data = _tolerant_decode("known_blockers", out)
    if data is None:
        return []
    return data if isinstance(data, list) else []


def _read_goal_blocker_refs() -> list:
    """Blockers that live on GOAL RECORDS, normalized to the known_blockers shape.

    The durable half of the two populations described in the module docstring.
    Returns entries carrying `_origin: "goal"` so main() can (a) keep them out of
    the `known_blockers` write-back and (b) refuse to auto-clear them.

    READ THE FULL RECORD, NEVER THE QUERY PROJECTION (guard-1242).
    `aspirations-query.sh --goal-status blocked` returns only
    [asp_id, category, goal_id, source, status, title] — `blocker_ref` is ABSENT
    from that projection, so building this population from it would yield a
    guaranteed empty result that looks exactly like a clean queue. That is the
    same vacuous-zero the widening exists to remove, so the query path would
    re-create the defect one layer up. `_rt.aspirations_read` returns full goal
    records; use it.

    Live stores only, deliberately. A goal inside a COMPLETED+archived aspiration
    is absent from these reads (guard-1555), and that is correct here: a blocker
    on finished work is moot. This is a live-blocker sweep, not an audit.

    guard-961: `blocker_ref` must be a DICT before any field access. A bare
    string or list is a malformed record, not a blocker — skip it rather than
    raise, matching the read-side discipline `_is_producer_managed` already uses.

    guard-383 symmetry with `_wm_read_blockers`: a source read failure is FATAL,
    never a silent `return []`. A silent empty list here would write the very
    "zero blockers" lie this function exists to eliminate. The single fail-open
    boundary stays the shell wrapper (rb-347).
    """
    try:
        from _goal_census import TERMINAL_STATUSES  # leaf module, drift-tested
    except Exception:                                # pragma: no cover - import guard
        TERMINAL_STATUSES = frozenset(
            {"completed", "decomposed", "expired", "skipped", "superseded"})
    # NOTE: "retired" is not in the canonical set, so a retired goal's blocker_ref
    # IS counted. Deliberate: a local superset would silently diverge from the
    # drift-tested constant. The over-count is visible instead — every detail
    # record carries `goal_status`, so a reader can see it.
    out = []
    for gsource in ("world", "agent"):
        try:
            raw = _rt.aspirations_read(source=gsource, active=True)
        except _rt.RtError as e:
            print(f"[blocker-recheck] aspirations_read({gsource}) failed: "
                  f"{e.body or e}", file=sys.stderr)
            sys.exit(1)
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError as e:
            print(f"[blocker-recheck] aspirations_read({gsource}) returned "
                  f"undecodable JSON: {e}", file=sys.stderr)
            sys.exit(1)
        # Both envelope shapes are live: the documented {"aspirations": [...]}
        # and a BARE list (measured 2026-08-01 on source=world, active=1).
        # Handle both — keying on only one silently yields zero aspirations.
        asps = data.get("aspirations") if isinstance(data, dict) else data
        for a in (asps or []):
            if not isinstance(a, dict):
                continue
            for g in (a.get("goals") or []):
                if not isinstance(g, dict):
                    continue
                if g.get("status") in TERMINAL_STATUSES:
                    continue
                ref = g.get("blocker_ref")
                if not isinstance(ref, dict):    # guard-961
                    continue
                gid = g.get("id")
                out.append({
                    # Normalized to the known_blockers shape so every downstream
                    # filter (_blocker_id, _age_hours, HUMAN_ONLY_BLOCKER_TYPES,
                    # _is_producer_managed, is_user_routed) applies unchanged.
                    "blocker_id": ref.get("external_id") or f"goalref:{gid}",
                    "type": ref.get("type"),
                    "reason": ref.get("why") or ref.get("reason"),
                    "detected_at": ref.get("created_at"),
                    "participants": g.get("participants"),
                    "source": "goal-blocker-ref",
                    "blocker_ref": ref,
                    "_origin": "goal",
                    "_goal_id": gid,
                    "_goal_source": gsource,
                    "_goal_status": g.get("status"),
                    "_intended_agent": g.get("intended_agent"),
                })
    return out


def _wm_set_blockers(blockers: list) -> bool:
    rc, _, _ = _py(
        [str(SCRIPT_DIR / "wm.py"), "set", "known_blockers"],
        input_text=json.dumps(blockers),
    )
    return rc == 0


def _age_hours(detected_at):
    """Compute hours since detected_at. Returns None if missing or unparsable.

    Broad except is intentional: detected_at may be any JSON-loadable value
    (dict, list, int, None). If we cannot determine age, the caller skips
    the blocker — we refuse to act on uncertain state. JSON-safe: None
    serializes cleanly; float('inf') does not.
    """
    if not detected_at:
        return None
    try:
        t = parse_naive_iso(detected_at)
    except Exception:
        return None
    # parse_naive_iso RETURNS None for an unparsable value rather than raising,
    # so the except above never fires for e.g. a dict/list/int stamp — and the
    # subtraction below then raised an uncaught TypeError, aborting the entire
    # sweep instead of skipping the one bad blocker. That contradicted this
    # function's own contract ("if we cannot determine age, the caller skips
    # the blocker"). Latent while detected_at was universally absent (the
    # `if not detected_at` early return caught every real call); reachable once
    #  made the created_at alias readable. Surfaced by
    # test_reader_returns_none_when_both_absent.
    if t is None:
        return None
    return (dt.datetime.now() - t).total_seconds() / 3600.0


def _blocker_id(b: dict) -> str | None:
    """Blocker identity, tolerating the legacy key ().

    `blocker_id` is the documented schema key (handoff-working-memory.md:152)
    and is what infra-health.py and every other reader use. create-blocker.py
    historically wrote `id` instead, so blockers already in agents' working
    memories carry only that. Without the alias, an aged legacy blocker that
    reaches the recheck reports blocker_id=null in its detail record and its
    failure_reason fallback collapses to the empty string -- the recheck runs
    but its output cannot be traced back to a blocker. Bounded migration shim;
    removable once fleet blockers have cycled.
    """
    return b.get("blocker_id") or b.get("id")


def _run_gate(failure_reason: str, intended: str) -> dict:
    rc, out, err = _py([
        str(SCRIPT_DIR / "capability-gate.py"),
        "--failure-reason", failure_reason,
        "--intended-participants", intended,
        "--output", "json",
    ])
    try:
        return json.loads(out)
    except Exception:
        # Gate invocation broken — visible on stderr so this doesn't rot silently.
        print(f"[blocker-recheck] capability-gate invocation failed (rc={rc}): "
              f"{err.strip() or out.strip() or '(no output)'}", file=sys.stderr)
        return {"match_count": 0, "would_block": False, "error": "gate invocation failed"}


def _add_investigate_goal(aspiration_id: str, blocker: dict, gate_result: dict) -> str:
    """Create an Investigate goal so the retrieval lapse gets learned from."""
    match = (gate_result.get("matches") or [{}])[0]
    matched_skill = match.get("skill") or (match.get("row") or "")[:60]
    matched_kw = match.get("matched_keyword", "")
    title = f"Investigate: capability '{matched_skill}' missed at blocker creation"
    description = (
        f"The capability gate re-examined blocker {blocker.get('blocker_id')} "
        f"after it aged past threshold and found an agent-provisionable "
        f"capability that was overlooked at creation time.\n\n"
        f"Matched capability: {matched_skill} (keyword: {matched_kw})\n"
        f"Original failure_reason: {blocker.get('reason') or blocker.get('diagnostic_context', {}).get('failure_reason', '(unknown)')}\n\n"
        f"Analyze: why did the CREATE_BLOCKER Step 2.5 capability scan miss this? "
        f"Was the failure_reason wording ambiguous? Was the capability registry "
        f"stale at that time? Extract a guardrail or update the rule if pattern-recurring."
    )
    goal_record = {
        "title": title,
        "description": description,
        "priority": "MEDIUM",
        "participants": ["agent"],
        "category": "framework-maintenance",
        "tags": ["capability-miss", "retrieval-lapse", "learning"],
        # origin-signal-gate: the aged blocker IS the triggering signal.
        "origin_signal": f"investigate:blocker-{blocker.get('blocker_id') or 'unknown'}",
    }
    # aspirations.py add-goal CLI was deleted in the 2026-05-14 cutover;
    # _rt.aspirations_add_goal is the canonical Python -> daemon replacement.
    # Daemon-only: no CLI fallback.
    # source RESOLVED from where the aspiration actually lives, not hardcoded
    # "world" ( sweep). In a deployment whose framework-hygiene home is
    #  that id lives in the AGENT queue — filing it with source="world"
    # reproduces aspiration_not_found in a new costume: the id is right, the store
    # is wrong. This is the exact trap _escalation_target.source_flag exists to
    # close, and the old help text's claim that the target "must exist in
    # world/aspirations.jsonl (not an agent-local queue)" is what made hardcoding
    # it look safe.
    try:
        from _paths import WORLD_DIR as _WD2, AGENT_DIR as _AD2  # type: ignore
        from _escalation_target import source_flag as _asp_source
        _src = _asp_source(aspiration_id, _WD2, _AD2)
    except Exception:
        _src = "world"          # pre-fix behaviour; never worse than before
    try:
        result = _rt.aspirations_add_goal(aspiration_id, goal_record, source=_src)
    except _rt.RtError as e:
        return f"<add-goal-failed:{(e.body or str(e)).strip() or 'no detail'}>"
    goal_id = result.get("goal_id") or result.get("id")
    return (goal_id or "<unknown-id>")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Re-examine aged blockers against the capability gate."
    )
    ap.add_argument("--max-age-hours", type=float, default=4.0,
                    help="Blockers older than this are rechecked. Default 4h.")
    ap.add_argument("--apply", action="store_true",
                    help="Actually clear blockers + create Investigate goals. Default: dry-run.")
    # Default RESOLVED, not hardcoded ( sweep). This was
    # default="", the third instance of the same defect fixed in
    # cadence-stale-canary and stale-sentinel-canary:  is THIS
    # deployment's framework queue and exists in neither queue downstream, so
    # every Investigate filed there died aspiration_not_found. Found by sweeping
    # for siblings of the original bug rather than waiting for this one to
    # surface on its own.
    # NOTE the old help text asserted "Must exist in world/aspirations.jsonl (not
    # an agent-local queue)" — that assumption is what made the hardcode look
    # safe, and it is wrong in deployments whose framework-hygiene home IS the
    # agent queue. The resolver returns the matching source.
    # WORLD_DIR / AGENT_DIR are NOT module globals here (this script only defines
    # SCRIPT_DIR / CORE_ROOT / PROJECT_ROOT and reaches state via the daemon
    # client), so import them explicitly. The first version of this fix referenced
    # them bare, raised NameError, and the except-branch silently returned
    # "" — a non-fix that passed syntax, ran without error, and reproduced
    # the exact bug. Caught only by checking what the default RESOLVED to instead
    # of trusting "no crash". A fail-open around a name error will hide a
    # non-functioning fix every time.
    try:
        from _paths import WORLD_DIR as _WD, AGENT_DIR as _AD  # type: ignore
        from _escalation_target import resolve as _resolve_asp
        _default_asp, _asp_via = _resolve_asp(CORE_ROOT, _WD, _AD)
    except Exception as _exc:
        _default_asp, _asp_via = "asp-115", f"fallback:{type(_exc).__name__}"
    ap.add_argument("--investigate-aspiration", default=_default_asp,
                    help="Aspiration ID to add Investigate goals under. Default is "
                         f"RESOLVED to one that exists: {_default_asp} ({_asp_via}). "
                         "Override via stale_cadence.escalation_aspiration or this flag.")
    args = ap.parse_args(argv)

    wm_blockers = _wm_read_blockers()
    goal_blockers = _read_goal_blocker_refs()
    # WM first so its entries keep their original write-back order (the
    # partition below relies on identity, not position, but a stable order
    # keeps the emitted slot byte-comparable across runs).
    blockers = wm_blockers + goal_blockers
    report = {
        "agent": os.environ.get("MIND_AGENT", ""),
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "total_blockers": len(blockers),
        # DECLARE THE POPULATION, do not just count it. `total_blockers: 0` was
        # honest and unreadable because it never said WHICH population it had
        # enumerated (enumerator-all-clear-boundary). These two always appear,
        # including when 0, so "the goal half found nothing" stays distinguishable
        # from "this build has no goal half".
        "wm_blockers": len(wm_blockers),
        "goal_blockers": len(goal_blockers),
        "rechecked": 0,
        "matches_found": 0,
        "cleared": 0,
        # Always present, including when 0 — a counter that appears only when
        # non-zero gives consumers an unstable shape and makes "the exemption
        # never fired" indistinguishable from "this build has no exemption".
        "producer_managed_exempt": 0,
        # Same stable-shape rule as producer_managed_exempt above: always present,
        # so "no goal-sourced blocker matched" never reads as "this build has no
        # goal-sourced path".
        "goal_sourced_report_only": 0,
        # WHY THE ZERO. `rechecked: 0` against a non-zero total_blockers is the
        # same unreadable all-clear this goal was filed about, just one field to
        # the right: a reader cannot tell "every blocker was correctly filtered"
        # from "the filters are silently broken". Every `continue` below tallies
        # its reason here, so the skips always sum to (total_blockers - rechecked)
        # and the zero explains itself. Keys are fixed so the shape is stable.
        "skipped": {
            "malformed": 0,        # not a dict
            "already_resolved": 0,  # carries a resolution
            "human_only_type": 0,   # HUMAN_ONLY_BLOCKER_TYPES — never auto-clears
            "producer_managed": 0,  # mirrors producer_managed_exempt
            "not_user_routed": 0,   # pure [agent] — no creation-time lapse to catch
            "unaged": 0,            # no parsable detected_at/created_at
            "below_age_threshold": 0,
        },
        "investigate_goals_created": [],
        "actions_taken": "dry-run" if not args.apply else "apply",
        "details": [],
    }

    def _skip(reason):
        report["skipped"][reason] += 1

    updated = []
    for b in blockers:
        # Only re-examine blockers that went to user (or hybrid) and are unresolved
        if not isinstance(b, dict):
            _skip("malformed")
            updated.append(b)
            continue
        if b.get("resolution"):
            _skip("already_resolved")
            updated.append(b)
            continue
        # Structural type filter — human-only blocker categories NEVER auto-clear.
        # A security-trust rotation, credential issuance, or hardware action
        # cannot be agent-provisioned regardless of keyword-match score. The
        # correct path for these is to surface them via pending-question
        # re-raise, not to clear them programmatically.
        if b.get("type") in HUMAN_ONLY_BLOCKER_TYPES:
            _skip("human_only_type")
            updated.append(b)
            continue
        # Producer-managed blockers NEVER auto-clear here — see
        # PRODUCER_MANAGED_SOURCE_PREFIXES. Counted, not silent: a sweep that
        # skips work without saying so reads as "nothing matched", which is the
        # same output a genuinely clean run produces.
        if _is_producer_managed(b):
            report["producer_managed_exempt"] = report.get("producer_managed_exempt", 0) + 1
            _skip("producer_managed")
            updated.append(b)
            continue
        participants = b.get("participants") or []
        # Recheck any blocker that went to [user], [agent,user], or has no
        # participants field (older/legacy blockers). Pure [agent] blockers
        # are already agent-routed so there's no retrieval lapse to catch.
        is_user_routed = (participants == ["user"]
                          or set(participants) == {"agent", "user"}
                          or not participants)
        if not is_user_routed:
            _skip("not_user_routed")
            updated.append(b)
            continue

        # LEGACY-SHAPE TOLERANCE (). `detected_at` is the documented
        # schema key and is now emitted by both writers, but blockers created
        # BEFORE that fix are live in agents' working memories carrying only
        # create-blocker.py's `created_at`. Without this alias they stay
        # permanently unreachable: age is None -> `continue` at EVERY age, so
        # the recheck sweep silently reports rechecked=0 forever rather than
        # "not yet aged". Same shape as unblock-intake-probe.py:469. The two
        # keys are aliases for one fact (both stamped at record creation), not
        # competing sources -- this is a bounded migration shim, removable once
        # fleet blockers have cycled, NOT a permanent fallback chain.
        age = _age_hours(b.get("detected_at") or b.get("created_at"))
        # age is None if BOTH keys are missing or unparsable — skip rather
        # than guess. age_hours below threshold — not yet aged, also skip.
        if age is None or age < args.max_age_hours:
            # Split the two, because they mean opposite things. "below threshold"
            # is a WAIT — this blocker will become eligible on its own. "unaged" is
            # a PERMANENT exclusion: with no parsable stamp the age test can never
            # pass, at any threshold, ever. A goal-sourced blocker_ref makes this
            # reachable — `created_at` is OPTIONAL in the blocker_ref schema, and a
            # live one lacking it was measured 2026-08-01 (). Do NOT
            # synthesize an age from expires_at to "fix" it: _age_hours' contract is
            # to refuse uncertain state, and a guessed age feeds the keyword-match
            # clear path. Counting it is the fix — a permanent exclusion that is
            # visible is a finding; one that is silent is this goal all over again.
            _skip("unaged" if age is None else "below_age_threshold")
            updated.append(b)
            continue

        report["rechecked"] += 1
        failure_reason = (
            b.get("reason")                       # infra-health.py streak alerts
            # create-blocker.py stores the narrative at TOP LEVEL as
            # `failure_reason`, not under diagnostic_context (which is
            # caller-supplied JSON and carries the key only by luck). This
            # rung was missing, so for every canonically-created blocker the
            # chain fell through to the ID STRING and fed *that* to the
            # capability gate below -- a meaningless verdict on an
            # identifier rather than a re-probe of the actual failure.
            # Unreachable until  fixed the age filter above; fixing
            # only the filter would have made the sweep run and still decide
            # on garbage input.
            or b.get("failure_reason")
            or (b.get("diagnostic_context") or {}).get("failure_reason")
            or _blocker_id(b) or ""
        )
        gate = _run_gate(failure_reason, "user")
        first_match = (gate.get("matches") or [{}])[0] if gate.get("matches") else {}
        top_match = first_match.get("skill") or (first_match.get("row") or "")[:80] or None
        detail = {
            "blocker_id": _blocker_id(b),
            "age_hours": round(age, 1),
            "match_count": gate.get("match_count", 0),
            "would_block": gate.get("would_block", False),
            "top_match": top_match,
            "matched_keyword": first_match.get("matched_keyword"),
            # Provenance is part of the finding, not decoration: a reader must be
            # able to tell which population a row came from without re-deriving it.
            "origin": b.get("_origin") or "wm",
        }
        if b.get("_origin") == "goal":
            detail["goal_id"] = b.get("_goal_id")
            detail["goal_source"] = b.get("_goal_source")
            detail["goal_status"] = b.get("_goal_status")
            detail["intended_agent"] = b.get("_intended_agent")

        if gate.get("would_block"):
            report["matches_found"] += 1
            if b.get("_origin") == "goal":
                # REPORT-ONLY by design — see the module docstring. Clearing this
                # would mutate a GOAL (often another agent's), on a keyword match
                # with no probe behind it (guard-1978). Surface it and let a reader
                # decide; `blocked-signal-resolution-check` owns this population's
                # resolution question and is likewise detective-only.
                report["goal_sourced_report_only"] = (
                    report.get("goal_sourced_report_only", 0) + 1)
                detail["action"] = (
                    "report-only (goal-sourced): capability gate matched an "
                    "agent-provisionable skill — re-derive by hand; this sweep "
                    "never mutates goal records"
                )
            elif args.apply:
                # INVARIANT: create the Investigate goal FIRST, THEN clear the
                # blocker. If reversed, a failed add-goal silently loses both
                # the blocker (so the user never sees the issue again) AND
                # the learning signal (no goal spawned to analyze the lapse).
                # Do not reorder.
                asp_id = args.investigate_aspiration
                goal_id = _add_investigate_goal(asp_id, b, gate)
                if goal_id.startswith("<add-goal-failed"):
                    detail["action"] = f"add-goal failed, blocker NOT cleared ({goal_id})"
                else:
                    top = (gate.get("matches") or [{}])[0]
                    b["resolution"] = {
                        "method": "capability-gate-recheck",
                        "cleared_at": dt.datetime.now().isoformat(timespec="seconds"),
                        "matched_capability": top.get("skill") or (top.get("row") or "")[:80] or None,
                        "note": "Blocker auto-cleared: capability-gate matched an agent-provisionable skill that was overlooked at creation time.",
                        "investigate_goal": goal_id,
                    }
                    report["cleared"] += 1
                    report["investigate_goals_created"].append({
                        "blocker_id": _blocker_id(b),
                        "goal_id": goal_id,
                        "aspiration_id": asp_id,
                    })
                    detail["action"] = "cleared + investigate goal created"
            else:
                detail["action"] = "would clear (dry-run)"
        else:
            detail["action"] = "legitimate user-routing; leaving as-is"

        report["details"].append(detail)
        updated.append(b)

    if args.apply and report["cleared"] > 0:
        # PARTITION — the one line that makes the widening safe. `updated`
        # accumulates BOTH populations, but only the WM half may ever be written
        # back to the WM slot. Without this filter the sweep would inject
        # goal-derived synthetic entries into `known_blockers`, inventing blockers
        # nobody created and corrupting every downstream consumer that iterates
        # that slot (Phase 0.5b re-probe, proactive escalation, quiescence-gate).
        # Filtering HERE rather than at each append site keeps the guarantee at a
        # single provable chokepoint instead of spread across six branches.
        _wm_set_blockers([
            x for x in updated
            if not (isinstance(x, dict) and x.get("_origin") == "goal")
        ])
        # Wake-on-signal (): tells interruptible-sleep.sh to exit 2 and
        # break backoff early when at least one blocker clears. Non-blocking —
        # the signal is purely advisory; if the script is missing or fails the
        # normal backoff timer still fires.
        # Windows path-separator fix ( audit): invoke via bash with
        # .as_posix() — direct .sh execution fails on Windows (no shebang
        # follow), AND a Windows-backslash path would be stripped by bash's
        # escape interpretation. Wrapped in except Exception: pass so the
        # advisory signal never blocks the script.
        try:
            from _runtime_bash import BASH  # rb-1472: not bare "bash"
            subprocess.run(
                [BASH,
                 (SCRIPT_DIR / "session-signal-set.sh").as_posix(),
                 "blocker-cleared"],
                check=False,
                timeout=5,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
