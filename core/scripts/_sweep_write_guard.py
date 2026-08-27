"""Shared refusal POLICY for scan-then-write sweeps over shared goal records.

WHAT THIS OWNS, AND WHAT IT DELIBERATELY DOES NOT (read this before adding a
parameter). This module is a PURE function of an already-performed re-read:
`(goal, provenance) -> reason-or-None`. It performs no I/O, imports no backend,
and knows nothing about how the caller reached the store of record.

That split is the whole design, and it is not stylistic:

  * POLICY is shared, because "may this sweep overwrite this record?" is ONE
    question and three sweeps answering it separately is three predicates that
    drift the first time one of them learns something (guard-2783). The subtle
    parts live here: an unverifiable read is not permission, completion
    provenance outranks a re-openable-looking status, a live CLAIM outranks
    both, and every refusal is fail-CLOSED.
  * COLLABORATORS ARE PASSED IN, NEVER IMPORTED HERE. `reread_goal_authoritative`
    takes `read_aspirations` and `is_owncloud` as required keyword arguments so
    each caller supplies its OWN module's copies. That is not ceremony: every
    caller keeps a one-line `_reread_goal_authoritative` wrapper whose body
    resolves those two names as module globals at CALL time, so
    `monkeypatch.setattr(mod, "_read_aspirations", ...)` and
    `monkeypatch.setattr(mod, "_is_owncloud_backend", ...)` keep working
    untouched. Import them here instead and the patches would still APPLY while
    the internal call resolved through THIS module's namespace — the stub
    silently stops being consulted and the suite stays green (guard-2385: grep
    the test tree for fakes before moving a function). The 21 tests in
    test_unblock_parent_lost_update_guard.py patch exactly that way and were
    re-run unchanged, plus mutation-proofed through this seam, to prove it.

Origin: g-115-6332. The lost-update race was measured on
unblock-parent-status-sweep (a candidate selected at scan time was written 3
seconds after another box completed it, destroying a 6,270-char outcome_note
and flipping a finished goal out of terminal). The guard was written there
first; this module is that guard's policy half, extracted so the sibling
scan-then-write sweeps can share it rather than re-derive it.
"""

import json
import sys
from pathlib import Path

from _paths import WORLD_DIR  # noqa: E402

# `_team_state` is the SSOT for the provenance vocabulary. Imported, never
# re-declared: a local copy of these strings would compare equal today and
# drift the first time one is renamed, in the direction that makes a
# mirror-sourced read look authoritative — the exact failure guard-1753 names.
#
# NOTE `_is_owncloud_backend` is deliberately NOT imported here — see the
# collaborator bullet in the header. Callers pass their own.
from _team_state import (  # noqa: E402
    PROV_AUTHORITATIVE,
    PROV_LOCAL_MIRROR,
    PROV_NONE,
)

__all__ = [
    "ACTIVE_CLAIM_FIELDS",
    "COMPLETION_PROVENANCE_FIELDS",
    "DEFAULT_OPEN_STATUSES",
    "reread_goal_authoritative",
    "stale_candidate_reason",
    "PROV_AUTHORITATIVE",
    "PROV_LOCAL_MIRROR",
    "PROV_NONE",
]


def _find_goal(items, goal_id):
    """Locate a goal across BOTH aspiration shapes these sweeps handle.

    `_read_aspirations` yields (aspiration_dict, source_str) TUPLES, while the
    own-cloud branch parses raw JSONL into bare dicts. Handling one and not the
    other is not a cosmetic slip: it raises AttributeError, which reads as "goal
    not found" and therefore as a REFUSAL — a wedge that looks exactly like the
    guard working correctly.
    """
    for entry in items or []:
        asp = entry[0] if isinstance(entry, tuple) else entry
        if not isinstance(asp, dict):
            continue
        for g in (asp.get("goals") or []):
            if g.get("id") == goal_id:
                return g
    return None


def reread_goal_authoritative(source, goal_id, *, read_aspirations, is_owncloud,
                              label="sweep"):
    """``(goal, provenance)`` — the goal record read from the STORE OF RECORD.

    WHY NOT A PLAIN RE-READ, which is what "re-check before writing" would
    normally mean. On own-cloud the local `aspirations.jsonl` is a read-through
    CACHE, so a local re-read returns whatever this box last pulled. That is not
    a narrower window than the scan — it is the SAME stale bytes the scan saw,
    and it is precisely how the g-115-6332 incident happened: alpha's sweep
    listed g-115-6326 as a live candidate at 14:33:41 *because* its mirror never
    carried bravo's 14:33:38 completion. A local re-check would have passed and
    the write would have proceeded unchanged.

    A lock does not help either, and this is the part most likely to be
    "simplified" away by a later reader: the knowledge-tree node
    `owncloud-write-path-loss-lanes` records that refresh-no_clobber returns
    stale local INSIDE the lock. The lock makes the write atomic; it says
    nothing about whether the DECISION was current. So the guard has to reach
    the store of record, not merely re-read under protection.

    Returns provenance deliberately: a fail-open reader that cannot distinguish
    "reached the store and found nothing" from "could not reach the store" makes
    its own blindness unobservable to every caller (guard-1753). Here that
    distinction decides whether a destructive write is allowed at all.
    """
    live_path = Path(WORLD_DIR) / "aspirations.jsonl" if source == "world" else None

    if live_path is None or not is_owncloud():
        # No sync layer (or a non-world source): the local file IS the store of
        # record, so a local read is authoritative rather than a mirror read.
        # Treating it as a mirror would refuse every write on a local backend.
        try:
            g = _find_goal(read_aspirations(source), goal_id)
        except Exception as e:  # noqa: BLE001 — LOUD, never silent
            # A swallowed error here becomes a permanent refusal, i.e. a sweep
            # that reports success while doing nothing. Fail-safe on the WRITE,
            # never silent on the CAUSE.
            print(f"[{label}] local authoritative read of {goal_id} raised "
                  f"{type(e).__name__}: {e}", file=sys.stderr)
            return (None, PROV_NONE)
        return (g, PROV_AUTHORITATIVE if g is not None else PROV_NONE)

    try:
        from owncloud_backend import OwnCloudBackend
        be = OwnCloudBackend.from_env()
        raw = be.read_text(str(live_path), force_fresh=True)
        items = [json.loads(ln) for ln in (raw or "").splitlines() if ln.strip()]
        g = _find_goal(items, goal_id)
        if g is not None:
            return (g, PROV_AUTHORITATIVE)
        # Reached the store and the goal is genuinely absent from it.
        return (None, PROV_NONE)
    except Exception as e:  # noqa: BLE001 — fail-open to the mirror, but SAY SO
        print(f"[{label}] authoritative re-read of {goal_id} failed "
              f"({type(e).__name__}: {e}); falling back to local mirror",
              file=sys.stderr)
    try:
        g = _find_goal(read_aspirations(source), goal_id)
    except Exception:  # noqa: BLE001
        return (None, PROV_NONE)
    return (g, PROV_LOCAL_MIRROR if g is not None else PROV_NONE)

