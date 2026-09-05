#!/usr/bin/env python3
"""Delivery-gated dependency release — is a blocker's DELIVERABLE reachable?

A blocked goal has always been released on its blocker's STATUS. Nothing asked
whether the blocker's deliverable exists anywhere the next executor can reach.
So a worker-closed blocker released downstream work while its commit lived only
on `refs/workers/**`, and the downstream executor either rebuilt work that
already existed or built against an interface that had not landed — both
silently, because nothing in the pickup path can see the difference (g-306-442).

WHAT THIS MODULE IS: the single predicate both release paths ask, so the hold
rule cannot drift between them. It OWNS no state, writes nothing, and is safe
to call from inside a store write.

THE FAIL DIRECTION IS THE WHOLE DESIGN, and it is deliberately the OPPOSITE of
the prober's underneath it. `commit-reachability.py` must never resolve an
unreadable signal to LANDED, because there a false LANDED ends an
investigation. Here the blast radius is inverted: a false HOLD freezes a
dependent goal for every agent on every box, and a git hiccup would freeze the
whole blocked population at once. A false RELEASE merely reproduces the
behaviour that shipped for months. So:

    only a DEFINITIVE "not reachable" holds.

INCONCLUSIVE (the probe could not run), a missing sha, an unloadable prober and
any unrecognised verdict all resolve to UNKNOWN, which RELEASES. That is not
timidity: it keeps an infrastructure blip from composing with a correct safety
mechanism into a fleet-wide dead end (the two-safety-mechanisms trap).

STATELESS BY CONTRACT (g-306-442 outcome 2: "the reachability probe runs at read
time and does not inherit a previously recorded verdict"). This function reads
NO stored verdict and writes none. A cached verdict is the one thing that would
break the design: a deliverable stranded at close time becomes reachable later,
when the carrier ref is consumed, and a stamped verdict would hold the dependent
forever precisely because nothing would ever re-derive it. Probe fresh, always.
Callers must not add a cache in front of this.
"""

import importlib.util
import os

DELIVERED = "delivered"   # reachable from the target ref — release the dependent
PENDING = "pending"       # DEFINITIVELY not reachable — hold the dependent
UNKNOWN = "unknown"       # could not tell — RELEASE (fail open); see module docstring

# Verdicts from commit-reachability.py that are a definitive "not reachable".
# Enumerated rather than derived as "not LANDED" on purpose: an unrecognised
# verdict string (a future value, a typo, a partial upgrade across boxes) must
# fall through to UNKNOWN and release, never to PENDING and freeze.
_STRANDED_VERDICTS = frozenset({
    "STRANDED_WORKER_REF",
    "STRANDED_REMOTE_BRANCH",
    "STRANDED_LOCAL_ONLY",
})
_LANDED_VERDICT = "LANDED"


def _load_prober(script_dir=None):
    """Load commit-reachability.py by path (its name is not importable).

    Same loader shape completed-not-committed-sweep.py uses. Returns None on any
    failure — the caller then reports UNKNOWN and releases.
    """
    try:
        base = script_dir or os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(base, "commit-reachability.py")
        if not os.path.exists(path):
            return None
        spec = importlib.util.spec_from_file_location("_commit_reachability", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def blocker_deliverable_sha(goal):
    """The sha a blocker goal claims to have delivered, or None.

    Only the REGISTERED `commit_sha` field counts. Deliberately not scraped from
    outcome_note prose: a sha mentioned in a narrative may be a commit that was
    read, reverted, or cited from another repo, and holding real work on a regex
    over free text is exactly the false HOLD this module's fail direction
    forbids. A blocker with no recorded sha reports UNKNOWN and releases.
    """
    if not isinstance(goal, dict):
        return None
    sha = goal.get("commit_sha")
    if not isinstance(sha, str):
        return None
    sha = sha.strip()
    return sha or None


def blocker_delivery_state(goal, repo=".", target_ref="origin/main",
                           script_dir=None, prober=None):
    """(state, detail) for ONE blocker goal. Probes fresh; caches nothing.

    state is DELIVERED / PENDING / UNKNOWN. Only PENDING holds a dependent.
    `prober` is injectable so tests can drive every branch without a git fixture.
    """
    sha = blocker_deliverable_sha(goal)
    if not sha:
        return UNKNOWN, "blocker records no commit_sha — cannot assess delivery"

    mod = prober if prober is not None else _load_prober(script_dir)
    if mod is None or not hasattr(mod, "triage"):
        return UNKNOWN, "commit-reachability prober unavailable"

    try:
        result = mod.triage(repo, sha, target_ref=target_ref)
    except Exception as exc:
        return UNKNOWN, f"reachability probe raised: {exc}"

    if not isinstance(result, dict):
        return UNKNOWN, "reachability probe returned no verdict"

    verdict = result.get("verdict")
    if verdict == _LANDED_VERDICT:
        return DELIVERED, f"{sha} is reachable from {target_ref}"
    if verdict in _STRANDED_VERDICTS:
        reason = result.get("reason") or verdict
        return PENDING, f"{sha} not reachable from {target_ref}: {reason}"
    # INCONCLUSIVE, or anything this version does not recognise.
    return UNKNOWN, f"reachability verdict {verdict!r} is not decisive"


def held_blocker_ids(goal_lookup, blocked_by, repo=".", target_ref="origin/main",
                     script_dir=None, prober=None):
    """Of `blocked_by`, which entries must STAY because delivery is pending.

    `goal_lookup` maps goal-id -> goal record (missing id -> no record -> the
    entry is not held here; a blocker that cannot be found is the status layer's
    problem, not delivery's). Returns (held_ids, details) with details keyed by
    goal id so a caller can record WHY a hold happened — an unexplained hold is
    the failure mode a structured signal exists to prevent.
    """
    held, details = [], {}
    for bid in blocked_by or []:
        blocker = (goal_lookup or {}).get(bid)
        if not blocker:
            continue
        state, detail = blocker_delivery_state(
            blocker, repo=repo, target_ref=target_ref,
            script_dir=script_dir, prober=prober)
        if state == PENDING:
            held.append(bid)
            details[bid] = detail
    return held, details
