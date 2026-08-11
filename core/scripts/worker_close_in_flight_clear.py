#!/usr/bin/env python3
"""Clear team-state `in_flight` at a worker Body's GENUINE close -- iff this Body set it.

WHY THIS EXISTS (g-306-132-d, measured 2026-08-03)
--------------------------------------------------
A worker Body claims through the SAME contract the reducer uses
(`aspirations-claim.sh <goal-id> <agent>`, worker-loop Phase 2), and that claim
WRITES `team-state.agent_status.<agent>.in_flight`. But the worker loop then
STOPS at Phase 4 by design -- it never runs verify/state-update/learning-gate --
and `iteration-close.sh do_verify` Step 3 (L629) is the ONLY place the normal
close path calls `team-state-clear-in-flight.sh`. So a worker sets in_flight and
nothing on its own close path ever clears it.

The two mechanisms that might have covered it do not:

  * `recovery-gate.sh` DOES clear in_flight -- via `session-manifest-clear.sh`
    (L273 -> L82-107, rb-671). That rail is present and correct; do NOT add a
    second one. But it fires only behind the 6-condition zombie AND-gate, which
    a cleanly-closed worker never trips.
  * `stranded-claim-sweep.py` clears in_flight when it matches a stranded claim,
    but it enumerates `status=in-progress` claims only. A worker that COMPLETED
    its goal leaves a terminal-status goal the sweep cannot see, so its in_flight
    can stay stale indefinitely.

A stale in_flight is not cosmetic: `check-team-state-before-silent.md` makes that
field the fleet's liveness signal, and every partner's selector drops candidate
goals a stale row claims are in progress (guard-2305).

WHY THE OWNERSHIP TEST IS MANDATORY
-----------------------------------
`in_flight` is keyed by AGENT NAME, not by Body or session -- the live schema is
`{goal_id, title, claimed_at, phase}` with NO sid (verified against live rows
2026-08-03). A worker and its reducer share one agent name and therefore ONE
in_flight row. So an UNCONDITIONAL clear at worker close would blank the row of a
REDUCER that is legitimately mid-execution on a different goal -- the same defect
inverted, and strictly worse than the one being fixed (it makes a live agent look
idle rather than a dead one look busy).

The discriminator is `claimed_by_sid`, stamped on the goal record by
`aspirations-claim.sh` (g-115-3176) and the only per-INSTANCE identity available:
neither `in_flight` itself, nor `iteration-checkpoint.json` (agent-wide
`session/`, singular), nor the Body WM records which Body claimed. We clear ONLY
when the goal named by the live in_flight row was claimed by THIS process's SID.

Fail-open in EVERY direction, and asymmetric on purpose: an unreadable signal
LEAVES the row alone. A missed clear is the pre-existing bug and self-heals on
the reducer's next sweep or close; a wrong clear silently misroutes the whole
fleet's selection. Always exits 0 -- this runs at a turn-end and must never
block one.

guard-2305: the clear goes through the purpose-built path
(`POST /v1/team-state/clear-in-flight`, the endpoint `team-state-clear-in-flight.sh`
itself calls), never the generic `team-state-update.sh --field in_flight --value null`
dotpath setter, which prints "Updated in_flight" over a NO-OP. The write is
read back before it is reported as done.

Usage:
    py -3 core/scripts/worker-close-in-flight-clear.py --agent <name> [--sid <sid>]

Emits one JSON object on stdout:
    {"verdict": "...", "agent": ..., "goal_id": ..., "reason": ...}

verdicts:
    absent        -- no in_flight row; nothing to do
    not-ours      -- in_flight belongs to another Body/instance; deliberately left
    unknown-owner -- claimed_by_sid or local SID unreadable; left alone (fail-safe)
    cleared       -- ours, cleared, and the read-back confirms it is gone
    raced         -- ours at the ownership read, but the row moved to another
                     goal before the write; the compare-and-swap declined and
                     the foreign row survived (the g-306-137 case)
    clear-failed  -- clear attempted but the read-back still shows a row
    clear-unverified -- the row is no longer ours, but the endpoint's body would
                     not parse, so no CAS verdict was ever read. NOT a failure
                     and NOT a proven success: the read-back alone cannot say
                     WHICH row was cleared (g-306-163)
    error         -- any unexpected failure; row left alone

WHY THE OWNERSHIP TEST ALONE WAS NOT A GUARD (g-306-137, guard-2474 clause 2)
----------------------------------------------------------------------------
The first version of this file read in_flight, resolved ownership across a full
active-aspirations dump, then called an endpoint that took only ?agent= and
blanked whatever row was present at call time. Nothing held a lock across that
span, so a sibling claim landing inside the window was destroyed anyway -- and
the read-back could not see it, because it asked only whether A row was gone,
never whether the RIGHT one was cleared (after a race `still` is None, which
read as success). The clear now carries `if_goal`, re-checked inside the row
lock the endpoint already holds, so the check and the act are on one subject.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _rt  # canonical Python -> daemon client  # noqa: E402

# Marker key for a 2xx whose body would not parse. Underscore-prefixed so it can
# never collide with a real endpoint field ().
UNPARSEABLE = "_unparseable_body"


def _decode_team_state_field(raw: str) -> Any:
    """Decode a /v1/team-state/read field response.

    The daemon serializes dict-valued fields as YAML and IGNORES a format=json
    query param (live-probed 2026-07-17, g-115-2417) -- the .sh wrapper's --json
    is a wrapper-side conversion, not a daemon behavior. Try JSON first (cheap,
    covers null/scalars), then YAML. Returns None on any failure.
    """
    raw = (raw or "").strip()
    if not raw or raw == "null":
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    try:
        import yaml  # deferred -- only dict-valued reads pay the import
        return yaml.safe_load(raw)
    except Exception:
        return None


def read_in_flight(agent: str) -> Optional[Dict[str, Any]]:
    """Return the agent's in_flight block, or None when absent/unreadable."""
    try:
        raw = _rt.rt_call(
            "GET",
            "/v1/team-state/read",
            query={"field": f"agent_status.{agent}.in_flight"},
        )
    except Exception:
        return None
    data = _decode_team_state_field(raw)
    return data if isinstance(data, dict) else None


