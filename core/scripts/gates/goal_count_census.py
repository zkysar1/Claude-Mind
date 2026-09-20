"""Cross-call goal-count census for the update-goal write path ().

WHY THIS EXISTS
---------------
Relayed from ZDS-Mind (omni, cc-06, 2026-09-17; full evidence on ZDS g-022-78):
a single-field `update-goal` write shrank the world queue by 192,787 bytes and
removed 29 goal records at once. Pinned by bisecting the copy-on-write snapshot
series on `size_bytes`. NOT a retention prune — the retirement store had been
untouched for 13 days AND 102 terminal goals survived the same write, so 29
records vanished while 102 comparable ones did not. That is a lost-update or
partial-rewrite signature, and it is higher severity than a prune, because a
prune is restricted to terminal rows by construction and a bad rewrite is not.

THE VECTOR, diagnosed at the dev origin (zeta, cc-02, 2026-09-20). `update_goal`
rewrites the WHOLE store from whatever `_read_jsonl` returned, under the lock,
with no count check between the read and the write. Torn lines are ruled out:
`_read_jsonl` does a bare `json.loads(line)` with no try/except, so a malformed
line RAISES and aborts the call. What remains is a SHORT BUT WELL-FORMED read —
`get_backend().refresh()` is best-effort, so a stale or partially-materialised
mirror yields a shorter `items` that parses perfectly, and the next statement
writes it back as the whole store.

WHY THE OBVIOUS CONTROL DOES NOT WORK, AND WHY THIS ONE IS CROSS-CALL
---------------------------------------------------------------------
The goal was filed asking to "assert live plus archived record count is
conserved ACROSS AN UPDATE-GOAL CALL". Within one call a short read CONSERVES
PERFECTLY: `before` is computed from the same short `items` the write uses, so
before == after and the assertion passes while 29 goals are dropped. An in-call
equality check is still worth having — it catches in-memory corruption between
read and write — but it is NOT this defect's detector, and shipping it as one
would have produced a gate that could never fire on the incident it was built
for (guard-2260: a remedy is a separate claim from its diagnosis).

Detecting a short read needs an expectation from OUTSIDE the call. Hence the
census: persist the goal count on every successful write, and compare the NEXT
read against it.

WHY THE EXISTING TELEMETRY IS BLIND, measured
---------------------------------------------
`changelog.append(..., lines_changed=len(items))` counts ASPIRATION records,
not goals — goals live inside each record's `goals[]` array. Measured on cc-02
over 866 `edit aspirations.jsonl` rows from `changelog-read.sh --limit 4000`:
`lines_changed` takes exactly THREE distinct values — 27 (467 rows), 26 (253),
28 (146) — against ~2,300 live goals. Positive control in the same read: 279
`board/coordination.jsonl` rows, so the scan was not filtering everything out.
29 goals vanishing from inside ONE aspiration record moves `lines_changed` by
ZERO. That is why the ZDS trace had to bisect snapshots on `size_bytes`.

REPORT-ONLY BY CONSTRUCTION AT THIS STAGE — READ THIS BEFORE WIRING A REFUSAL
-----------------------------------------------------------------------------
The result key is `anomalous`, NOT `blocked`, and that naming is deliberate so
a caller cannot mistake a census verdict for a refusal verdict by autocomplete.
This gate does not refuse, and `TOLERANCE` is 0 — it reports EVERY decrease.

That is not a threshold choice; it is the ABSENCE of one, recorded honestly.
`field_shrink.py` chose 0.25 by measuring who NEWLY gets refused across the
live corpus first. The equivalent measurement does not exist here yet: the
persisted count can legitimately lag real removals performed by OTHER endpoints
(archival, retirement, supersession), and nobody has measured how large those
batches are. Inventing a tolerance now would be the widen-blindly move
guard-1562 forbids. So stage 1 ships the instrument that PRODUCES the
measurement, and the number gets chosen from its telemetry.

Blast radius is why this ordering is not optional: `update_goal` is the fleet's
hot write path on a live daemon, and a gate that refuses wrongly wedges every
agent at once.

PROMOTION CRITERION, recorded at birth (guard-769)
--------------------------------------------------
Promote to a refusal ONLY when BOTH hold:
  (a) telemetry over 20+ `decrease-anomalous` firings separates genuine loss
      from legitimate cross-endpoint removal, giving a tolerance with measured
      headroom below 29 (the ZDS incident magnitude); and
  (b) the refusal has an override header mirroring `X-Mind-Override-Shrink`,
      and each branch that can reach the refusal message invites a rationale
      that is actually reachable in that branch (guard-5593).
Until then a `no-decrease`-heavy firing log is the census WORKING — the event
it watches for is rare and expensive, not common and cheap.

DESIGN NOTES
------------
No try/except anywhere in this module, deliberately — same rationale as
`field_shrink.py` (guard-3803): a gate's fail-open handler also covers its own
message construction, so a bug while COMPOSING a verdict silently converts it
into an all-clear. The predicate is integer arithmetic behind explicit
isinstance checks, so there is no dependency to fail. Callers may wrap the CALL
in a fail-open handler — the daemon does, because a census must never abort a
write — but must not swallow exceptions inside this module's contract.

Public API:
    count_goals(items) -> int
    evaluate(observed, expected) -> dict

`evaluate` return shape (every branch sets `decision_path` — guard-502):
    {
      "anomalous": bool,
      "message": str | None,   # pre-formatted report text; None when not anomalous
      "observed": int | None,
      "expected": int | None,
      "delta": int | None,     # observed - expected; negative means goals vanished
      "decision_path": str,    # unique per branch, for gate telemetry
    }
"""
from __future__ import annotations

