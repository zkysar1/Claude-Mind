""" — a capture entry must never be the victim of its OWN append.

THE DEFECT, MEASURED RATHER THAN INFERRED. Both append paths enforced the cap as
`arr.sort(key=_eviction_sort_key); while len(arr) > limit: arr.pop(0)`, and that
key sorts UNFLAGGED entries first. So in a lane sitting AT cap whose entries are
100% `load_bearing`, a newly appended UNFLAGGED item sorts to index 0 and is
popped by the very call that added it.

Measured on the LIVE append path (alpha worker Body, hostname cc-07, uname -r
6.8.0-137-generic, own-cloud, 2026-08-17) against a real spark_capture holding
50/50 load_bearing at cap 50:

    unflagged append -> rc=0, entry ABSENT afterwards      (self-destructed)
    flagged   append -> rc=0, entry PRESENT afterwards     (positive control)

The flagged control is what makes the unflagged absence evidence rather than a
guess: it proves the write path was live and the lane was reachable, so "absent"
means destroyed and not "never arrived". Without it the run is equally consistent
with the probe never having landed at all -- which is exactly how the first
attempt at this measurement read, before the control was added.

WHY IT MATTERED MORE THAN ONE LOST ROW. The lane was selectively deaf to exactly
the entries an honest reporter marks routine, and the effect was self-reinforcing:
only flagged entries survived, so the flagged rate stayed pinned at 100%, so the
next unflagged entry died too. That is how `load_bearing` became a constant and
stopped carrying the triage signal it exists for -- measured at 50/50 on cc-07 and
independently at 69/69 on the cc-04 reducer, i.e. a property of the lane and not of
one box. The write also reports success (rc=0), so nothing anywhere observes the
loss; an append that is silently undone is not a cap, it is a write failure
wearing a cap's clothes.

WHAT THE FIX DOES NOT DO. It does not raise a cap, and it does not change the
priority order for anything except the newcomer: unflagged peers are still evicted
before flagged peers (test_priority_order_is_preserved_for_peers pins that). When
the lane is 100% flagged the key has no variance and cannot prioritise at all, so
falling through to the oldest peer is the only order left as well as the right one.

WHAT IT TURNED OUT NOT TO BE ENOUGH FOR (g-306-316). The guard above is scoped to
the CALL — `arr[_victim] is item` is an identity test against the item THIS
invocation holds. On the next append that entry is an ordinary unflagged peer and
is popped, so N consecutive unflagged appends into a saturated lane kept exactly
ONE, a 90% loss at N=10 that every test in this file passes straight through. The
completion is a PER-WINDOW reserved floor, pinned in
test_capture_eviction_unflagged_floor.py; both protections remain, scoped to
different units (guard-4236). Two fixtures in this file sit inside that floor and
are annotated where they appear.

TWIN-COPY TRAP. The runtime path is the DAEMON copy in
mind_api/src/endpoints/wm_write.py::append_slot; wrappers are daemon-only, so a
wm.py-only edit is inert at runtime (guard-742/547, the g-115-1992 bug class).
test_both_copies_carry_the_newcomer_guard is a source-level pin that fails loudly
if a future edit moves one half and not the other.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

import wm  # noqa: E402

SLOT = "spark_capture"
CAP = 50  # memory-pipeline.yaml working_memory_pruning.array_limits.spark_capture


def _entry(n: int, load_bearing: bool, ts: str | None = None) -> dict:
    """One capture item in the shape the production append path produces.

    `_item_ts` is present because the append path stamps every dict item; a
    fixture without it would exercise a shape production never emits (guard-920).
    """
    return {
        "goal_id": f"g-306-308-{n:03d}",
        "category": "framework-architecture",
        "observation": f"entry {n}",
        "sq_trigger": None,
        "load_bearing": load_bearing,
        "_item_ts": ts or f"2026-08-14T00:{n // 60:02d}:{n % 60:02d}",
    }


def _append(item: dict, slot: str = SLOT) -> None:
    """Invoke the real cmd_append against the redirected WM.

    The item arrives on STDIN in production, so the fixture feeds stdin rather
    than passing it as an attribute -- a different call shape would exercise a
    branch production never takes (guard-920).
    """
    saved = sys.stdin
    sys.stdin = io.StringIO(json.dumps(item))
    try:
        wm.cmd_append(SimpleNamespace(slot=slot))
    finally:
        sys.stdin = saved


class _TempWM:
    """BODY_WM_PATH is the ONLY correct redirect -- patching wm.WM_PATH is a no-op
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


