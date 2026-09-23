"""Verdict-logic tests for _claim_liveness.py (, guard-1151 Layer B).

The wrapper claim-liveness-check.sh maps: LIVE/INDETERMINATE -> exit 0
(fail-open), STALE -> exit 1. These tests pin the pure classification —
the harm asymmetry is encoded here: any unreadable/missing input MUST be
INDETERMINATE (never STALE), because a wrong STALE would refuse a
legitimate daemon restart.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from _claim_liveness import verdict  # noqa: E402


def _asp(goals):
    return json.dumps({"id": "asp-115", "goals": goals})


def _goal(**kw):
    base = {"id": "g-115-1", "status": "in-progress", "claimed_by": "alpha"}
    base.update(kw)
    return base


def test_live_claim():
    kind, reason = verdict(_asp([_goal()]), "alpha", "g-115-1")
    assert kind == "LIVE"
    assert "alpha" in reason


def test_stale_when_completed():
    kind, reason = verdict(_asp([_goal(status="completed")]), "alpha", "g-115-1")
    assert kind == "STALE"
    assert "completed" in reason


def test_stale_when_released_to_pending():
    kind, _ = verdict(_asp([_goal(status="pending", claimed_by=None)]),
                      "alpha", "g-115-1")
    assert kind == "STALE"


# ── : `pending` + OUR OWN claim is LIVE, not STALE ────────────────
# aspirations-claim.sh writes claimed_by/claimed_at/started/executed_by and
# leaves status at 'pending', so this is the shape the framework's own claim
# script PRODUCES. The pre-fix chain returned STALE for it, which made
# mind-api-start.sh --restart exit 3 and the post-commit recycle silently
# refuse — a committed daemon-code fix never reached the live daemon
# (guard-7213). The pair below is the discriminator: identical status, the
# ONLY difference is whether the claim is ours.
def test_live_when_pending_but_claim_is_ours():
    kind, reason = verdict(_asp([_goal(status="pending", claimed_by="alpha")]),
                           "alpha", "g-115-1")
    assert kind == "LIVE"
    assert "pending" in reason


def test_pending_arm_requires_our_own_claim():
    # Same status as above; a FOREIGN claim must still be STALE, which is what
    # keeps the new arm from widening the gate (release() pops claimed_by, so
    # a released claim lands on the claimed_by=None case pinned just above).
    kind, _ = verdict(_asp([_goal(status="pending", claimed_by="bravo")]),
                      "alpha", "g-115-1")
    assert kind == "STALE"


def test_pending_arm_does_not_admit_terminal_statuses():
    # The arm is keyed on 'pending' exactly — every terminal status must keep
    # falling through to STALE even when the claim is still stamped as ours.
    for st in ("completed", "skipped", "expired", "superseded", "decomposed",
               "blocked"):
        kind, _ = verdict(_asp([_goal(status=st, claimed_by="alpha")]),
                          "alpha", "g-115-1")
        assert kind == "STALE", f"status={st!r} must remain STALE"


def test_pending_arm_sits_below_the_read_quality_guards():
    # guard-6943: the new branch must not outrank the INDETERMINATE guards —
    # an absent record must stay fail-open even though the id and agent match.
    kind, _ = verdict(_asp([_goal(status="pending", claimed_by="alpha")]),
                      "alpha", "g-115-NOT-PRESENT")
    assert kind == "INDETERMINATE"


def test_stale_when_taken_over():
    # status stays in-progress but another agent owns the claim now.
    kind, reason = verdict(_asp([_goal(claimed_by="bravo")]), "alpha", "g-115-1")
    assert kind == "STALE"
    assert "bravo" in reason


def test_indeterminate_on_unparseable():
    kind, _ = verdict("not json at all", "alpha", "g-115-1")
    assert kind == "INDETERMINATE"


def test_indeterminate_on_goal_missing():
    kind, _ = verdict(_asp([_goal(id="g-115-2")]), "alpha", "g-115-1")
    assert kind == "INDETERMINATE"


def test_bare_goal_record_accepted():
    kind, _ = verdict(json.dumps(_goal()), "alpha", "g-115-1")
    assert kind == "LIVE"


def test_list_payload_accepted():
    kind, _ = verdict(json.dumps([_goal()]), "alpha", "g-115-1")
    assert kind == "LIVE"


def test_aspiration_wrapper_shape_accepted():
    payload = json.dumps({"aspiration": {"id": "asp-115", "goals": [_goal()]}})
    kind, _ = verdict(payload, "alpha", "g-115-1")
    assert kind == "LIVE"


def test_goal_id_key_variant_accepted():
    g = _goal()
    g["goal_id"] = g.pop("id")
    kind, _ = verdict(_asp([g]), "alpha", "g-115-1")
    assert kind == "LIVE"


# ── Wiring: WHICH claim the gate reads () ──────────────────────────
# Everything above pins the CLASSIFIER, and all of it stayed green through a
# scope defect in the one call site that consumes it — guard-1943's lesson that
# pinning a decision says nothing about the wiring. mind-api-start.sh read
# `agent_status.<agent>.in_flight.goal_id`, a row team-state-in-flight.sh stamps
# ONLY for the Body holding this box's running-session-id. One agent NAME runs on
# many machines, so on every worker box that row named the REDUCER's goal on
# ANOTHER machine and the gate refused a MACHINE-LOCAL daemon recycle from a
# FLEET-WIDE claim. Measured cc-09 2026-09-03 (refused citing , held by
# alpha on cc-04) and re-measured cc-08 2026-09-10 (read , held by sid
# d647fb30 on cc-04 at status=pending -> STALE -> --restart exited 3, daemon
# untouched, while this Body's OWN claim read LIVE).
#
# INVARIANT: the Body-keyed row is read FIRST, and the agent-keyed row — the
# reducer's — may be read only under a running-session-id locality guard
# (running-session-id is sync_tier machine_local, so its presence IS the test).
_START_SH = _SCRIPTS / "mind-api-start.sh"

_AGENT_ROW_READ = "agent_status.${MIND_AGENT}.in_flight.goal_id"
_BODY_ROW_READ = "agent_status.${MIND_AGENT}.in_flight_bodies.${MIND_SID}.goal_id"


def _claim_gate_scope(text):
    """(ok, reason) — is the claim-liveness gate's claim read box-scoped?"""
    if _BODY_ROW_READ not in text:
        return False, "no Body-keyed in_flight_bodies.<sid> read: the gate cannot see its own claim"
    body_at = text.index(_BODY_ROW_READ)
    agent_at = text.find(_AGENT_ROW_READ)
    if agent_at == -1:
        return True, "Body-keyed read only"
    if agent_at < body_at:
        return False, "agent-keyed row read BEFORE the Body-keyed one: a peer's claim wins"
    guard_at = text.find("running-session-id", body_at)
    if guard_at == -1 or guard_at > agent_at:
        return False, "agent-keyed row read with no running-session-id locality guard"
    return True, "Body-keyed first; agent-keyed row guarded by running-session-id"


def test_claim_gate_reads_a_box_scoped_claim():
    ok, reason = _claim_gate_scope(_START_SH.read_text(encoding="utf-8"))
    assert ok, "mind-api-start.sh claim gate is not box-scoped: " + reason


def test_claim_gate_scope_check_rejects_the_pre_fix_shape():
    """Positive control: the checker must FAIL the shape that actually shipped.

    Without this, a checker that silently matched nothing would pass forever —
    the same always-green failure the classifier tests above demonstrated.
    """
    pre_fix = (
        'if [ -n "${MIND_AGENT:-}" ]; then\n'
        '    _clc_gid=$(bash "$SCRIPT_DIR/team-state-read.sh" '
        '--field "agent_status.${MIND_AGENT}.in_flight.goal_id" --json)\n'
        "fi\n"
    )
    ok, reason = _claim_gate_scope(pre_fix)
    assert not ok, "the checker passed the pre-fix agent-keyed read"
    assert "in_flight_bodies" in reason