# Report every decrease. NOT a measured threshold — see the module docstring.
# Raising this above 0 requires the telemetry described under PROMOTION
# CRITERION; do not pick a number to quiet the log.
TOLERANCE = 0


def count_goals(items) -> int:
    """Total goals across every aspiration record in `items`.

    This is the quantity `changelog.lines_changed` does NOT measure: it
    descends into each record's `goals[]` array instead of counting records.
    Records that are not dicts, and `goals` values that are not lists, are
    skipped rather than raising — a census must be computable over whatever
    the read returned, including a partially-materialised one.
    """
    total = 0
    if not isinstance(items, list):
        return 0
    for record in items:
        if not isinstance(record, dict):
            continue
        goals = record.get("goals")
        if isinstance(goals, list):
            total += len(goals)
    return total


def evaluate(observed, expected) -> dict:
    """Compare a just-read goal count against the count persisted last write.

    Args:
        observed: goal count computed from THIS call's read (see count_goals).
        expected: goal count persisted by the previous successful write, or
                  None when no expectation exists yet (bootstrap, or a store
                  that has never been written through the census).

    Pure — reads no files, no env, no clock. Never raises on ordinary input.
    """
    def _result(anomalous, decision_path, message=None,
                observed_v=None, expected_v=None, delta=None):
        return {
            "anomalous": anomalous,
            "message": message,
            "observed": observed_v,
            "expected": expected_v,
            "delta": delta,
            "decision_path": decision_path,
        }

    # bool is a subclass of int; exclude it explicitly so a stray True cannot
    # be compared as 1 and silently pass for a count.
    def _is_count(v):
        return isinstance(v, int) and not isinstance(v, bool)

    if not _is_count(observed):
        return _result(False, "non-integer-observed")

    # The bootstrap state, and the only state in which this census is blind.
    # Deliberately NOT anomalous: the first write through the census is what
    # establishes the expectation, and treating its absence as a finding would
    # make every fresh store and every never-censused store report on its first
    # touch. The cost of this branch is that the FIRST short read after a
    # bootstrap is invisible; the alternative is a gate nobody can adopt.
    if expected is None:
        return _result(False, "no-expectation", observed_v=observed)

    if not _is_count(expected):
        return _result(False, "non-integer-expected", observed_v=observed)

    delta = observed - expected

    if delta >= 0:
        # The overwhelmingly common case. `update_goal` mutates one goal in
        # place and never removes one, so a same-or-larger count is what a
        # healthy read looks like — including after another endpoint ADDED
        # goals between the two calls.
        return _result(False, "no-decrease",
                       observed_v=observed, expected_v=expected, delta=delta)

    if -delta <= TOLERANCE:
        return _result(False, "decrease-within-tolerance",
                       observed_v=observed, expected_v=expected, delta=delta)

    message = (
        f"goal_count_census: the store read for this update returned "
        f"{observed} goals against {expected} persisted by the previous "
        f"successful write — {-delta} goal(s) missing. `update_goal` never "
        f"removes a goal, so a decrease here is either a SHORT BUT "
        f"WELL-FORMED read (a stale or partially-materialised mirror; the "
        f"29-goal loss this census was built for, ZDS g-022-78) or a "
        f"legitimate removal by another endpoint (archival, retirement, "
        f"supersession) that has not yet refreshed the persisted count. "
        f"REPORT ONLY — this write was NOT refused. Before treating it as "
        f"loss, check `.history` for the pre-image and compare snapshot "
        f"size_bytes; before treating it as benign, name the endpoint that "
        f"removed them."
    )
    return _result(True, "decrease-anomalous", message=message,
                   observed_v=observed, expected_v=expected, delta=delta)
