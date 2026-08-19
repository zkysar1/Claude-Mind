""" — a capture lane reserves a floor of slots for UNFLAGGED entries.

WHY THE g-306-308 NEWCOMER GUARD WAS NOT ENOUGH, AND WHY THAT WAS INVISIBLE.
That guard says "the entry THIS call just added must not be its own victim", and
it is correct. But its protection is scoped to the CALL: `arr[_victim] is item`
is an identity test against the item this invocation holds. On the NEXT append
the previous newcomer is an ordinary unflagged peer, it sorts to index 0, and it
is popped. So N consecutive unflagged appends into a saturated all-flagged lane
kept exactly ONE — the last — while every single-call test passed.

Measured deterministically in g-306-314 against the real wm.py policy: survivors
were 1 at N=2, 1 at N=3, 1 at N=5 and 1 at N=10, i.e. 90% silent loss at N=10,
with the unsaturated positive control keeping all N. That is the gap this file
pins, and it is a UNIT mismatch rather than a logic error: the protection was
per-call and the pressure is per-window (guard-4236).

WHAT THE FLOOR COSTS, STATED PLAINLY. Above the floor nothing changes —
unflagged peers are still evicted before flagged ones, which
test_priority_is_preserved_above_the_floor pins. Below it the order inverts on
purpose and the oldest FLAGGED entry is evicted instead. That is a real cost and
it is the intended trade: a lane sitting at 100% `load_bearing` has no variance
left in the sort key, so it has stopped prioritising anything, and the flag has
degraded from a triage hint into an admission ticket (guard-4150). It is also
smaller than it looks — at 100% saturation today's policy ALREADY destroys the
oldest flagged entry on every append, so the floor bounds an existing loss class
rather than creating a new one.

WHAT THIS FILE DOES NOT CLAIM. The floor does not guarantee any particular
unflagged entry survives indefinitely; it guarantees the lane never goes DEAF to
unflagged content. Whether the reducer drains fast enough for a given entry to be
read is a rate question the evictor cannot answer (guard-2071).

TWIN-COPY TRAP. The runtime path is the DAEMON copy in
mind_api/src/endpoints/wm_write.py::append_slot, whose eviction loop is INLINE
rather than a call into wm.py; wrappers are daemon-only, so a wm.py-only edit is
inert at runtime (guard-742/547). test_both_copies_carry_the_floor is the
source-level pin for that, and it is the specific check guard-4236 step 2 asks
for.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

import wm  # noqa: E402

SLOT = "spark_capture"
CAP = 50    # memory-pipeline.yaml working_memory_pruning.array_limits.spark_capture
FLOOR = 10  # _unflagged_floor(50) — asserted independently below, not assumed


def _entry(n: int, load_bearing: bool, ts: str | None = None) -> dict:
    """One capture item in the shape the production append path produces.

    `_item_ts` is present because the append path stamps every dict item; a
    fixture without it would exercise a shape production never emits (guard-920).
    """
    return {
        "goal_id": f"g-306-316-{n:04d}",
        "category": "framework-architecture",
        "observation": f"entry {n}",
        "sq_trigger": None,
        "load_bearing": load_bearing,
        "_item_ts": f"{ts or '2026-08-14'}T00:{n // 60:02d}:{n % 60:02d}",
    }


def _ids(arr) -> list:
    return [e.get("goal_id") for e in arr if isinstance(e, dict)]


def _n_unflagged(arr) -> int:
    return sum(1 for e in arr if not wm._is_flagged(e))


def _append_then_enforce(arr, item, limit=CAP) -> int:
    """The production call shape: append, THEN enforce with `item` named.

    cmd_append does exactly this (wm.py: `arr.append(item)` then
    `enforce_slot_limit(arr, limit, item=item)`), so driving the helper this way
    exercises the branch production takes rather than a contrived one
    (guard-920 / rb-5235).
    """
    arr.append(item)
    return wm.enforce_slot_limit(arr, limit, item=item)


# --- the floor itself --------------------------------------------------------

@pytest.mark.parametrize("limit,expected", [
    (0, 0),      # no cap -> nothing to reserve
    (1, 0),      # a one-item lane cannot reserve a share of itself
    (2, 1),
    (5, 1),      # int(1.0) == 1
    (20, 4),     # exp_capture
    (50, 10),    # spark_capture
])
def test_unflagged_floor_boundaries(limit, expected):
    assert wm._unflagged_floor(limit) == expected


def test_floor_never_starves_flagged_entirely():
    """The reservation is a guarantee that the lane keeps HEARING unflagged
    content, not a demotion of load_bearing. At every cap there must remain at
    least one slot a flagged entry can occupy."""
    for limit in range(2, 200):
        assert wm._unflagged_floor(limit) <= limit - 1


def test_floor_matches_the_constant_this_file_asserts_against():
    """Guards the rest of the file: if the ratio moves, FLOOR here goes stale and
    the survivor assertions below would silently start testing the wrong number."""
    assert wm._unflagged_floor(CAP) == FLOOR


# --- THE regression: consecutive writes, not one -----------------------------

@pytest.mark.parametrize("n", [2, 3, 5, 10])
def test_n_consecutive_unflagged_appends_keep_the_floor_not_one(n):
    """THE defect, at the unit guard-4236 names. Pre-fix this kept exactly 1 for
    every n; the per-call guard saved only whichever entry was in flight."""
    arr = [_entry(i, True) for i in range(CAP)]
    for k in range(n):
        _append_then_enforce(arr, _entry(1000 + k, False, ts="2026-08-17"))

    survivors = [i for i in _ids(arr) if int(i.rsplit("-", 1)[1]) >= 1000]
    assert len(survivors) == min(n, FLOOR), (
        f"expected min(n={n}, floor={FLOOR}) unflagged survivors, got "
        f"{len(survivors)}: {survivors}. Exactly 1 is the pre-fix signature — "
        f"a per-call guard against per-window pressure (guard-4236)."
    )
    assert len(arr) == CAP, "the cap must still hold"


def test_the_abc_sequence_from_the_goal_no_longer_destroys_b():
    """The literal a/b/c reproduction the goal names: flagged a, unflagged b,
    unflagged c, into a saturated lane. Pre-fix, c's append destroyed b."""
    arr = [_entry(i, True) for i in range(CAP)]
    _append_then_enforce(arr, _entry(1, True, ts="2026-08-17"))    # a
    _append_then_enforce(arr, _entry(2, False, ts="2026-08-17"))   # b
    _append_then_enforce(arr, _entry(3, False, ts="2026-08-17"))   # c

    ids = _ids(arr)
    assert "g-306-316-0002" in ids, "b was destroyed by c's append (the defect)"
    assert "g-306-316-0003" in ids, "c must survive its own append (g-306-308)"


