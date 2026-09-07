"""Is an agent-queue read answerable from THIS box? (, item 2)

A CROSS-AGENT WRITE FAILS LOUDLY; A CROSS-AGENT READ FAILS SILENTLY.
`OwnCloudBackend._put` consults `owncloud_sync._owned_agents_with_provenance()`
and raises `no_claim` — "this box is permanently behind the claim-holder's
advancing version. STRUCTURAL, not a race" — before any write to an agent dir
this box does not own. The READ path had no equivalent: it answered HTTP 200
with a complete-looking record read from a mirror that the framework already
knew was structurally behind (guard-6156, guard-6166).

MEASURED 2026-09-06/07 across three boxes: zeta closed g-001-109 at 19:19:21
with its own read-back; `MIND_AGENT=zeta aspirations-query.sh --goal-field id
g-001-109 --full` on cc-05 returned rc=0 with `status=pending`, `outcome_note`
empty, `completed_at`/`completed_by`/`outcome_class` all None — 6h48m later,
from a mirror 8,545 B short of the store. Read alone that says the owner LOST a
write. The same staleness put that closed goal at rank 1 of 1,562 in the
selector. The only thing that stopped execution was the WRITE-side gate, and
that protection is incidental: it is absent for any cross-agent goal whose
disposition needs no peer-queue write (an investigation, a re-measure, a
report), which runs to completion against a closed record.

WHAT THIS MODULE CLAIMS, AND WHAT IT DOES NOT. It reports UNVERIFIABILITY, not
divergence. "This box does not hold the runner claim for agent dir X, so this
read came from a mirror that is structurally behind the store of record" is
always true when it fires and costs one cached consult. "Local and the store
actually differ right now" is a stronger claim and is NOT made here — see
`_measure_divergence_is_not_free` below for the measurements that rule it out.

FAIL-OPEN IN EVERY DIRECTION (guard-1562). Any provenance other than
"live-claims", any resolution failure, any import failure returns
`unverified=False` — the same carve-out the write gate states for itself:
"local-backend, transient-error and unknown-machine all fall through ... on
those this box may in fact own the dir and merely failed to prove it, and
asserting a structural impossibility there would be the same confident-and-wrong
error this fix removes." A read must never fail because the disclosure could
not be computed.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Optional, Tuple


#: Value stamped on a row that came from an agent queue this box cannot verify.
#: Rides the PAYLOAD stream, so it survives the pipes that destroy a stderr
#: warning or a distinct exit code (guard-5596, measured on goal-selector.sh:
#: 103 pipe uses across 8 sessions in one day).
UNVERIFIED = "peer-mirror-unverified"

#: How long an ownership verdict is reused. A runner claim is a lease measured
#: in hours (`runner_heartbeat.stepdown_seconds` is 1950s = 32.5 min just for
#: the STEP-DOWN half), so a 5-minute reuse window cannot straddle a takeover
#: in any way that matters here: the disclosure is advisory, and both stale
#: directions are safe — a stale "owned" merely withholds a warning that the
#: next window emits, and a stale "unverified" over-warns on a queue that just
#: became ours.
_TTL_SECONDS = 300.0

_lock = threading.Lock()
_cached: Optional[Tuple[float, frozenset, str]] = None


def _measure_divergence_is_not_free() -> None:
    """Why this module asserts unverifiability rather than measured divergence.

    The tempting design is to report ACTUAL divergence, and there appears to be
    a free signal for it: `OwnCloudBackend._refresh` records the both-diverged
    (`no_clobber`) verdict in `_diverged_keys`, and `jsonl_cache.get()` already
    calls `ensure_local()` immediately before every read — so a set-membership
    test after the read looks like a zero-I/O divergence probe (the shape
    `goal-selector.py::_refresh_declined` uses, g-115-9276 item 1).

    IT IS NOT ONE HERE, AND THE FALSE READING IS THE DANGEROUS DIRECTION.
    `ensure_local` calls `_refresh(path, force_fresh=False)`, which (a)
    unconditionally `discard`s the key from `_diverged_keys` on entry and then
    (b) returns early on a warm TTL cache WITHOUT contacting S3 — the branch
    whose own comment says it "proves NOTHING (no S3 contact), so this discard
    CAN drop a still-true flag there." So after a cached read the set is empty
    whether or not the mirror diverges, and a `False` would mean "not checked",
    not "no divergence". Reporting that as an all-clear is guard-5501 exactly: a
    diagnostic whose silence is not evidence.

    Getting a real divergence verdict therefore costs a `force_fresh` round
    trip. MEASURED on cc-10 (own-cloud, 2026-09-07, alpha worker Body):
    `backend-cat.sh head --exit-on-drift` 452 ms (alpha queue) / 450 ms (zeta);
    a full authoritative GET 682 ms (148,655 B) / 549 ms (122,842 B); and the
    ownership consult below 330 ms uncached. This box owns ZERO agent dirs
    (`_owned_agents_with_provenance()` -> `(set(), 'live-claims')`), which is the
    normal state of a worker Body — so a per-read probe would fire on EVERY
    `source=agent` read, including the loop-hot path, at ~450-680 ms each. This
    goal's third verification outcome forbids exactly that: "the chosen remedy's
    call-frequency cost is MEASURED before any per-read backend fetch is added."
    It is measured, and the measurement says no.

    Never called; it exists to keep the rejected design and its numbers next to
    the code that rejected them.
    """


def _owned_now():
    """`(owned_frozenset, provenance)` from the ownership SSOT, TTL-cached.

    Delegates to `owncloud_sync._owned_agents_with_provenance()` — the SAME
    resolution the write-side `no_claim` gate consults — rather than re-deriving
    ownership from claim files. Three copies of this predicate would be three
    things to keep in sync (guard-130 / guard-2676).
    """
    global _cached
    now = time.monotonic()
    with _lock:
        if _cached is not None and (now - _cached[0]) < _TTL_SECONDS:
            return _cached[1], _cached[2]
    try:
        from owncloud_sync import _owned_agents_with_provenance
        owned, provenance = _owned_agents_with_provenance()
        entry = (now, frozenset(owned or ()), str(provenance or ""))
    except Exception:  # noqa: BLE001 — advisory input; never fail a read on it
        # Cached like any other verdict: a box with no own-cloud module would
        # otherwise pay the import failure on every single read.
        entry = (now, frozenset(), "")
    with _lock:
        _cached = entry
    return entry[1], entry[2]


def reset_cache() -> None:
    """Drop the memoized verdict. For tests, and for a caller that has just
    changed the claim state and wants the next read to re-resolve."""
    global _cached
    with _lock:
        _cached = None


def agent_name_for(path) -> Optional[str]:
    """The agent dir a path lives under, or None.

    Same resolution the write gate uses: resolve against `agents_root()` and
    take the first path part. `.resolve()` is load-bearing — a relative path
    resolves against the process CWD and would name the wrong agent (the
    g-115-4256 class, where a probe built on a relative path reported
    local==authoritative for all 5 agents while 4 of 5 had diverged).
    """
    try:
        from _paths import agents_root
        root = Path(agents_root()).resolve()
        return Path(path).resolve().relative_to(root).parts[0]
    except Exception:  # noqa: BLE001 — not under an agent dir, or unresolvable
        return None


def agent_queue_unverified(path) -> Optional[str]:
    """The agent name when a read of `path` cannot be verified from this box.

    Returns the AGENT NAME (truthy) when this box does not hold the live runner
    claim for the agent dir `path` lives under, and `None` otherwise. The name
    rather than a bare bool because every caller needs it for the message.

    `None` is returned — deliberately fail-open — when the path is not under an
    agent dir, when the ownership provenance is anything but "live-claims"
    (local-backend, transient-error, unknown-machine), or when the consult
    itself failed.
    """
    agent = agent_name_for(path)
    if agent is None:
        return None
    # The fail-open guarantee belongs at the PUBLIC boundary, not only inside
    # `_owned_now`. The endpoints call this function; a read must never fail
    # because the disclosure could not be computed, and that must stay true if a
    # later edit lets the consult raise. Caught here rather than trusted to the
    # layer below — the module docstring promises fail-open in every direction,
    # and a promise the boundary does not keep is a defect (the first draft of
    # this file propagated a raising consult straight through; its own test
    # caught it).
    try:
        owned, provenance = _owned_now()
    except Exception:  # noqa: BLE001 — advisory input; never fail a read on it
        return None
    if provenance != "live-claims":
        return None
    if agent in owned:
        return None
    return agent


def unverified_detail(agent: str, what: str) -> str:
    """The message body shared by every refusal this module motivates.

    ONE literal, referenced by both endpoints — two strings that must agree is
    a drift surface, and this one has to keep naming the correct remedy.
    """
    return (
        f"{what} agent dir {agent!r} is NOT owned by this box: it does not hold "
        f"the live runner claim, so the local mirror of that queue is "
        f"structurally behind the store of record and its MISSING rows are "
        f"always the NEWEST ones — i.e. exactly the closes. An empty or "
        f"not-found answer from here is therefore NOT evidence of absence "
        f"(guard-2302, guard-6156, guard-6166). Read it authoritatively "
        f"instead: `bash core/scripts/backend-cat.sh cat agents/{agent}/"
        f"aspirations.jsonl`, or corroborate with `backend-cat.sh head "
        f"agents/{agent}/aspirations.jsonl --exit-on-drift` (rc 3 = DRIFT). "
        f"Rows that DO come back from this queue are stamped "
        f"`read_from={UNVERIFIED}`."
    )