def _lane(slot: str = SLOT) -> list:
    import yaml
    data = yaml.safe_load(Path(os.environ["BODY_WM_PATH"]).read_text(encoding="utf-8"))
    return (data.get("slots") or {}).get(slot) or []


def _fill(n: int, load_bearing: bool) -> None:
    for i in range(n):
        _append(_entry(i, load_bearing))


def _ids(slot: str = SLOT) -> list:
    return [e.get("goal_id") for e in _lane(slot) if isinstance(e, dict)]


# --- the core defect ---------------------------------------------------------

def test_unflagged_newcomer_survives_a_saturated_all_flagged_lane():
    """THE regression. Reproduces the cc-07 measurement in-process: lane at cap,
    100% load_bearing, append one UNFLAGGED entry. Pre-fix this entry was popped
    by its own append and the call still returned success."""
    with _TempWM():
        _fill(CAP, load_bearing=True)
        assert len(_lane()) == CAP
        newcomer = _entry(999, load_bearing=False, ts="2026-08-17T18:00:00")
        _append(newcomer)
        assert "g-306-308-999" in _ids(), (
            "the entry this append added was destroyed by that same append -- "
            "a write that reports success and then undoes itself"
        )
        assert len(_lane()) == CAP, "the cap must still hold"


def test_the_evicted_entry_is_the_oldest_peer_not_the_newcomer():
    """The newcomer displaces the OLDEST flagged peer. When every entry is
    flagged the key has zero variance, so oldest-first is the only order left."""
    with _TempWM():
        _fill(CAP, load_bearing=True)
        _append(_entry(999, load_bearing=False, ts="2026-08-17T18:00:00"))
        ids = _ids()
        assert "g-306-308-000" not in ids, "the oldest peer should have been evicted"
        assert "g-306-308-999" in ids


def test_flagged_newcomer_also_survives_positive_control():
    """The control from the live probe. If this ever fails the harness itself is
    broken, and the test above proves nothing rather than proving a fix."""
    with _TempWM():
        _fill(CAP, load_bearing=True)
        _append(_entry(999, load_bearing=True, ts="2026-08-17T18:00:00"))
        assert "g-306-308-999" in _ids()


def test_priority_order_is_preserved_for_peers():
    """The fix must NOT flatten load_bearing into a no-op. With unflagged peers
    present ABOVE the reserved floor, an unflagged peer is still evicted before
    any flagged one.

    FIXTURE AMENDED by g-306-316, intent unchanged. This test used to seed ONE
    unflagged entry against 49 flagged. That state now sits INSIDE the unflagged
    floor (_unflagged_floor(50) == 10), which reserves those slots precisely so a
    saturated lane cannot go deaf to unflagged content — so the single unflagged
    entry is protected there and a flagged one yields instead. That inversion is
    the floor working, not a regression, and it is pinned deliberately by
    test_below_the_floor_a_flagged_peer_yields_instead below. Seeding 11
    unflagged puts the fixture back in the region this test is actually about.
    """
    with _TempWM():
        for i in range(11):  # one above the floor, so priority still governs
            _append(_entry(i, load_bearing=False))
        for i in range(39):
            _append(_entry(100 + i, load_bearing=True))
        assert len(_lane()) == CAP
        _append(_entry(999, load_bearing=True, ts="2026-08-17T18:00:00"))
        ids = _ids()
        assert "g-306-308-000" not in ids, (
            "the oldest unflagged peer must be evicted before any flagged entry "
            "-- neither the newcomer guard nor the floor may disable the "
            "priority key where the floor does not apply"
        )
        assert "g-306-308-100" in ids, "no flagged peer should have been touched"
        assert "g-306-308-999" in ids