def test_beyond_the_floor_the_oldest_unflagged_yields_not_a_flagged_one():
    """The floor is a floor, not a ceiling: past it, unflagged entries resume
    absorbing the cap pressure and flagged peers are left alone."""
    arr = [_entry(i, True) for i in range(CAP)]
    for k in range(FLOOR + 3):
        _append_then_enforce(arr, _entry(1000 + k, False, ts="2026-08-17"))

    ids = _ids(arr)
    assert _n_unflagged(arr) == FLOOR
    assert "g-306-316-1000" not in ids, "the OLDEST unflagged should have yielded"
    assert "g-306-316-1012" in ids, "the newest unflagged must be present"


def test_priority_is_preserved_above_the_floor():
    """The fix must NOT flatten load_bearing into a no-op. With unflagged
    entries ABOVE the floor, an unflagged peer is still evicted before any
    flagged one — the property test_priority_order_is_preserved_for_peers pins
    in the newcomer file, re-pinned here in the region the floor does not
    govern."""
    arr = ([_entry(i, False, ts="2026-08-10") for i in range(FLOOR + 1)]
           + [_entry(100 + i, True, ts="2026-08-12")
              for i in range(CAP - FLOOR - 1)])
    assert len(arr) == CAP

    _append_then_enforce(arr, _entry(999, True, ts="2026-08-17"))

    ids = _ids(arr)
    assert "g-306-316-0000" not in ids, (
        "above the floor the oldest UNFLAGGED entry must still be the victim — "
        "the floor must not invert priority where it does not apply"
    )
    assert "g-306-316-0100" in ids, "no flagged peer should have been touched"
    assert "g-306-316-0999" in ids


# --- negative controls -------------------------------------------------------

def test_unsaturated_lane_keeps_all_n_positive_control():
    """The control that makes the survivor counts above mean something: below
    cap, nothing is evicted at all, so a low survivor count there would indict
    the harness rather than the policy."""
    arr = [_entry(i, True) for i in range(CAP - 20)]
    for k in range(10):
        assert _append_then_enforce(arr, _entry(1000 + k, False)) == 0
    assert len(arr) == CAP - 10
    assert len([i for i in _ids(arr) if int(i.rsplit("-", 1)[1]) >= 1000]) == 10


def test_an_all_unflagged_lane_is_untouched_by_the_floor():
    """Regression guard for the two existing callers' common case. When nothing
    is flagged the floor must be inert — otherwise every ordinary capped slot in
    memory-pipeline.yaml silently changes eviction order."""
    arr = [_entry(i, False) for i in range(12)]
    same = arr
    assert wm.enforce_slot_limit(arr, 4) == 8
    assert len(arr) == 4
    assert same is arr, "in-place is the contract both callers rely on"
    assert _ids(arr) == [f"g-306-316-{i:04d}" for i in range(8, 12)], (
        "oldest-first must be unchanged when the flag has no variance"
    )


