""" — clearing the LAST `blocked_by` must also restore `status`.

THE DEFECT. Two writers clear a dependent's `blocked_by`; only one restored
`status`, and the one that did is the CONDITIONAL one. `dependent-unblock.py`
Step 1b flips blocked -> pending, but runs only when aspirations-verify Phase 5
calls it. `_clear_stale_blockers` fires AUTOMATICALLY on every terminal-status
transition (complete / complete_by / retire / archive_sweep / update_goal), so
it is the path that actually runs on most closes — and it removed the reference
and stopped. The residue is the reason-less-blocked state: status="blocked" with
blocked_by=[], blocked_since=None, blocker_ref=None. It is invisible to the
selector AND to every sweep keyed on blocked_by (guard-4041).

Measured twice in production: g-326-190 (2026-08-14, cc-02) and g-369-72
(2026-08-31, cc-05), the latter with its blocker's completion timestamp ~1h
earlier — which is why the SAME-BLOCKER case below is modelled on a real
transition rather than a synthesized one.

WHY BOTH MIRRORS ARE TESTED IN ONE FILE. The daemon copy in
`mind_api/src/endpoints/aspirations_write.py::_clear_stale_blockers_inline` is
the live path (daemon-only architecture); the CLI copy in
`core/scripts/aspirations.py::_clear_stale_blockers` is its documented twin.
guard-547 / guard-2323 require porting a twin in the same change, and a test that
pins only one of them cannot tell you the pair diverged — which is the failure
mode those guards exist for. Each is loaded the only way it can be — the daemon copy as a package module
(it uses relative imports), the CLI copy by file path (core/scripts is not a
package) — so the pair is pinned together without either loader constraining the
other.

The three must-NOT-flip guards are modelled on
core/scripts/tests/test_dependent_unblock_status_restore.py, deliberately: the
whole point of this change is that the two release paths now apply the SAME
guard triple, so they should fail the same way when it is wrong.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _daemon_clear():
    # Package import, NOT a file-path load: aspirations_write.py uses relative
    # imports, so loading it by path dies with "attempted relative import with
    # no known parent package". The CLI twin below has no package at all and so
    # must go the other way — the asymmetry is real, not an inconsistency.
    from mind_api.src.endpoints import aspirations_write as AW
    return AW._clear_stale_blockers_inline


def _cli_clear():
    import sys
    sys.path.insert(0, str(ROOT / "core/scripts"))
    mod = _load(ROOT / "core/scripts/aspirations.py", "_g6262_cli")
    fn = mod._clear_stale_blockers
    # delivery_gate=False keeps this a pure unit test of the clear/restore
    # branch: the gate's job is to HOLD a reference, and a held reference means
    # `cleaned` is non-empty, which is a different arm entirely (pinned below).
    return lambda items, resolved: fn(items, resolved, delivery_gate=False)


BOTH = pytest.mark.parametrize(
    "clear",
    [pytest.param(_daemon_clear, id="daemon"), pytest.param(_cli_clear, id="cli")],
)


def _items(goal: dict):
    return [{"id": "asp-1", "goals": [goal]}]


def _goal(**over):
    g = {"id": "g-1-1", "status": "blocked", "blocked_by": ["g-1-0"],
         "blocked_since": "2026-08-30T00:00:00"}
    g.update(over)
    return g


@BOTH
def test_last_blocker_cleared_restores_status(clear):
    """The  transition: sole blocker completes, reference is dropped,
    and the goal must come back to `pending` rather than sit reason-less."""
    items = _items(_goal())
    clear()(items, {"g-1-0"})
    g = items[0]["goals"][0]
    assert g["blocked_by"] == []
    assert g["blocked_since"] is None
    assert g["status"] == "pending"


@BOTH
def test_still_blocked_by_others_does_not_restore(clear):
    """Guard 1. Dropping ONE of several predecessors leaves a live block."""
    items = _items(_goal(blocked_by=["g-1-0", "g-1-2"]))
    clear()(items, {"g-1-0"})
    g = items[0]["goals"][0]
    assert g["blocked_by"] == ["g-1-2"]
    assert g["status"] == "blocked"
    assert g["blocked_since"] == "2026-08-30T00:00:00"


@BOTH
@pytest.mark.parametrize("status",
                         ["completed", "skipped", "in-progress", "expired",
                          "pending"])
def test_non_blocked_status_is_never_touched(clear, status):
    """Guard 2. Only `blocked` is ours to undo — a completed goal that still
    carried a stale reference must keep its terminal status."""
    items = _items(_goal(status=status))
    clear()(items, {"g-1-0"})
    g = items[0]["goals"][0]
    assert g["blocked_by"] == []
    assert g["status"] == status


@BOTH
def test_blocker_ref_holds_the_status(clear):
    """Guard 3. A structured blocker is a separate suppression axis owned by
    CREATE_BLOCKER; blocked_by going empty says nothing about whether THAT
    blocker cleared, so the reference drops and the status stays."""
    items = _items(_goal(blocker_ref="blk-7"))
    clear()(items, {"g-1-0"})
    g = items[0]["goals"][0]
    assert g["blocked_by"] == []
    assert g["status"] == "blocked"


@BOTH
def test_defer_reason_is_not_a_guard(clear):
    """defer and status are ORTHOGONAL axes. Gating the restore on
    defer_reason would re-strand exactly the goals that carry both — the
    reason dependent-unblock.py Step 1b calls this out explicitly."""
    items = _items(_goal(defer_reason="precondition_unmet: waiting on a window"))
    clear()(items, {"g-1-0"})
    g = items[0]["goals"][0]
    assert g["status"] == "pending"
    assert g["defer_reason"] == "precondition_unmet: waiting on a window"


@BOTH
def test_unrelated_blocker_is_a_no_op(clear):
    """Nothing resolved for this goal: no field moves, including status."""
    items = _items(_goal())
    clear()(items, {"g-9-9"})
    g = items[0]["goals"][0]
    assert g["blocked_by"] == ["g-1-0"]
    assert g["status"] == "blocked"
    assert g["blocked_since"] == "2026-08-30T00:00:00"


@BOTH
def test_legacy_string_blocked_by_is_promoted_and_restored(clear):
    """The legacy string shape still normalises to a list, and the restore
    applies to it too — otherwise the oldest records are the ones left
    stranded."""
    items = _items(_goal(blocked_by="g-1-0"))
    clear()(items, {"g-1-0"})
    g = items[0]["goals"][0]
    assert g["blocked_by"] == []
    assert g["status"] == "pending"
