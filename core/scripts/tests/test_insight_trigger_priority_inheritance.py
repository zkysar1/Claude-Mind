#!/usr/bin/env python3
""" item (2), producer half: an Apply-chore inherits the priority of
the goal it unblocks.

WHY THIS EXISTS. The filed priority came from the BOARD POST'S severity, which
measures how important the FINDING is and says nothing about how much work is
queued behind the chore. Measured incident (g-115-6243): a one-command chore
filed LOW while three HIGH goals sat blocked behind it — rank 10 for 36 hours.
Severity and blocking-cost are different quantities.

WHAT THIS SEAM EXCLUDES (guard-1462). Every test here drives the PURE
`inherit_priority` or `_build_goal_payload` with a hand-built record. The store
scan (`probe_goal_record`), the filing loop's stash of `_target_record`, and the
daemon write are all upstream and structurally unfalsifiable here; the existing
sweep suites cover the loop, and the 80-test regression run covers the contract.

Anti-vacuity guard: `test_the_inheritance_cases_do_not_collapse`. Mutate against
THAT ALONE (guard-1793).
"""
import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "..", "insight-trigger-sweep.py")
_spec = importlib.util.spec_from_file_location("its_mod", _SRC)
its = importlib.util.module_from_spec(_spec)
sys.modules["its_mod"] = its
_spec.loader.exec_module(its)


def _trigger(severity="informs", target_record=None):
    t = {
        "msg_id": "msg-test-1", "author": "bravo", "channel": "findings",
        "timestamp": "2026-08-28T00:00:00", "text": "body", "tags": ["x"],
        "action": "do the thing", "target": "either", "severity": severity,
        "affects_goal": "g-115-6243",
    }
    if target_record is not None:
        t["_target_record"] = target_record
    return t


def _goal(priority="HIGH", category="framework", gid="g-115-6243"):
    return {"id": gid, "priority": priority, "category": category,
            "status": "pending"}


# ── the pure helper ──────────────────────────────────────────────────────────

def test_low_chore_blocking_a_high_goal_is_promoted():
    """The  shape, and the whole reason this exists."""
    assert its.inherit_priority("LOW", "HIGH") == "HIGH"


def test_high_chore_is_never_demoted_by_a_low_target():
    """Inheritance takes a MAX. A chore that is independently urgent must not be
    dragged down by the priority of what happens to depend on it."""
    assert its.inherit_priority("HIGH", "LOW") == "HIGH"


def test_equal_priorities_are_unchanged():
    assert its.inherit_priority("MEDIUM", "MEDIUM") == "MEDIUM"


def test_absent_target_priority_leaves_the_chore_alone():
    assert its.inherit_priority("MEDIUM", None) == "MEDIUM"


def test_unrecognised_target_priority_never_promotes():
    """A typo or a new vocabulary value must not silently become HIGH."""
    assert its.inherit_priority("LOW", "URGENT") == "LOW"
    assert its.inherit_priority("LOW", "") == "LOW"


def test_unrecognised_own_priority_falls_back_to_the_target():
    assert its.inherit_priority("WEIRD", "HIGH") == "HIGH"
    assert its.inherit_priority("WEIRD", "ALSO-WEIRD") == "WEIRD"


# ── the payload ──────────────────────────────────────────────────────────────

def test_payload_promotes_and_records_where_it_came_from():
    p = its._build_goal_payload(_trigger("informs", _goal("HIGH")))
    assert p["priority"] == "HIGH"                      # not LOW from severity
    assert "priority-inherited-from:g-115-6243" in p["tags"]
    assert "Priority inherited" in p["description"]


def test_payload_without_a_target_is_untouched():
    """NEGATIVE CONTROL — the no-regression property. A trigger with no resolved
    target must produce exactly the severity-derived priority, no inheritance
    tag, and no category key invented from nowhere."""
    p = its._build_goal_payload(_trigger("informs"))
    assert p["priority"] == "LOW"
    assert not any(t.startswith("priority-inherited-from:") for t in p["tags"])
    assert "Priority inherited" not in p["description"]
    assert "category" not in p