#: Fields that only a real close writes. Their presence is completion
#: provenance even when `status` itself looks re-openable, which matters
#: because the damage signature is exactly `status=<terminal-ish> AND
#: completed_by AND outcome_class` — a record a sweep already overwrote.
COMPLETION_PROVENANCE_FIELDS = ("completed_by", "completed_by_sid", "outcome_class")

#: Fields that prove a claim is held RIGHT NOW. Only `claimed_by_sid` qualifies,
#: and the distinction is load-bearing rather than stylistic: `aspirations.py`
#: pops claimed_by / claimed_at / claimed_by_sid TOGETHER on release and on
#: close, so a non-null sid means a claim is live at this instant — whereas
#: `started` / `executed_by` / `executed_by_sid` SURVIVE a release and are
#: therefore execution HISTORY, not ownership.
#:
#: Measured 2026-08-24 on the world store, 2,193 open goals ():
#: claimed_by_sid present on 4, started on 231 (10.5%), executed_by_sid on 205
#: (9.3%); on TERMINAL goals started/executed_by_sid persist at ~78%. Keying
#: this refusal on the execution fields — which is what the originating report
#: proposed — would have disarmed all three sweeps across ~10% of the open queue
#: permanently, so the narrow field is the correct one AND the safe one.
#:
#: guard-4434 is why the SID is named rather than `claimed_by`: an ownership
#: test that mentions claimed_by and not the sid has a hole by construction,
#: because a record whose NAME was cleared while its sid survived reads as
#: unowned to every such predicate.
ACTIVE_CLAIM_FIELDS = ("claimed_by_sid",)

#: The statuses a scan-then-write sweep may legitimately act on. A sweep whose
#: candidate predicate is narrower passes its own tuple; none may pass a WIDER
#: one that admits a terminal status, which is the case this guard exists for.
DEFAULT_OPEN_STATUSES = ("pending", "in-progress")


def stale_candidate_reason(goal, provenance, *, open_statuses=DEFAULT_OPEN_STATUSES):
    """``None`` when the write may proceed, else the refusal reason.

    `goal` / `provenance` are the caller's authoritative re-read, performed
    immediately before the write. The caller owns that read; this function only
    judges it.

    FAIL-CLOSED, and the asymmetry is the whole argument. Proceeding on a stale
    decision DESTROYS a completion record. Refusing costs one deferred hygiene
    action: the goal stays open, so it is still a candidate on the next run and
    the sweep self-heals the moment the store is readable again. An unverifiable
    read is therefore treated as ABSENT rather than as permission
    (`.claude/rules/archive-before-delete.md` step 2 applies the identical rule
    to an unreadable recovery layer).
    """
    if provenance == PROV_LOCAL_MIRROR:
        return ("store of record unreachable — the local mirror cannot prove "
                "this goal is still open, and an unverifiable read is not "
                "permission to overwrite a possible completion")
    if goal is None:
        return "goal not found in the store of record on re-read"

    status = (goal.get("status") or "")
    if status not in open_statuses:
        return (f"status changed to {status!r} between scan and apply — "
                f"another box moved this goal while the sweep was running")

    present = [f for f in COMPLETION_PROVENANCE_FIELDS if goal.get(f)]
    if present:
        return (f"completion provenance present ({', '.join(present)}) despite "
                f"status={status!r} — a close is in flight or already landed")

    claimed = [f for f in ACTIVE_CLAIM_FIELDS if goal.get(f)]
    if claimed:
        holder = goal.get("claimed_by") or "an unnamed holder"
        return (f"claim in flight ({', '.join(claimed)} set, claimed_by="
                f"{holder!r}) — an agent is executing this goal right now, and "
                f"status alone cannot show that: the claim path writes "
                f"claimed_by_sid/started/executed_by_sid but leaves status at "
                f"'pending', so the one field a status-keyed sweep consults is "
                f"exactly the one a live claim does not move")
    return None
