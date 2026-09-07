"""Behavior tests for user_leg_scope advisory (PR 7c/5).

Pure check. Returns warning text when participants include 'user' but
user_leg_scope is absent.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "core" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from gates.user_leg_scope import evaluate, VALID_USER_LEG_SCOPES


def test_user_participant_no_scope_warns():
    out = evaluate(
        goal_id="g-001-01",
        participants=["agent", "user"],
        user_leg_scope=None,
    )
    assert out["warned"] is True
    assert "g-001-01" in out["message"]
    assert "user_leg_scope" in out["message"]


def test_user_participant_with_scope_passes():
    out = evaluate(
        goal_id="g-001-01",
        participants=["agent", "user"],
        user_leg_scope="commit",
    )
    assert out["warned"] is False
    assert out["message"] is None


def test_no_user_participant_passes():
    out = evaluate(
        goal_id="g-001-01",
        participants=["agent"],
        user_leg_scope=None,
    )
    assert out["warned"] is False
    assert out["participants_include_user"] is False


def test_empty_participants_passes():
    out = evaluate(
        goal_id="g-001-01",
        participants=[],
        user_leg_scope=None,
    )
    assert out["warned"] is False


def test_non_list_participants_passes():
    """Pathological input — non-list participants treated as no user."""
    out = evaluate(
        goal_id="g-001-01",
        participants="user",  # string, not list
        user_leg_scope=None,
    )
    assert out["warned"] is False


def test_empty_scope_warns():
    """user_leg_scope='' is falsy → warn (same as None)."""
    out = evaluate(
        goal_id="g-001-01",
        participants=["user"],
        user_leg_scope="",
    )
    assert out["warned"] is True


def test_warning_lists_valid_scopes():
    out = evaluate(
        goal_id="g-001-01",
        participants=["user"],
        user_leg_scope=None,
    )
    msg = out["message"]
    for scope in VALID_USER_LEG_SCOPES:
        assert scope in msg, f"valid scope {scope!r} should appear in warning"


def test_custom_valid_scopes():
    """Caller can override the scope list shown in the warning."""
    out = evaluate(
        goal_id="g-001-01",
        participants=["user"],
        user_leg_scope=None,
        valid_scopes={"custom-scope-a", "custom-scope-b"},
    )
    assert "custom-scope-a" in out["message"]
    assert "custom-scope-b" in out["message"]
    # Default scopes should NOT appear
    assert "deployment-approval" not in out["message"]


def test_canonical_scope_set_unchanged():
    """If the canonical set ever changes, this test reminds the author to
    update aspirations.py's duplicate VALID_USER_LEG_SCOPES constant.

    THE `==` IS EXACT ON PURPOSE — do not relax it to a subset/superset check
    to clear a red. An exact set assertion catches TWO classes: a rename/drop,
    AND an addition nobody intended. Relaxing it keeps the first and silently
    retires the second forever, and because the test then passes, nothing ever
    surfaces what was given up (guard-4223). When a legitimate addition lands,
    RESTATE this literal at its new size instead: print the running set, diff
    it into added/renamed/dropped, and stop if dropped is non-empty.
    """
    assert VALID_USER_LEG_SCOPES == frozenset({
        "commit", "push", "deployment-approval",
        "architecture-decision", "credential-grant",
        "data-provision", "new-resource",
        # Added 2026-08-28 (zeta,  / ). Its sibling
        # aspirations.py VALID_USER_LEG_SCOPES was updated in the same change —
        # which is exactly what this pin exists to remind the author to do.
        "principal-identity",
        # Added 2026-08-28 (50e45661cf, ), landed on main 2026-09-07
        # by the  worker-ref drain. BOTH modules were synced in that
        # change (user_leg_scope.py and aspirations.py each read 9, identical
        # sets — measured) and this pin was the third site, missed. Restated
        # per guard-4223: added=['human-window'], dropped=[] at restate time.
        "human-window",
    })
