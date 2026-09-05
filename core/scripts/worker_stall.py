#!/usr/bin/env python3
"""Peer-side worker-stall detection ().

WHY THIS EXISTS, and why it is NOT "wire the watchdog into the worker loop".

`agent-watchdog.py --tick` has exactly one invoker, `iteration-close.sh`, which
the WORKER loop deliberately skips -- so no probe has ever run on a worker box.
The obvious fix is to call the tick from `worker-loop` Phase -0.4. That fix is
necessary and INSUFFICIENT, for two measured reasons:

  1. FOUR of the ten probes are structurally INERT in worker shape. A worker box
     is `agent-state: IDLE` BY DESIGN (worker-loop/SKILL.md L125-127, L316).
     `classify_stalled` returns None unless `agent_state == "RUNNING"`, so
     StalledProbe -- the probe the goal calls "exactly the detector needed" --
     returns None for ANY diary age on a worker. Measured by calling the pure
     classifier directly: diary ages 0m..10000m all yield None. RunningSidProbe
     and StopHookBlockProbe read `running-session-id`, which worker-loop's
     do-NOT list (L279) forbids a worker from setting. HeartbeatProbe fires only
     on a stale heartbeat DURING RUNNING. Wiring the tick as-is therefore
     delivers 6 of 10 probes and none of the stall detectors.

  2. AN IN-LOOP TICK DIES WITH THE LOOP. The incident that motivated the goal
     (cc-08, 2026-08-06, ~2h stalled on a lost login) was process/auth death:
     there was no loop left to run a tick. No in-loop mechanism can ever catch
     that class. Detection has to be OUT-OF-PROCESS.

So this module is the peer-side half. It runs on the REDUCER, inside the tick
that already fires every iteration -- no new wiring, no new cron, no new call
site -- and reads the one worker signal that is designed to cross boxes.

THE SIGNAL ALREADY EXISTS AND IS ALREADY READ; IT IS JUST READ TOO LATE AND
NEVER REPORTED. `stranded-claim-sweep.py` calls `_body_carrier_verdict` at
L1132, but ONLY inside the branch that has already passed
`foreign_sid_grace_minutes` (120m) -- and then only to decide keep-vs-release,
emitting no alert either way. A carrier stale at 20/60/119 minutes produces
nothing at all. In the motivating incident a human found the stall at ~89
minutes, i.e. BEFORE the fleet's own code would have so much as looked at the
carrier. This module looks at 60 minutes and REPORTS.

THE LOAD-BEARING NEGATIVE -- why stale-carrier alone must never alert.
`sessions/<SID>/body-closing` (the sentinel that means "this Body closed
deliberately") is machine-local: `sessions/` (plural) is walk-pruned by
`_EXCLUDE_DIRS`, which is precisely why the syncable
`session/body-heartbeat-<SID>.json` twin exists at all. So a peer CANNOT read
the closure sentinel, and a cleanly-closed body is indistinguishable from a
stalled one by carrier freshness alone -- a closed body's carrier goes stale and
stays stale forever. Measured on the live store: of 9 carriers, 2 were >24h old
and both belonged to long-finished bodies. A naive freshness sweep alerts on
those on its first tick, every tick, forever.

The discriminator is therefore the CLAIM, not the sentinel, and it is better
than the sentinel would have been because it targets what actually matters: a
stalled body HOLDING A CLAIM is blocking real work, while a stale carrier with
no claim is just a body that finished. This is available cross-box (claims live
in the shared world store) and it degrades in the safe direction -- an unreadable
claim store yields no alert rather than a false one.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

# Default carrier-staleness threshold, in minutes. Deliberately reuses the
# existing `runner_heartbeat.stale_minutes` convention (60) rather than
# inventing a number:
#   - it must EXCEED a legitimate long work unit. worker-loop Phase -0.4 ticks
#     at the top of every cycle, so carrier age ~= time since the current unit
#     began, and a deep unit legitimately runs 30-45 min (the same figure the
#     `_diary_stale_threshold_seconds` docstring reasons from).
#   - it must fall WELL BELOW stranded-claim-sweep's 120m foreign-SID grace, or
#     the alert arrives after the silent release it exists to pre-empt. 60
#     fires a full hour earlier, and ~29 min before the point at which a human
#     noticed in the motivating incident.
DEFAULT_STALE_MINUTES = 60.0

# Verdicts. Kept separate rather than collapsed to alert/no-alert for the
# guard-2418 reason the sibling carrier reader states: `no_claim` and
# `unreadable` take the same action today, but merging them would erase the
# signal that tells a future reader whether the carrier pipeline works at all.
V_ALIVE = "alive"                    # carrier fresh -> body is ticking
V_STALLED_WITH_CLAIM = "stalled_with_claim"   # THE ALERT
V_STALE_NO_CLAIM = "stale_no_claim"  # carrier says CLOSED -> benign, never alerts
V_STALLED_NO_CLOSE = "stalled_no_close"  # THE SECOND ALERT ()
V_STALE_PARKED = "stale_parked"      # deliberately dormant -> benign, never alerts
V_STALE_STATE_UNKNOWN = "stale_state_unknown"  # no state in carrier -> benign, COUNTED
V_UNREADABLE = "unreadable"          # present but no usable ts

# The `body_state` values that mean the Body FINISHED.
#
# MIRRORED from body-manifest.py CLOSED_STATES, which is the SSOT that declares
# the active/parked-vs-closed partition ("a future state joins exactly one of
# these two tuples and every consumer inherits the right answer"). Deliberately
# NOT imported at runtime: body-manifest.py is hyphen-named (importlib-only) and
# this module is loaded by agent-watchdog on every tick, so acquiring an import
# that can fail would let a probe-module load error take out the probe. Drift is
# caught LOUDLY at test time instead -- test_state_partition_matches_body_manifest
# importlib-loads the real module and asserts these agree, the same
# pin-the-divergence technique test_reducer_self_fence.py uses on its sibling.
CLOSED_BODY_STATES = frozenset({"closed-pending-merge", "merged", "closed-stale"})
# `parked` is a LIVE but deliberately dormant Body (body-manifest.park_body;
# capped at PARK_MAX_HOURS=60 with an hourly reducer re-poll). Excluded from the
# alert on purpose: its re-poll cadence is ~= this module's staleness threshold,
# so alerting on it would fire on the NORMAL case, and a flag that fires
# constantly stops being read at all -- the same reasoning `degraded_read` in
# scan() gives for not voiding on a single unreadable carrier.
PARKED_BODY_STATE = "parked"

ALERTING_VERDICTS = frozenset({V_STALLED_WITH_CLAIM, V_STALLED_NO_CLOSE})


def classify_body(
    carrier_age_minutes: Optional[float],
    holds_live_claim: bool,
    stale_minutes: float = DEFAULT_STALE_MINUTES,
    body_state: Optional[str] = None,
) -> str:
    """Pure classifier -- the whole decision, with no I/O.

    `carrier_age_minutes is None` means the carrier could not be decoded or
    carried no usable timestamp. That is NOT a stall: it is an instrument
    fault, and reporting it as a stall would make the probe's first symptom of
    its own breakage look like the condition it hunts (guard-1587's uniform-null
    trap). It gets its own verdict and never alerts.

    Note the asymmetry, which is the design: freshness alone can CLEAR a body
    (fresh carrier => alive, no claim lookup needed) but can never CONDEMN one.
    Condemning requires a SECOND signal, because a stale carrier is the normal
    resting state of every body that has ever finished.

    `body_state` is that second signal for a CLAIMLESS body (g-306-319). Until
    it existed, the only second signal was the claim join, so a worker that
    text-died BETWEEN units -- after releasing unit N, before claiming N+1 --
    was byte-identical to a cleanly-finished Body and got the benign
    `stale_no_claim`. That was not an oversight: with no way to tell finished
    from vanished, benign was the only choice that did not flood. The premise
    expired when a Body was witnessed dying in exactly that window (alpha
    worker, cc-07, 6h49m dark before its own 600s net resurrected it).

    What changed is not the classifier's cleverness but the EVIDENCE reaching
    it. `body_state` already existed, already populated, already written by the
    close path -- but it lives in sessions/<SID>/body-manifest.yaml, and
    `sessions` is in owncloud_sync._EXCLUDE_DIRS (walk-pruned, never pushed), so
    a peer structurally could not read it. The fix carries that STRUCTURED field
    into the carrier that IS published, rather than widening a predicate over
    prose (guard-2499: when a classifier is blind, move to the structured field
    the writer already knows at write time -- do not guess harder).

    So a stale claimless body now splits four ways, and only one alerts:
      closed-*  -> V_STALE_NO_CLAIM      the Body finished. Benign, and now
                                          EARNED rather than assumed.
      parked    -> V_STALE_PARKED        deliberately dormant. Benign.
      absent    -> V_STALE_STATE_UNKNOWN a carrier written before this field
                                          existed, or by a box that has not
                                          pulled yet. Benign -- alerting here
                                          would flood the fleet for exactly as
                                          long as the rollout takes -- but
                                          COUNTED by scan(), so the population
                                          that cannot be judged stays visible
                                          instead of silently absorbing stalls
                                          (guard-4000: a fail-safe KEEP that
                                          never reports is how an uncollectable
                                          population grows unbounded).
      anything  -> V_STALLED_NO_CLOSE    a Body that was LIVE at its last tick
      else                                and never wrote a close. THE STALL.

    The default for an unrecognised state is the ALERT, and that direction is
    deliberate: any future non-closed, non-parked state is by construction a
    live state, and a live Body that stopped ticking is the exact condition this
    module hunts. Missing a stall is the failure that motivated the change;
    a new state announcing itself as one false alert is the cheaper error.
    """
    if carrier_age_minutes is None:
        return V_UNREADABLE
    if carrier_age_minutes <= stale_minutes:
        return V_ALIVE
    if holds_live_claim:
        return V_STALLED_WITH_CLAIM
    state = (body_state or "").strip()
    if not state:
        return V_STALE_STATE_UNKNOWN
    if state in CLOSED_BODY_STATES:
        return V_STALE_NO_CLAIM
    if state == PARKED_BODY_STATE:
        return V_STALE_PARKED
    return V_STALLED_NO_CLOSE


def is_alerting(verdict: str) -> bool:
    """Single source of truth for which verdicts escalate. Callers must not
    re-derive this with their own string comparison -- that is how a verdict
    added later silently starts or stops alerting."""
    return verdict in ALERTING_VERDICTS


TERMINAL_STATUSES = {"completed", "skipped", "expired"}


def _claims_from_lines(lines) -> Dict[str, str]:
    """Parse an aspirations JSONL stream into claimed_by_sid -> goal_id."""
    out: Dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            asp = json.loads(line)
        except json.JSONDecodeError:
            continue
        for g in asp.get("goals") or []:
            if g.get("status") in TERMINAL_STATUSES:
                continue
            sid = g.get("claimed_by_sid")
            if sid:
                out.setdefault(str(sid), str(g.get("id") or ""))
    return out


def live_claim_sids(world_store: Path, text: Optional[str] = None) -> Dict[str, str]:
    """Map claimed_by_sid -> goal_id for every NON-TERMINAL claimed goal.

    Parses `text` when supplied, else the local file. Fail-open to an empty map.
    """
    if text is not None:
        return _claims_from_lines(text.splitlines())
    try:
        with world_store.open(encoding="utf-8") as fh:
            return _claims_from_lines(fh)
    except Exception:
        return {}


def _read_queue_lines(world_store: Path):
    """`(lines | None, provenance)` for one aspirations queue.

    EXTRACTED from `read_claims` (g-306-412 item 3) so a SECOND parser of the
    same bytes cannot drift from the first. The read strategy, its ordering and
    its provenance semantics are unchanged and still documented on
    `read_claims`, which is where they were argued; this function is only the
    I/O half, lifted so `read_terminal_goal_ids` shares it rather than copying
    the S3-or-local block (guard-2676: a second implementation of a capability
    drifts silently, and nothing fails when it does).

    `None` for the lines means NEITHER layer was readable, and it is returned
    beside `provenance == "none"` so a caller cannot mistake an unreadable store
    for an empty one (guard-2418 / guard-1753) -- the distinction `read_claims`
    already made and this preserves.
    """
    try:
        import sys
        scripts = world_store.parent.parent.parent / "core" / "scripts"
        if scripts.is_dir() and str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        from storage_backend import get_backend  # noqa: PLC0415
        from _owncloud_codec import decode_response  # noqa: PLC0415  # 

        b = get_backend()
        key = b._s3_key(world_store)
        # : the queue may be gzip on the wire — decode through the one
        # transport seam (magic-byte authoritative; a plain object passes through).
        body = decode_response(
            b.s3.get_object(Bucket=b.bucket, Key=key), key=key
        ).decode("utf-8")
        return body.splitlines(), "authoritative"
    except Exception:
        pass
    try:
        with world_store.open(encoding="utf-8") as fh:
            return fh.read().splitlines(), "local-mirror"
    except OSError:
        return None, "none"


def _terminal_goal_ids_from_lines(lines) -> "set":
    """Ids of goals sitting in a TERMINAL status. Sibling of `_claims_from_lines`.

    POSITIVE evidence only, and that is the whole reason this exists rather than
    reusing the claim map. `_claims_from_lines` records `sid -> goal_id` with
    `setdefault`, so a sid holding TWO non-terminal goals keeps only the first:
    "this row's goal is missing from the claim map" is therefore NOT evidence
    the goal is finished, it can equally be the second goal of a busy Body. A
    delete predicate cannot be built on that absence (guard-2418); it needs the
    store to SAY the goal is terminal, which is what this returns.
    """
    out = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            asp = json.loads(line)
        except json.JSONDecodeError:
            continue
        for g in asp.get("goals") or []:
            if g.get("status") in TERMINAL_STATUSES:
                gid = g.get("id")
                if gid:
                    out.add(str(gid))
    return out


def read_terminal_goal_ids(*stores: Path) -> "tuple[set, str]":
    """Terminal goal ids across SEVERAL queues, with the WEAKEST provenance.

    Union sibling of `read_claims_union`, and union for the same reason: a Body
    of this agent can claim into the world queue OR its own agent queue, so a
    world-only read would report an agent-queue goal as "not terminal" purely
    because it was never looked at.

    THE CONSERVATIVE DIRECTION IS THE OPPOSITE ONE HERE, so the union's safety
    argument does not carry over unexamined. For claims, adding entries can only
    turn a reap into a keep. For terminal ids, adding entries can only turn a
    keep into a REAP -- the destructive direction. What keeps that safe is that
    every id added is POSITIVE evidence read from a queue: the union widens what
    is READ, never what is INFERRED from silence. A store that cannot be read
    contributes nothing and degrades the provenance, which the caller checks.
    """
    if not stores:
        return set(), "none"
    rank = {"none": 0, "local-mirror": 1, "authoritative": 2}
    merged = set()
    weakest = "authoritative"
    for store in stores:
        lines, prov = _read_queue_lines(store)
        if lines is not None:
            merged |= _terminal_goal_ids_from_lines(lines)
        if rank[prov] < rank[weakest]:
            weakest = prov
    return merged, weakest


def _known_goal_ids_from_lines(lines) -> "set":
    """EVERY goal id in these lines, whatever its status ().

    Sibling of `_terminal_goal_ids_from_lines`, and deliberately status-blind:
    this answers "does a record for this id EXIST anywhere", never "is it
    finished". The two must not be merged — a status filter here would make an
    id's absence ambiguous between "no such goal" and "a goal in a status the
    filter dropped", and the caller reads absence as grounds to DELETE.
    """
    out = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            asp = json.loads(line)
        except json.JSONDecodeError:
            continue
        for g in asp.get("goals") or []:
            gid = g.get("id")
            if gid:
                out.add(str(gid))
    return out


def read_known_goal_ids(*stores: Path) -> "tuple[set, str]":
    """Ids of goals that EXIST in any of these stores, with WEAKEST provenance.

    THE SAFETY DIRECTION IS THE EXACT INVERSE OF `read_terminal_goal_ids`, and
    that inversion is the whole reason this is a separate reader rather than a
    flag on that one. There, adding ids turns a keep into a REAP. Here, adding
    ids turns a reap into a KEEP: a caller reaps on ABSENCE from this set, so
    every store added can only ever spare a row. Widening the census is the
    conservative move, which is why the ARCHIVE belongs in this call and must
    never be added to the terminal one.

    That inversion is also what makes the archive load-bearing rather than
    thorough. MEASURED 2026-09-04 (alpha, hostname cc-09, uname -r
    6.8.0-138-generic, authoritative reads): world queue 2,921 ids + alpha queue
    32 + archive 2,480 = 5,432 union. Censusing only the two LIVE queues would
    therefore have reported ~2,480 archived-but-present goals as "resolving
    nowhere" — mass inference-from-silence in the delete direction, from a
    predicate that would have looked correct in every test that never opened the
    archive. guard-3379: an absence claim must name which persistence layers were
    censused, and one store is never enough.

    WHY THESE THREE LAYERS ARE SUFFICIENT for the reaper's question: it is
    owning-agent-only and a session of agent X cannot claim into agent Y's queue,
    so every goal a row of this agent can name lives in the world queue, this
    agent's own queue, or the archive both drain into. Sufficiency is a property
    of the CALLER's scope, not of this function — a caller that widens whose rows
    it judges must widen the stores it passes, or absence stops meaning absence.

    Provenance is the weakest of the halves, same as its siblings: an unreadable
    layer contributes nothing and the caller — not this function — decides what an
    unanswered layer means. An unreadable store must never be able to mint a reap.
    """
    if not stores:
        return set(), "none"
    rank = {"none": 0, "local-mirror": 1, "authoritative": 2}
    merged = set()
    weakest = "authoritative"
    for store in stores:
        lines, prov = _read_queue_lines(store)
        if lines is not None:
            merged |= _known_goal_ids_from_lines(lines)
        if rank.get(prov, 0) < rank[weakest]:
            weakest = prov if prov in rank else "none"
    return merged, weakest


def read_claims(world_store: Path) -> "tuple[Dict[str, str], str]":
    """Claim map plus the PROVENANCE of the bytes it was parsed from.

    guard-980 applies to BOTH halves of this join, and the first version applied
    it to only one. Carriers were read from the store of record with an explicit
    comment about read-through caches; claims were then read from the local
    file -- and claims are the CONDEMNING half, since freshness alone can only
    clear a body. Measured cc-02 2026-08-06T16:40: local aspirations.jsonl was
    21,769,799 bytes / 16:35:26 while S3 held 21,778,540 bytes / 16:36:13 --
    diverged by 8,741 bytes and 47 seconds at the instant of measurement. A goal
    completed on another box still reads as claimed in that stale copy, which
    classifies a cleanly-finished body as `stalled_with_claim`: a FALSE ALERT,
    the exact failure the module's load-bearing negative exists to prevent.

    The original fail-open argument was half right and worth keeping straight: an
    ABSENT store yields an empty map and can only suppress alerts. A STALE store
    is fully readable and confidently wrong, so absence and staleness are not the
    same risk and only the first one is safe.

    Provenance is returned rather than swallowed for the guard-1753 reason: a
    value that cannot say which layer produced it lets an error path masquerade
    as a good read.

    Provenance is keyed off READ SUCCESS, never off map truthiness. The first
    version wrote `"local-mirror" if local else "none"`, which reported an
    identical `"none"` for a store that was read fine and simply holds no live
    claims and a store that could not be opened at all -- measured
    indistinguishable. That is the very blindness the field exists to remove:
    an empty map is a legitimate answer, so emptiness cannot stand in for "no
    layer answered". `"none"` now means exactly one thing -- neither layer was
    readable.
    """
    lines, provenance = _read_queue_lines(world_store)
    if lines is None:
        return {}, provenance
    return _claims_from_lines(lines), provenance


def read_claims_union(*stores: Path) -> "tuple[Dict[str, str], str]":
    """Claim map across SEVERAL queues, reporting the WEAKEST provenance of any.

    A Body claims into the world queue OR its own agent queue, so a claim map
    built from the world queue ALONE reads an agent-queue-only claim as NO CLAIM
    -- and `holds_live_claim == False` is the sole condition under which
    `body_row_reaper` reaps. Measured 2026-08-08 (alpha, hostname cc-04,
    `uname -r` 6.8.0-136-generic): world 7 live-claim sids, agent queues 2 (alpha
    1, zeta 1), of which 0 were world-invisible and 0 coincided with a body row.
    So the gap was real in the code and DORMANT in the data -- which is a
    snapshot, not a structural guarantee: nothing stops a Body from working only
    agent-queue goals, and that Body is exactly the one this union protects.

    The union is MONOTONICALLY CONSERVATIVE, and that is why it is the safe
    direction to fix in: adding claims can only turn a reap into a keep, never a
    keep into a reap. A wrong keep costs a lingering row; a wrong reap orphans
    in-flight work.

    Provenance is the WEAKEST of the halves (none < local-mirror < authoritative)
    for the same guard-1753 reason `read_claims` returns it at all: an empty map
    from an unreadable store and an empty map from a store with genuinely no live
    claims are indistinguishable, so the caller -- not this function -- decides
    what an unanswered half means. Reporting the most flattering half would let
    one good read launder a failed one.
    """
    if not stores:
        return {}, "none"
    rank = {"none": 0, "local-mirror": 1, "authoritative": 2}
    merged: Dict[str, str] = {}
    weakest = "authoritative"
    for store in stores:
        claims, via = read_claims(store)
        # An UNRECOGNISED provenance NORMALISES to "none"; it does not pass
        # through. Ranking it lowest is only half the job, because callers
        # decline on the VALUE (`via == "none"`) — propagating an unknown string
        # would rank it weakest AND clear the decline test, the two halves
        # disagreeing about what "untrusted" means. Unreachable today
        # (read_claims returns three literals) and normalised here anyway, since
        # the alternative is asking every caller to enumerate the trusted set.
        # rb-7201, found by the fresh-eyes pass on the commit that added this.
        if via not in rank:
            via = "none"
        if rank[via] < rank[weakest]:
            weakest = via
        for sid, goal_id in claims.items():
            merged.setdefault(sid, goal_id)
    return merged, weakest


def _parse_iso(s: str) -> Optional[dt.datetime]:
    """Parse a carrier timestamp to a NAIVE datetime, whatever offset it carries.

    The first version did `split("+")[0]`, which strips `+HH:MM` and nothing
    else. A trailing `Z` survives it, and on 3.12 `fromisoformat("...Z")`
    returns an AWARE datetime -- which then raises TypeError when subtracted
    from a naive `now` in scan(). That raise escaped the whole scan, so ONE
    malformed carrier silenced every OTHER body's verdict (measured: 2
    genuinely-stalled bodies produced zero events). `-HH:MM` cannot be split
    off at all, because dates contain `-`.

    Normalising here fixes the defect at the VALUE (guard-2521) rather than at
    the raise: no caller can now receive an aware datetime. The per-row guard
    in scan() is defence-in-depth for other malformed shapes, not the fix.
    Converting through UTC before dropping tzinfo keeps a `-05:00` stamp
    correct rather than reading its wall-clock digits as UTC.
    """
    txt = str(s).strip()
    if txt.endswith("Z") or txt.endswith("z"):
        txt = txt[:-1]
    try:
        parsed = dt.datetime.fromisoformat(txt)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return parsed


def enumerate_carriers(agents_root: Path) -> "tuple[List[Dict[str, Any]], Dict[str, Any]]":
    """Enumerate body-heartbeat carriers, WITH evidence of how complete the scan was.

    Prefers the STORE OF RECORD (guard-980: under own-cloud the local tree is a
    read-through cache, so a carrier written by another box may simply not exist
    locally -- and `Path.exists()` returning False for a live worker is exactly
    the false negative this probe cannot afford). Falls back to the local mirror
    only when the backend is unavailable, and says which path it used so a
    reader can tell a real all-clear from a degraded one.

    RETURNS A PAIR, and the second element is the point. The first version
    returned a bare list, and THREE separate routes could emit `[]` while only
    ONE of them raised: an empty authoritative roster, a partially-failed local
    glob, and a local glob that matched nothing. `[]` therefore meant both
    "there are no carriers" and "I could not enumerate", and scan() rendered
    the second as a confident `scanned: 0, alerts: [], degraded_read: false`.
    Fixing the fallback's TRIGGER would have covered only the route that
    raises -- 1 of 3 (guard-2521: fix at the VALUE, not at the raise). So no
    route may return a bare list any more; every one must state its own
    completeness.

    `meta` carries:
        read_via  -- "authoritative" | "local-mirror" | "none"
        complete  -- False when this enumeration could not answer the question
        agents_enumerated -- roster size the carrier scan actually covered
        reason    -- set only when complete is False; why it could not answer

    RANKED, not OR-ed (guard-1686). The mirror serves exactly one premise: the
    authoritative path STRUCTURALLY could not answer (the backend raised). An
    authoritative roster that enumerated N>0 agents and found no carriers is a
    real answer -- a fleet can legitimately have zero live bodies -- so it
    returns complete=True with an empty list and does NOT fall through. A
    roster of ZERO agent prefixes is the opposite: the agents root always holds
    agents, so an empty roster means the prefix is wrong, which is not an
    answer at all.
    """
    # NOTE the two-step listing, and do NOT collapse it back to one broad walk.
    # The first version paginated `<env>/agents/` whole and filtered keys after
    # listing. Measured cc-02 2026-08-06: 47,519 objects LISTED to retrieve 9
    # carriers -- 5,280x amplification, ~48 round-trips, on the reducer's
    # per-iteration tick, in a function whose docstring promises to be fast.
    # One delimited call for the roster plus one EXACT-prefix list per agent
    # (the carrier filename is itself a prefix) measures 9 objects for 9
    # carriers: 100% precision, 5,280x fewer objects, 4.8x fewer calls.
    found: List[Dict[str, Any]] = []
    # Per-object GET failures, COUNTED rather than swallowed. The `doc = {}`
    # below turns an unreadable carrier into an `unreadable` VERDICT, and that
    # verdict was invisible to every completeness field this function returns:
    # the roster listing succeeds, so `complete` stays True, and a fleet where
    # NOT ONE carrier could be fetched reported a confident `degraded_read:
    # false` (). The count is the evidence; the asymmetry that acts on
    # it lives in scan().
    read_errors = 0
    first_read_error: Optional[str] = None
    try:
        import sys
        if str(agents_root.parent / "core" / "scripts") not in sys.path:
            sys.path.insert(0, str(agents_root.parent / "core" / "scripts"))
        from storage_backend import get_backend  # noqa: PLC0415
        from _owncloud_codec import decode_response  # noqa: PLC0415  # 

        b = get_backend()
        probe_key = b._s3_key(agents_root / "_probe" / "session" / "body-heartbeat-x.json")
        if "/_probe/" not in probe_key:
            # The key shape the base prefix is derived from is not what we
            # assumed, so `rsplit` would silently hand back the whole key and
            # every subsequent list would match nothing. Refuse to report that
            # as an empty fleet.
            raise ValueError(f"unexpected probe key shape: {probe_key!r}")
        base = probe_key.rsplit("/_probe/", 1)[0] + "/"
        roster = b.s3.list_objects_v2(Bucket=b.bucket, Prefix=base, Delimiter="/")
        agent_prefixes = [c["Prefix"] for c in (roster.get("CommonPrefixes") or [])]
        if not agent_prefixes:
            # NOT an answer. The agents root always holds agents, so a roster
            # of zero means the prefix is wrong -- the enumeration failed. Fall
            # through to the mirror rather than reporting an empty fleet.
            raise ValueError(f"roster returned no agent prefixes under {base!r}")
        pg = b.s3.get_paginator("list_objects_v2")
        for ap in agent_prefixes:
            agent = ap[len(base):].rstrip("/")
            for page in pg.paginate(
                Bucket=b.bucket, Prefix=ap + "session/body-heartbeat-"
            ):
                for o in page.get("Contents") or []:
                    key = o["Key"]
                    if not key.endswith(".json"):
                        continue
                    sid = key.rsplit("body-heartbeat-", 1)[-1][: -len(".json")]
                    try:
                        doc = json.loads(
                            decode_response(  #  transport seam
                                b.s3.get_object(Bucket=b.bucket, Key=key), key=key
                            ).decode()
                        )
                    except Exception as exc:
                        # The blanket catch stays, and narrowing by tuple is not
                        # available here: the failure family spans botocore
                        # client/endpoint errors (from a lazily imported
                        # backend), UnicodeDecodeError and JSONDecodeError.
                        # guard-373's real objection is that a blanket catch
                        # MASKS a logic bug -- so record the class and message
                        # instead, which makes the mask LOUD rather than
                        # pretending to remove it (guard-1977).
                        doc = {}
                        read_errors += 1
                        if first_read_error is None:
                            first_read_error = f"{type(exc).__name__}: {exc}"
                    found.append(
                        {"agent": agent, "sid": sid, "doc": doc,
                         "read_via": "authoritative"}
                    )
        # A real answer, even when empty: the roster was enumerable and every
        # agent in it was scanned.
        return found, {
            "read_via": "authoritative",
            "complete": True,
            "agents_enumerated": len(agent_prefixes),
            "reason": None,
            "carrier_read_errors": read_errors,
            "first_carrier_read_error": first_read_error,
        }
    except Exception as exc:
        auth_error = f"{type(exc).__name__}: {exc}"
    # `found` is DELIBERATELY discarded rather than extended below. A pagination
    # that fails PARTWAY leaves rows already collected; appending the mirror rows
    # on top duplicated every such sid (once per read path) and double-counted it
    # in `alerts`. The all-or-nothing cases hid it -- tests exercised total
    # success and total failure, never a failure on the second page.
    # Fallback: local mirror. Marked, so a degraded scan is never mistaken for a
    # complete one. `rows` starts EMPTY here rather than continuing `found` --
    # that is the whole fix; see the note above the `except`.
    rows: List[Dict[str, Any]] = []
    agent_dirs = 0
    # RESET, for the same reason `rows` starts empty rather than continuing
    # `found`: the authoritative rows are discarded on this path, so counting
    # their read failures here would attribute errors to rows that are not in
    # the report. The mirror's own failures are counted below.
    read_errors = 0
    first_read_error = None
    try:
        session_dirs = sorted(agents_root.glob("*/session"))
        agent_dirs = len(session_dirs)
        for conf in session_dirs:
            for p in sorted(conf.glob("body-heartbeat-*.json")):
                sid = p.name[len("body-heartbeat-") : -len(".json")]
                try:
                    doc = json.loads(p.read_text(encoding="utf-8"))
                except Exception as exc:
                    # Same swallow, same fix, second site. guard-345: when a
                    # silent failure is traced to one cause, probe the other
                    # ways the same boundary reaches the same symptom. This is
                    # one of them; the third is a carrier that reads fine and
                    # carries a corrupt or absent `ts`, which `_parse_iso`
                    # renders as the SAME `unreadable` verdict with no read
                    # failure at all -- which is why scan() keys the asymmetry
                    # on the verdict rather than on this counter.
                    doc = {}
                    read_errors += 1
                    if first_read_error is None:
                        first_read_error = f"{type(exc).__name__}: {exc}"
                rows.append(
                    {
                        "agent": conf.parent.name,
                        "sid": sid,
                        "doc": doc,
                        "read_via": "local-mirror",
                    }
                )
    except OSError as exc:
        # Partial walk. Whatever was collected is real, but this is NOT a
        # complete enumeration and must never read as one.
        return rows, {
            "read_via": "local-mirror",
            "complete": False,
            "agents_enumerated": agent_dirs,
            "reason": f"authoritative unavailable ({auth_error}); "
                      f"mirror walk failed: {type(exc).__name__}: {exc}",
            "carrier_read_errors": read_errors,
            "first_carrier_read_error": first_read_error,
        }
    if agent_dirs == 0:
        # Same shape as the empty roster above: the agents root always holds
        # agents, so matching none means this path could not answer either.
        return rows, {
            "read_via": "none",
            "complete": False,
            "agents_enumerated": 0,
            "reason": f"authoritative unavailable ({auth_error}); "
                      f"mirror matched no agent session dirs under {agents_root}",
            "carrier_read_errors": read_errors,
            "first_carrier_read_error": first_read_error,
        }
    # The mirror answered, but it is a read-through cache — a carrier written
    # by another box may simply not be here (guard-980). Never `complete`.
    return rows, {
        "read_via": "local-mirror",
        "complete": False,
        "agents_enumerated": agent_dirs,
        "reason": f"authoritative unavailable ({auth_error}); "
                  f"read-through mirror cannot see carriers this box never pulled",
        "carrier_read_errors": read_errors,
        "first_carrier_read_error": first_read_error,
    }


def scan(
    agents_root: Path,
    world_store: Path,
    stale_minutes: float = DEFAULT_STALE_MINUTES,
    now: Optional[dt.datetime] = None,
) -> Dict[str, Any]:
    """Full peer-side sweep. Returns a report; performs no mutation and files
    no goals -- reporting is the entire job, because the measured defect is that
    the signal was computed and never reported."""
    now = now or dt.datetime.now()
    # ENUMERATE FIRST, then read claims -- the order is load-bearing, not
    # cosmetic. The store set depends on WHICH agents own carriers, so the
    # carrier list has to exist before the claim map can be scoped ().
    rows, enum_meta = enumerate_carriers(agents_root)
    # A Body claims into the world queue OR its own agent queue, so a world-only
    # map reads an agent-queue-only claim as NO CLAIM. In the reaper that made a
    # protected row reapable (); here it makes a stalled Body SILENT,
    # because `stalled_with_claim` is the alerting verdict and it requires a
    # visible claim. Same scope gap, detection direction instead of reaping.
    #
    # Bounded by DISTINCT OWNING AGENTS, not by carriers: 9 reads for 11 carriers
    # in the population measured 2026-08-08. The owner is already on every row
    # from enumerate_carriers, so no extra lookup is needed to derive it.
    #
    # The union is deliberately GLOBAL rather than per-owner, and the cost of
    # that choice is confined to one field: `held_goal` is now union-scoped, so
    # for a sid present in a non-owning agent's queue it can name that queue's
    # goal. The ALERT itself keys on claim PRESENCE, which is unaffected. Note
    # `held_goal` was already only-one-of-many for a Body holding several claims
    # (read_claims maps a sid to a single goal), so this widens an existing
    # imprecision in a reported field rather than introducing one into a verdict.
    claim_stores = [world_store] + [
        agents_root / a / "aspirations.jsonl"
        for a in sorted({r["agent"] for r in rows if r.get("agent")})
    ]
    claims, claims_via = read_claims_union(*claim_stores)
    bodies: List[Dict[str, Any]] = []
    # Per-item resilience is right, but the EVIDENCE must never be discarded
    # (guard-1893). Count what was dropped and keep the first error, so a
    # systematically-failing row set cannot present as a clean empty scan.
    dropped = 0
    first_drop_error: Optional[str] = None
    for row in rows:
        try:
            ts = _parse_iso(str((row.get("doc") or {}).get("ts") or ""))
            age = None if ts is None else (now - ts).total_seconds() / 60.0
            goal = claims.get(row["sid"])
            # : the second signal for a claimless body. Absent on a
            # carrier written before the field existed, or by a box that has
            # not pulled the writer yet -- classify_body renders that as
            # V_STALE_STATE_UNKNOWN rather than guessing either way.
            state = (row.get("doc") or {}).get("body_state")
            verdict = classify_body(age, goal is not None, stale_minutes, state)
            bodies.append(
                {
                    "agent": row["agent"],
                    "sid": row["sid"][:8],
                    "host": (row.get("doc") or {}).get("host"),
                    "carrier_age_minutes": None if age is None else round(age, 1),
                    "held_goal": goal,
                    "body_state": state,
                    "verdict": verdict,
                    "read_via": row.get("read_via"),
                }
            )
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            # Narrow on purpose (guard-373): these are the malformed-carrier
            # classes. A broader catch would mask a logic bug here as a benign
            # per-row skip, which is the failure this whole module is about.
            dropped += 1
            if first_drop_error is None:
                first_drop_error = f"{type(exc).__name__}: {exc}"
    alerts = [b for b in bodies if is_alerting(b["verdict"])]
    # The detectable invariant is the ASYMMETRY -- non-empty in, empty out --
    # not the emptiness (guard-1893). An empty fleet and a fleet whose every
    # row raised produce the same `bodies` list and must not read the same.
    enumeration_lost_everything = bool(rows) and not bodies
    # SECOND asymmetry, same shape, different loss mode. The one above catches
    # rows that RAISED out of `bodies` entirely; this one catches rows that
    # survived INTO `bodies` carrying no usable state. Both render as a clean
    # `alerts: []`, and neither is an all-clear.
    #
    # Keyed on the VERDICT, not on `carrier_read_errors`, because three routes
    # reach `unreadable` and only one is a read failure: a failed GET, a doc
    # whose `ts` is corrupt, and a doc with no `ts` at all (guard-345). Keying
    # on the counter would leave two of the three routes silent, which is the
    # defect this fix exists to end rather than relocate.
    usable = [b for b in bodies if b["verdict"] != V_UNREADABLE]
    all_carriers_unreadable = bool(bodies) and not usable
    #  COVERAGE, per guard-3489: report how much of the population this
    # scan could actually judge, as a first-class field beside the alert count.
    # A stale claimless body with no `body_state` is un-judgeable for the
    # between-units stall class -- it is rendered benign, which is the only
    # non-flooding choice during rollout, and that is exactly why the count has
    # to be visible. Without it a fleet of entirely pre-rollout carriers reports
    # `alerts: []` in the same bytes as a fleet with nothing wrong.
    #
    # DELIBERATELY NOT folded into `degraded_read`. That flag means "this report
    # does not bound the fleet", and an unknown-state carrier still bounds the
    # with-claim stall class perfectly well. More decisively, it would be TRUE on
    # every box until the writer propagates fleet-wide -- and a flag that fires
    # constantly stops being read, which is the reasoning this function already
    # applies to a single unreadable carrier.
    state_unknown = sum(
        1 for b in bodies if b["verdict"] == V_STALE_STATE_UNKNOWN)
    state_known = sum(
        1 for b in bodies
        if b["verdict"] in (V_STALE_NO_CLAIM, V_STALE_PARKED, V_STALLED_NO_CLOSE))
    return {
        "scanned": len(bodies),
        "stale_minutes": stale_minutes,
        "alerts": alerts,
        "bodies": bodies,
        "claims_read_via": claims_via,
        # Enumeration evidence, so a zero scan can always be told apart from a
        # scan that could not look. `complete` False means this report does NOT
        # bound the fleet -- read it as "unknown", never as "nothing wrong".
        "enumeration": enum_meta,
        "carriers_found": len(rows),
        "rows_dropped": dropped,
        "first_drop_error": first_drop_error,
        "enumeration_lost_everything": enumeration_lost_everything,
        # Carrier-read evidence, promoted out of enum meta so a caller reading
        # only the summary still sees it. ALWAYS reported, even when it does not
        # flip `degraded_read` below -- that is this pair's whole job: guard-2081
        # requires that a source failure never scroll past unrecorded, and this
        # is where a single failed GET stays visible.
        "carrier_read_errors": enum_meta.get("carrier_read_errors", 0),
        "first_carrier_read_error": enum_meta.get("first_carrier_read_error"),
        "all_carriers_unreadable": all_carriers_unreadable,
        # Stale-body close-vs-vanished coverage (). `state_unknown`
        # counts bodies this scan could NOT judge on that axis.
        #
        # A PERSISTENT NON-ZERO HAS TWO CAUSES AND ONLY ONE IS A DEFECT, so do
        # not read it as an alarm on its own. (a) Carriers of Bodies that DIED
        # before this field existed: nothing rewrites a dead Body's carrier, so
        # those stay unknown forever and are genuinely unjudgeable -- measured 4
        # on cc-07 at rollout, aged 3.6 to 12.5 days. (b) A LIVE Body whose
        # carrier lacks the field, which does mean the writer is not reaching
        # that box -- heartbeat-tick.sh restamps every cycle, so a live Body's
        # carrier is unknown only if the writer is absent or failing.
        # The discriminator is `carrier_age_minutes`, which is already on every
        # row: an unknown-state row that is FRESH is case (b).
        "state_known": state_known,
        "state_unknown": state_unknown,
        # Degraded if ANY leg fell back or lost data. The claim half is included
        # because it is the condemning one: a stale claim map is what turns a
        # finished body into a false alert. The enumeration half is included
        # because an incomplete carrier list silently shrinks the population
        # every other field is computed over.
        #
        # `all_carriers_unreadable` joins them, and note what does NOT: a
        # NON-ZERO `carrier_read_errors` on its own. guard-2081 argues any
        # source failure should void the aggregate, and that is right about the
        # printed-summary case it was written for -- but this report is a dict,
        # and an unreadable carrier is already a first-class per-body verdict
        # plus a counted field, so nothing scrolls past. The remaining question
        # is only what `degraded_read` MEANS, and it means "this report does not
        # bound the fleet". One bad carrier among nine leaves eight trustworthy
        # verdicts and any alert among them real; voiding on that would fire on
        # the common case -- a finished body's carrier can be unreadable -- and
        # a flag that fires constantly stops being read at all. All-unreadable
        # is different in kind: it bounds nothing.
        "degraded_read": (
            any(b.get("read_via") == "local-mirror" for b in bodies)
            or claims_via != "authoritative"
            or not enum_meta.get("complete", False)
            or dropped > 0
            or all_carriers_unreadable
        ),
    }