def local_sid(explicit: Optional[str] = None) -> Optional[str]:
    """This process's session id.

    `bash-agent-inject.py` exports MIND_SID into every Bash call, but that hook
    FAILS OPEN ON TIMEOUT (CLAUDE.md, python-invocation), so an absent value is a
    real possibility and must resolve to "unknown owner", never to "mine".
    """
    if explicit:
        v = explicit.strip()
        if v:
            return v
    v = os.environ.get("MIND_SID", "").strip()
    return v or None


def claiming_sid(goal_id: str) -> Optional[str]:
    """`claimed_by_sid` for goal_id, scanning both queues; None if unreadable.

    Scans world THEN agent: the in_flight row carries no source, and a worker can
    claim from either. Returns on the first record that HAS the field, so a
    legacy pre-g-115-3176 claim (field absent) reads as unknown rather than as a
    mismatch -- unknown leaves the row alone, mismatch would too, but the verdict
    names the real reason.
    """
    for source in ("world", "agent"):
        try:
            raw = _rt.aspirations_read(source=source, active=True)
            decoded = _rt.tolerant_decode_aggregate("active", raw)
        except Exception:
            continue
        asps = decoded.get("aspirations", []) if isinstance(decoded, dict) else decoded
        for asp in asps or []:
            for g in (asp.get("goals") or []):
                if g.get("id") != goal_id:
                    continue
                sid = g.get("claimed_by_sid")
                if isinstance(sid, str) and sid:
                    return sid
                return None
    return None