def test_payload_does_not_annotate_when_nothing_was_promoted():
    """A target that does not RAISE the priority must leave no inheritance
    trace — otherwise the tag stops meaning 'this was promoted'."""
    p = its._build_goal_payload(_trigger("invalidates", _goal("LOW")))
    assert p["priority"] == "HIGH"
    assert not any(t.startswith("priority-inherited-from:") for t in p["tags"])


def test_category_is_inherited_but_never_invented():
    p = its._build_goal_payload(_trigger("informs", _goal(category="coordination")))
    assert p["category"] == "coordination"
    p2 = its._build_goal_payload(_trigger("informs", _goal(category=None)))
    assert "category" not in p2


# ── the relay floor () ────────────────────────────────────────────
# A relayed claim-fenced store write carries no severity tag, so it parsed as
# informs -> LOW and was never selected ( / ). Literal
# action types below, never iterated from the constant: an emptied constant
# must FAIL these, not pass them vacuously.

def _relay(action, severity="informs"):
    t = _trigger(severity)
    t["action"] = action
    return t


def test_untagged_relay_from_a_real_post_files_at_medium():
    """End to end from a board post's tags: no severity tag at all."""
    from datetime import datetime
    now = datetime(2026, 9, 27, 18, 0, 0)
    msg = {"id": "msg-test-relay", "author": "alpha", "text": "close pq-x",
           "timestamp": "2026-09-27T11:48:45",
           "tags": ["requires_action_by:alpha@ayoai-mind", "action_type:pq-close"]}
    trig = its._parse_trigger_msg(msg, "coordination", now,
                                  datetime(2026, 9, 27, 11, 48, 45))
    assert trig["severity"] == "informs"   # untagged still parses as informs
    assert its._build_goal_payload(trig)["priority"] == "MEDIUM"


def test_relayed_store_writes_are_floored_at_medium():
    # land-write: the verb NO_CLAIM_MESSAGE names. The rest: the verbs relayers
    # used before it named one (measured 2026-09-28, see RELAY_ACTION_TYPES).
    for action in ("land-write", "pq-close", "close-pending-questions",
                   "experience-add", "register", "register-experience"):
        p = its._build_goal_payload(_relay(action))
        assert p["priority"] == "MEDIUM", action
        assert "Priority floor" in p["description"], action


def test_relay_verbs_match_whatever_the_relayer_typed():
    """action_type is free text, and some posts spell verbs with underscores."""
    for action in ("land_write", "PQ-Close", "Register_Experience"):
        assert its._build_goal_payload(_relay(action))["priority"] == "MEDIUM", action


def test_other_untagged_action_types_stay_low():
    """NEGATIVE CONTROL: the floor is for relays only. apply and execute are
    left out on purpose: most addressed posts using them are not relays."""
    for action in ("audit-orphan-census", "apply", "execute"):
        p = its._build_goal_payload(_relay(action))
        assert p["priority"] == "LOW", action
        assert "Priority floor" not in p["description"], action


def test_the_floor_never_lowers_a_relay():
    assert its._build_goal_payload(_relay("pq-close", "invalidates"))["priority"] == "HIGH"


def test_relay_action_types_live_in_one_named_constant():
    assert {"land-write", "pq-close", "experience-add"} <= its.RELAY_ACTION_TYPES


# ── anti-vacuity (mutate THIS one, guard-1793) ───────────────────────────────

def test_the_inheritance_cases_do_not_collapse():
    """Five inputs that must NOT all answer the same way.

    A helper hardwired to return HIGH would satisfy the promotion test above on
    its own; a helper that never promotes would satisfy the no-demotion test.
    This is the assertion that fails for either.
    """
    got = {
        "promote":      its.inherit_priority("LOW", "HIGH"),
        "no_demote":    its.inherit_priority("HIGH", "LOW"),
        "equal":        its.inherit_priority("MEDIUM", "MEDIUM"),
        "absent":       its.inherit_priority("MEDIUM", None),
        "unrecognised": its.inherit_priority("LOW", "URGENT"),
    }
    assert got == {"promote": "HIGH", "no_demote": "HIGH", "equal": "MEDIUM",
                   "absent": "MEDIUM", "unrecognised": "LOW"}, got
    assert len(set(got.values())) == 3   # HIGH, MEDIUM, LOW all reachable