def test_below_the_floor_a_flagged_peer_yields_instead():
    """The other side of the amendment above, pinned rather than left implicit.

    This is the EXACT fixture this file used to assert the opposite on (1
    unflagged, 49 flagged), kept so the behaviour change is visible in the diff
    instead of vanishing with the old assertion. Below the reserved floor the
    lane protects its scarce unflagged content and the oldest FLAGGED entry is
    the victim — the stated cost of g-306-316, not an accident.
    """
    with _TempWM():
        _fill(CAP - 1, load_bearing=True)
        _append(_entry(500, load_bearing=False, ts="2026-08-10T00:00:00"))
        assert len(_lane()) == CAP
        _append(_entry(999, load_bearing=True, ts="2026-08-17T18:00:00"))
        ids = _ids()
        assert "g-306-308-500" in ids, (
            "with only 1 unflagged entry against a floor of 10, the lane must "
            "keep it -- otherwise N consecutive unflagged writes still collapse "
            "to one and g-306-316 bought nothing"
        )
        assert "g-306-308-000" not in ids, "the oldest FLAGGED entry yields"
        assert "g-306-308-999" in ids


def test_under_cap_appends_evict_nothing():
    """Negative control: the guard must not cause spurious eviction below cap."""
    with _TempWM():
        _fill(3, load_bearing=True)
        _append(_entry(999, load_bearing=False))
        assert len(_lane()) == 4
        assert "g-306-308-999" in _ids()


def test_eviction_is_still_counted_no_regression_on_g_306_289():
    """The newcomer guard must not silence the counter  added -- the
    entry that IS evicted still has to be visible."""
    import yaml
    with _TempWM():
        _fill(CAP, load_bearing=True)
        _append(_entry(999, load_bearing=False, ts="2026-08-17T18:00:00"))
        data = yaml.safe_load(
            Path(os.environ["BODY_WM_PATH"]).read_text(encoding="utf-8"))
        ev = data.get("capture_evictions") or {}
        assert ev.get(SLOT, 0) >= 1, (
            f"eviction must stay counted, got {ev!r} -- a silent drop is the "
            f"defect g-306-289 fixed"
        )


# --- the twin-copy pin -------------------------------------------------------

def test_both_copies_carry_the_newcomer_guard():
    """guard-742/547: the DAEMON copy is the live path, so a wm.py-only fix is
    inert at runtime. Source-level pin -- it fails when the halves diverge, which
    is the failure mode a behavioural test on wm.py alone cannot see."""
    cli = (CORE_SCRIPTS / "wm.py").read_text(encoding="utf-8")
    daemon = (CORE_SCRIPTS.parent.parent / "mind_api" / "src" / "endpoints"
              / "wm_write.py").read_text(encoding="utf-8")
    for name, src in (("wm.py", cli), ("wm_write.py", daemon)):
        assert "is item and len(arr) > 1" in src, (
            f"{name} is missing the newcomer guard -- if only one copy has it, "
            f"the daemon (live) and CLI paths disagree and the runtime half may "
            f"be the one without it"
        )
        # The guard's CONDITION is not the guard. Neutering its BODY to `pass`
        # leaves both assertions above intact while restoring the 
        # self-destruct in the LIVE path -- measured, not hypothesised. So pin the
        # ADJUSTMENT too. One non-comment CODE line in each copy; verified to fire
        # against that sabotage before being written (guard-385).
        assert "_victim + 1 if _victim + 1 < len(arr)" in src, (
            f"{name} has the newcomer condition but no adjustment -- the guard "
            f"matches and then evicts the newcomer anyway"
        )
        assert "arr.pop(_victim)" in src, f"{name} still pops index 0 unconditionally"