def clear_in_flight(agent: str, if_goal: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """POST the purpose-built clear (guard-2305). Parsed response, or None.

    `if_goal` makes the write a COMPARE-AND-SWAP (guard-2474 clause 2). The
    ownership verdict computed in `decide()` is derived from a SNAPSHOT; passing
    the goal_id we verified lets the endpoint re-check it under the row lock it
    already holds, so a sibling claim landing inside that window is declined
    instead of destroyed. Without this parameter the ownership test buys
    nothing — the endpoint blanks whatever row is present at call time.

    Three return shapes, deliberately distinguishable (rb-748 — a discriminated
    return is what keeps a nullable pass-through from collapsing two outcomes
    into one):
      * the decoded body ({"cleared": bool, "skipped_goal_id": str|None,
        "row_survived": bool}) — the caller can tell "cleared" from "declined,
        the row moved". `row_survived` carries the decline whose row had no
        goal_id to name, which `skipped_goal_id` reports as None and therefore
        cannot express (g-306-171);
      * {UNPARSEABLE: True} — a 2xx arrived but no JSON OBJECT did, so no CAS
        verdict was ever received (see the three sites below);
      * None — the call itself failed.
    """
    try:
        query = {"agent": agent}
        if if_goal:
            query["if_goal"] = if_goal
        raw = _rt.rt_call(
            "POST",
            "/v1/team-state/clear-in-flight",
            query=query,
        )
    except Exception:
        return None
    # THREE ways a 2xx can carry no CAS verdict, and all three must mark the
    # ambiguity.  closed only the middle one; the other two reached the
    # same bare {} WITHOUT raising, which is why its general claim went unchecked
    # on the neighbouring input shapes (). In every case the write may
    # well have happened, but we cannot say WHICH row it hit, and the read-back
    # cannot settle it — run()'s own comment says after a race it returns None
    # and reads as success. Because `resp.get("cleared", True)` defaults
    # OPTIMISTIC, any bare {} returned from here is indistinguishable from
    # {"cleared": true} and yields the verdict "cleared" for a response no CAS
    # field ever reached: a PROVEN clear asserted from nothing.
    text = (raw or "").strip()
    if not text:
        # (1) EMPTY / whitespace-only / absent body. This one never raised: the
        # old `or "{}"` fallback handed json.loads a literal "{}" that parsed
        # FINE, so it sailed past the except below into the optimistic default.
        return {UNPARSEABLE: True}
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        # (2) Genuinely unparseable — the one branch  fixed.
        return {UNPARSEABLE: True}
    if not isinstance(body, dict):
        # (3) Valid JSON that is not an object (array, string, number, `null`).
        # This returned a bare {}, landing in the same optimistic default.
        return {UNPARSEABLE: True}
    # A well-formed OBJECT that merely OMITS `cleared` deliberately keeps the
    # optimistic default (applied in run()). Flipping that to False would make a
    # version-skewed daemon which omits the field report clear-failed for a
    # perfectly good clear, and daemons hold old modules until they restart.
    # That call was made in  and still holds — do not reverse it while
    # closing . The distinction this function draws is "did an OBJECT
    # arrive at all?", never "did that object have the field?".
    return body


def clear_body_row(agent: str, sid: Optional[str]) -> str:
    """Clear THIS Body's `in_flight_bodies.<sid>` row. Returns a status token.

    WHY THIS NEEDS NO OWNERSHIP TEST (g-306-160)
    --------------------------------------------
    The whole reducer-row apparatus above exists because `in_flight` is keyed by
    AGENT NAME, so a worker and its reducer contend for one row and only
    `claimed_by_sid` can tell them apart. The body row is keyed BY THIS SID, so
    ownership is established by the key itself -- there is no other writer, and
    no CAS is needed. That is the point of body-keying, and it is why this leg is
    deliberately simple rather than a copy of the careful logic above.

    IT DELETES THE KEY (g-306-186). It used to SET NULL, and that was forced
    rather than chosen: this leg went through the generic
    `POST /v1/team-state/update` field-path dispatch, whose `_remove_nested`
    returns early unless the target is a LIST, so `--operation remove` on a dict
    key was a SILENT NO-OP that reported ok:true having done nothing. The residue
    was one permanent null-valued key per SID this agent had ever run, on a
    shared synced store — measured at 2 keys fleet-wide before the fix, which is
    small only because the worker-Body population is still ~0; growth tracks body
    count, so the cost lands exactly when the feature succeeds.
    `POST /v1/team-state/clear-body-row` is the dedicated writer for it
    (guard-2305), sharing ONE modifier factory with the CLI twin so the two
    cannot drift (guard-742). Teaching `_remove_nested` about dict keys was the
    obvious alternative and was rejected: it is a hand-mirrored PAIR, not one
    primitive, and the census found zero production `remove` callers to
    amortize the widened semantics against.

    Consumers still isinstance-guard, and must keep doing so: a null can still
    arrive from a hand-edit or from pre-fix residue on a box that has not yet run
    a close (the endpoint sweeps null siblings, so that drains on first use).
    The endpoint is idempotent — an already-absent sid is a supported no-op.

    IT IS GATED ON AGENT RESIDENCY, which is a DIFFERENT axis from the ownership
    question above and was learned the hard way. Setting a key to null does not
    only clear -- on an agent with no row at all it CREATES one, because the
    daemon writes the path it is given. So this leg, running unconditionally by
    design (the `not-ours`/`unknown-owner` paths are a worker Body's common
    case), manufactured a team-state shard for every non-resident `--agent` it
    was ever called with. Measured 2026-08-04: the parametrized test one file
    over passes `no-such-agent-xyz`, and a full-suite run left a real
    `no-such-agent-xyz` shard carrying `in_flight_bodies: {<sid>: null}` in the
    SHARED store -- which then tripped `test_active_agents_tripwire`, the roster
    detector, from an unrelated gate suite. Nothing in this file's own tests
    could see it: the write prints nothing and returns "cleared" either way.
    The predicate is deliberately IDENTICAL to the write side's in
    `team-state-in-flight.sh` -- a body row exists only for a resident agent, so
    there is never one to clear for a non-resident one.

    Fail-open in every direction, like the rest of this file: this runs at a
    turn-end and must never raise. Note the residency check fails toward
    CLEARING: an unresolvable agent dir leaves the pre-gate behavior, because a
    stale body row is a phantom claim other agents act on, while a phantom row
    for a name that does not exist is inert bookkeeping.
    """
    if not sid:
        return "no-sid"
    try:
        from _paths import agent_dir  # noqa: PLC0415 — lazy, cycle-proof
        if not Path(agent_dir(agent)).is_dir():
            return "not-resident"
    except Exception:
        pass  # unresolvable -> fall through and clear (see docstring)
    try:
        _rt.rt_call(
            "POST",
            "/v1/team-state/clear-body-row",
            query={"agent": agent, "sid": sid},
        )
    except Exception:
        return "failed"
    return "cleared"


def decide(agent: str, sid: Optional[str],
           in_flight: Optional[Dict[str, Any]],
           owner_sid: Optional[str]) -> Dict[str, Any]:
    """Pure ownership decision -- every branch is reachable without a daemon.

    Split out from the I/O above so the branch table is unit-testable; the
    fail-safe invariant under test is that only an AFFIRMATIVE sid match
    authorizes a clear.
    """
    if not in_flight:
        return {"verdict": "absent", "agent": agent, "goal_id": None,
                "reason": "no in_flight row"}
    goal_id = in_flight.get("goal_id")
    if not goal_id:
        return {"verdict": "unknown-owner", "agent": agent, "goal_id": None,
                "reason": "in_flight row carries no goal_id"}
    if not sid:
        return {"verdict": "unknown-owner", "agent": agent, "goal_id": goal_id,
                "reason": "local SID unknown (MIND_SID unset)"}
    if not owner_sid:
        return {"verdict": "unknown-owner", "agent": agent, "goal_id": goal_id,
                "reason": "claimed_by_sid unreadable or absent on the goal record"}
    if owner_sid != sid:
        return {"verdict": "not-ours", "agent": agent, "goal_id": goal_id,
                "reason": f"claimed_by_sid {owner_sid} != this Body {sid}"}
    return {"verdict": "ours", "agent": agent, "goal_id": goal_id,
            "reason": "claimed_by_sid matches this Body"}


def run(agent: str, sid: Optional[str]) -> Dict[str, Any]:
    """Clear this Body's rows: the body-keyed row, then the reducer row iff ours.

    The body leg runs FIRST and UNCONDITIONALLY (g-306-160). It is keyed by this
    SID, so it is ours on EVERY path -- including the paths where the reducer row
    is explicitly not ours ("not-ours", "unknown-owner"). Those are exactly the
    cases a worker Body hits, so gating the body clear behind the reducer verdict
    would strand it in the common case.

    The reducer leg is `_run_reducer_leg` VERBATIM -- wrapping rather than
    threading a parameter through its seven return sites keeps that carefully
    tuned branch table (and its CAS/read-back semantics) literally unchanged, so
    this addition cannot perturb it. `body_row` is reported under its own key and
    never merges into the `verdict` vocabulary.
    """
    body_row = clear_body_row(agent, sid)
    out = _run_reducer_leg(agent, sid)
    out["body_row"] = body_row
    return out


def _run_reducer_leg(agent: str, sid: Optional[str]) -> Dict[str, Any]:
    in_flight = read_in_flight(agent)
    goal_id = (in_flight or {}).get("goal_id")
    owner = claiming_sid(goal_id) if goal_id else None
    verdict = decide(agent, sid, in_flight, owner)
    if verdict["verdict"] != "ours":
        return verdict

    # Pass the goal we verified: the endpoint re-checks it under the row lock,
    # so the check and the act are on the same subject (guard-2474 clause 2).
    resp = clear_in_flight(agent, if_goal=goal_id)
    if resp is None:
        return {"verdict": "clear-failed", "agent": agent, "goal_id": goal_id,
                "reason": "clear-in-flight call failed"}
    raced = resp.get("skipped_goal_id")
    if raced:
        # The row moved between our ownership read and the write. The foreign
        # row SURVIVED -- that is the whole point of the CAS, and the fail-safe
        # direction: a missed clear self-heals, a wrong clear makes a live agent
        # look idle to every partner's selector (rb-6498).
        return {"verdict": "raced", "agent": agent, "goal_id": goal_id,
                "reason": f"row moved to {raced} before the clear; left alone"}
    if resp.get("row_survived") is True:
        # Same race, but the row it moved TO carries no goal_id to name, so the
        # branch above sees skipped_goal_id=None and cannot catch it. Without
        # this the call fell through to the `cleared is False` branch below and
        # returned verdict "clear-failed" — which reads as a broken write path
        # and invites a retry, when in fact the endpoint worked and declined on
        # purpose. "raced" is the accurate verdict: the row WAS ours at the
        # ownership read and is not ours now, so another writer landed inside
        # the window ().
        return {"verdict": "raced", "agent": agent, "goal_id": goal_id,
                "reason": "row moved to an entry carrying no comparable "
                          "goal_id before the clear; left alone unverified"}
    # guard-2305: read the field back -- a plain success message is not evidence.
    # The read-back alone CANNOT establish the right row was cleared (after a
    # race it returns None and reads as success); the CAS above is what makes
    # this check meaningful, and it stays to catch a write that silently no-ops.
    still = read_in_flight(agent)
    if still and still.get("goal_id") == goal_id:
        return {"verdict": "clear-failed", "agent": agent, "goal_id": goal_id,
                "reason": "read-back still shows the same in_flight row"}
    if resp.get(UNPARSEABLE):
        # The row is no longer ours, but we never parsed a CAS verdict, and the
        # read-back cannot supply one (see the comment above: after a race it
        # returns None and reads as success). Name the ambiguity instead of
        # inheriting the optimistic default (). Distinct from
        # clear-failed: nothing says the clear FAILED — we just cannot attest
        # which row it hit, and this rail's whole posture is that an unproven
        # clear must not be reported as a proven one.
        return {"verdict": "clear-unverified", "agent": agent, "goal_id": goal_id,
                "reason": "endpoint returned an unparseable body; "
                          "row is no longer ours but the CAS verdict is unknown"}
    if not resp.get("cleared", True):
        return {"verdict": "clear-failed", "agent": agent, "goal_id": goal_id,
                "reason": "endpoint reported nothing was cleared"}
    return {"verdict": "cleared", "agent": agent, "goal_id": goal_id,
            "reason": "worker Body genuine close; read-back confirms cleared"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--agent", required=True)
    ap.add_argument("--sid", default=None,
                    help="this Body's session id (defaults to $MIND_SID)")
    args = ap.parse_args(argv)
    try:
        out = run(args.agent, local_sid(args.sid))
    except Exception as exc:  # never block a turn-end
        out = {"verdict": "error", "agent": args.agent, "goal_id": None,
               "reason": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(out))
    return 0  # always 0 -- fail-open at a turn-end


if __name__ == "__main__":
    sys.exit(main())
