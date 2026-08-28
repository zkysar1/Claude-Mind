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