def test_merge_path_honours_the_floor_with_no_item():
    """body-merge.py::merge_wm passes no `item` — it is the generalize-down path,
    where a worker Body's capture entries actually reach a reducer. The per-call
    guard never applied there at all, so the floor is the ONLY protection this
    path has ever had."""
    arr = ([_entry(i, False, ts="2026-08-10") for i in range(4)]
           + [_entry(100 + i, True, ts="2026-08-12") for i in range(CAP + 6)])

    evicted = wm.enforce_slot_limit(arr, CAP)

    assert evicted == 10
    assert len(arr) == CAP
    assert _n_unflagged(arr) == 4, (
        "all 4 unflagged sit below the floor of 10 and must survive; pre-fix "
        "every one of them was popped before any flagged entry was considered"
    )


# --- end-to-end through the real append path ---------------------------------

class _TempWM:
    """BODY_WM_PATH is the ONLY correct redirect — patching wm.WM_PATH is a no-op
    for I/O and would target the LIVE agent's WM (guard-862)."""

    def __enter__(self):
        self._orig = os.environ.get("BODY_WM_PATH")
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BODY_WM_PATH"] = str(Path(self._tmp.name) / "working-memory.yaml")
        wm.cmd_init(SimpleNamespace())
        return self

    def __exit__(self, *exc):
        if self._orig is None:
            os.environ.pop("BODY_WM_PATH", None)
        else:
            os.environ["BODY_WM_PATH"] = self._orig
        self._tmp.cleanup()
        return False


def _cmd_append(item: dict) -> None:
    saved = sys.stdin
    sys.stdin = io.StringIO(json.dumps(item))
    try:
        wm.cmd_append(SimpleNamespace(slot=SLOT))
    finally:
        sys.stdin = saved


def test_end_to_end_through_cmd_append_not_just_the_helper():
    """A green helper suite certifies the FUNCTION, never the WIRING (guard-1943).
    This drives the real cmd_append against a real WM file so a future refactor
    that stops routing through enforce_slot_limit fails here."""
    import yaml
    with _TempWM():
        for i in range(CAP):
            _cmd_append(_entry(i, True))
        for k in range(5):
            _cmd_append(_entry(1000 + k, False, ts="2026-08-17"))

        data = yaml.safe_load(
            Path(os.environ["BODY_WM_PATH"]).read_text(encoding="utf-8"))
        lane = (data.get("slots") or {}).get(SLOT) or []
        survivors = [e for e in lane if not e.get("load_bearing")]

        assert len(lane) == CAP
        assert len(survivors) == 5, (
            f"5 consecutive unflagged appends through the REAL append path must "
            f"leave 5 survivors (floor={FLOOR}), got {len(survivors)}"
        )


# --- the twin-copy pin (guard-4236 step 2) -----------------------------------

def test_both_copies_carry_the_floor():
    """guard-742/547: the daemon copy is the live path and its eviction loop is
    INLINE rather than a call into wm.py, so the two can silently diverge. This
    is the grep guard-4236 step 2 mandates, as an assertion instead of a habit."""
    cli = (CORE_SCRIPTS / "wm.py").read_text(encoding="utf-8")
    daemon = (CORE_SCRIPTS.parent.parent / "mind_api" / "src" / "endpoints"
              / "wm_write.py").read_text(encoding="utf-8")
    for name, src in (("wm.py", cli), ("wm_write.py", daemon)):
        # Pin the CALL SITE and the CONDITION, never the bare name. `"_unflagged_floor"
        # in src` is satisfied by the DEFINITION alone, so a copy that defines the
        # floor and never calls it passed — and that sabotage is not hypothetical:
        # it restores the exact pre- behaviour (pop the oldest unflagged,
        # 90% loss at N=10) in the LIVE daemon path. Both patterns were verified by
        # injecting that bug and confirming they fire (guard-385); `\(limit\)` cannot
        # match `def _unflagged_floor(limit: int)` (guard-1311); each matches exactly
        # one non-comment CODE line per copy (guard-1099).
        assert re.search(r"_unflagged_floor\(limit\)", src), (
            f"{name} defines the reserved floor but never CALLS it — the copy "
            f"silently reverts to popping the oldest unflagged entry"
        )
        assert re.search(r">\s*limit\s*-\s*_?floor", src), (
            f"{name} has no floor comparison in its eviction loop — the floor is "
            f"computed and then ignored, which is the same regression"
        )
        assert "UNFLAGGED_FLOOR_RATIO = 0.2" in src, (
            f"{name} disagrees on the reserved share; the two copies must hold "
            f"the same constant or the live path silently uses another policy"
        )
        assert "def _is_flagged" in src, (
            f"{name} is missing the flag predicate the floor indexes with"
        )
