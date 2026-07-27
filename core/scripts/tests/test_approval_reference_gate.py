"""test_approval_reference_gate.py — unit test for gates.approval_reference ().

The approval-reference advisory warns on the fabricated-approval shape verified
in g-115-2855: a HIGH-BLAST-RADIUS goal that ASSERTS prior approval but carries
NO verifiable approval reference. WARN-only (never blocks) — the
description_length.py precedent. Detection is the narrow conjunction
(approval_assertion AND high_blast_radius AND NOT verifiable_ref); a false
positive would require an approval-asserting, high-blast-radius goal that names
no reference at all.

Same sys.path import shape as test_capability_gate_narrative.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

from gates.approval_reference import evaluate  # noqa: E402


def _warned(title, description, recurring=False):
    goal = {"title": title, "description": description}
    if recurring:
        goal["recurring"] = True
    return evaluate(goal, source="world", meta_dir=None)["warned"]


def test_canonical_g_115_2854_warns():
    # The near-miss shape: asserts approval + high blast (L1 split, 560 nodes)
    # + no verifiable reference.
    assert _warned(
        "Execute approved L1 split: promote npc-intelligence to a new top-level L1",
        "Tree-structure owner (bravo) APPROVED the S8 intelligence-L1 split on "
        "2026-07-21. 560 nodes.",
    ) is True


def test_decisions_board_ref_suppresses():
    assert _warned(
        "Execute approved L1 split: promote to top-level",
        "Approved per decisions-board msg-20260721-120000-user-1234; 560 nodes "
        "restructure.",
    ) is False


def test_user_directive_ref_suppresses():
    assert _warned(
        "Execute approved tree restructure",
        "User-directive granted this reparent; approved in g-353-01.",
    ) is False


def test_changelog_ref_suppresses():
    assert _warned(
        "Execute approved bulk delete of retired-agent objects",
        "Purge approved; the changelog records the retirement authorization. "
        "500 nodes.",
    ) is False


def test_benign_framework_goal_no_warn():
    # The goal that FILED this gate — talks about approval/high-blast but
    # asserts nothing.
    assert _warned(
        "Idea: goal-creation gate refusing approval-asserting goals",
        "Build a gate that flags high-blast-radius goals lacking a verifiable "
        "approval reference.",
    ) is False


def test_approval_but_low_blast_no_warn():
    assert _warned(
        "Execute approved retry-logic fix",
        "The owner approved the change to deploy.sh retry backoff. One-liner.",
    ) is False


def test_high_blast_but_no_approval_no_warn():
    assert _warned(
        "Investigate: L1 split feasibility for the tree",
        "Should we restructure the tree? 560 nodes would move. Assess only.",
    ) is False


def test_recurring_exempt():
    assert _warned(
        "Execute approved L1 split 560 nodes",
        "owner approved the split, no ref",
        recurring=True,
    ) is False


def test_negated_not_approved_no_warn():
    # A goal ABOUT a rejected approval must not trip the assertion detector,
    # including the title→description newline-join edge (was the first-pass
    # false positive: "not approved\nThe").
    assert _warned(
        "Investigate why the L1 split was not approved",
        "The 560-node restructure was not approved; document why.",
    ) is False


def test_negated_never_approved_no_warn():
    assert _warned(
        "Note: this L1 restructure was never approved",
        "The 560-node reparent was never approved by anyone.",
    ) is False


def test_actor_approved_the_split_warns():
    assert _warned(
        "Execute the promotion",
        "alpha approved the split of the 800 nodes into a new top-level L1; "
        "proceeding.",
    ) is True


def test_return_shape_and_triggers():
    r = evaluate(
        {"title": "Execute approved L1 split", "description": "560 nodes, no ref"},
        source="agent", meta_dir=None,
    )
    assert set(r.keys()) >= {"warned", "message", "telemetry_written", "triggers"}
    assert r["warned"] is True
    assert r["message"] is not None and "APPROVAL-REFERENCE" in r["message"]
    assert r["triggers"] == {
        "approval_assertion": True,
        "high_blast_radius": True,
        "verifiable_ref": False,
    }
    assert r["telemetry_written"] is False  # meta_dir=None


if __name__ == "__main__":  # allow direct-run in addition to pytest collection
    import types
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and isinstance(v, types.FunctionType)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
