"""test_owncloud_merge_lane_freeze_detector.py — class-(a) freeze detection ().

The own-cloud sweep has TWO freeze lanes and, before this, ONE detector between
them:

  class (b) FENCE-ONLY (`merge_handler_for` -> None) — a both-diverged store
    appends to `conflict_paths`, which becomes the streaks artifact that
    `mirror_health.classify` reads and agent-watchdog's MirrorWedgeProbe
    auto-files from. Detected, filed, visible.

  class (a) MERGE-PROTECTED (a handler EXISTS and DECLINES) — surfaced ONLY as
    an `error [union-merge-push]` line on the sweep's stderr. Never written to
    the streaks file, so mirror-health could not see it and nothing auto-filed
    it. A file stopped propagating to every other box INDEFINITELY while the
    health surface the fleet trusts reported `healthy`.

THE POINT OF THIS FILE IS THE POSITIVE CONTROL, which the goal names as an
outcome in its own right: "a detector never exercised is indistinguishable from
one that always returns clean." So these tests do not merely assert that the
recorder appends a string — they drive the WHOLE chain the fleet actually
depends on, decline -> conflict_paths -> _update_conflict_streaks -> streaks
artifact -> mirror_health.classify, and assert the verdict flips to `wedged`.

The negative controls are load-bearing and there are two, because a detector
that fires on everything is as useless as one that fires on nothing:
  - a TRANSPORT fault must NOT be recorded (it is not a divergence, and an
    outage touches every file at once)
  - a CONTENTION ConflictError seen on ONE sweep must stay sub-threshold
    `healthy` (the artifact sorts freeze from contention by PERSISTENCE)

Runnable two ways:
  py -3 core/scripts/tests/test_owncloud_merge_lane_freeze_detector.py
  py -3 -m pytest core/scripts/tests/test_owncloud_merge_lane_freeze_detector.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))
sys.path.insert(0, str(SCRIPT_DIR))

import mirror_health  # noqa: E402
import owncloud_sync as _mod  # noqa: E402
from owncloud_backend import ConflictError  # noqa: E402

FROZEN = "/w/world/knowledge/tree/system/frozen-node.md"


def _stats() -> dict:
    return {"errors": 0, "conflicts": 0}


def _decline() -> ConflictError:
    """The CURRENT class-(a) decline, verbatim from owncloud_backend (guard-4778).

    Quoted rather than paraphrased so a reworded raise site shows up here as a
    diff to read, not as a silent pass.
    """
    return ConflictError(
        f"coordination merge REFUSED for {FROZEN}: the store's merge handler "
        f"declined to reconcile diverged content (same-heading divergence or an "
        f"undecodable side). Frozen for reader reconciliation -- no write attempted.")


# ── the recorder ───────────────────────────────────────────────────────────

def test_a_merge_handler_decline_is_recorded_as_a_conflict_path():
    """The whole defect in one assertion: this used to append nothing."""
    st = _stats()
    _mod._record_merge_lane_freeze(st, FROZEN, _decline())
    assert st.get("conflict_paths") == [FROZEN]
    assert st.get("merge_lane_frozen") == 1


def test_a_transport_fault_is_NOT_recorded():
    """Negative control. A network error is not a divergence.

    It is also not FILE-scoped — an outage fails every object in the sweep — so
    recording it would flood the artifact precisely when it is least readable,
    under a verdict whose text says "both-diverged".
    """
    st = _stats()
    _mod._record_merge_lane_freeze(st, FROZEN, OSError("connection reset by peer"))
    assert "conflict_paths" not in st
    assert "merge_lane_frozen" not in st


def test_the_malformed_blob_channel_is_also_recorded():
    """Both class-(a) channels freeze, so both must be seen.

    owncloud_backend raises ConflictError from TWO places: the handler RETURNING
    None (the guard-4778 same-heading refusal) and the handler RAISING on a
    malformed blob. Discriminating on the type rather than the message is what
    makes the second one free.
    """
    st = _stats()
    _mod._record_merge_lane_freeze(
        st, FROZEN, ConflictError(f"coordination merge failed for {FROZEN}: bad yaml"))
    assert st.get("conflict_paths") == [FROZEN]


# ── the chain the fleet actually depends on ────────────────────────────────

def _sweep_streaks(tmp_path, monkeypatch, paths):
    """Run ONE sweep's streak bookkeeping with `paths` conflicting, return the artifact."""
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    st = _stats()
    if paths:
        st["conflict_paths"] = list(paths)
    _mod._update_conflict_streaks(st)
    p = tmp_path / "owncloud-conflict-streaks.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def test_positive_control_a_persistent_decline_drives_mirror_health_to_wedged(tmp_path, monkeypatch):
    """END-TO-END, and this is the test the goal actually asked for.

    Replays a class-(a) decline on consecutive sweeps and asserts the health
    surface the fleet reads flips to `wedged` — the verdict MirrorWedgeProbe
    auto-files an Investigate goal from. Before this change the same replay left
    the artifact empty and the verdict `healthy`, forever.
    """
    threshold = mirror_health.DEFAULT_THRESHOLD
    streaks = {}
    for sweep in range(1, threshold + 1):
        st = _stats()
        _mod._record_merge_lane_freeze(st, FROZEN, _decline())   # the sweep re-fails
        streaks = _sweep_streaks(tmp_path, monkeypatch, st["conflict_paths"])
        assert streaks.get(FROZEN) == sweep, f"streak should advance each sweep (sweep {sweep})"

    v = mirror_health.classify(streaks, age_min=0.0, threshold=threshold)
    assert v["verdict"] == "wedged", v
    assert FROZEN in json.dumps(v), "the wedged verdict must name the frozen file"


def test_negative_control_a_one_sweep_transient_stays_healthy(tmp_path, monkeypatch):
    """Contention must NOT read as a freeze, and the artifact sorts them by PERSISTENCE.

    A contention-class ConflictError (If-Match lost, merge-reconcile retries
    exhausted) is recorded on the sweep it happens, then clears. `classify`
    reports it as a sub-threshold transient under a `healthy` verdict. This is
    why the recorder does not try to sort freeze from contention at the raise
    site — the artifact already does it, by the one signal that distinguishes
    them.
    """
    st = _stats()
    _mod._record_merge_lane_freeze(
        st, FROZEN, ConflictError(f"If-Match failed for {FROZEN}: remote changed"))
    streaks = _sweep_streaks(tmp_path, monkeypatch, st["conflict_paths"])
    assert mirror_health.classify(streaks, age_min=0.0)["verdict"] == "healthy"

    # Next sweep is clean -> the map is REBUILT from that sweep's paths, so the
    # streak is forgotten rather than carried.
    streaks = _sweep_streaks(tmp_path, monkeypatch, [])
    assert FROZEN not in streaks
    assert mirror_health.classify(streaks, age_min=0.0)["verdict"] == "healthy"


# ── the wiring, which is the half that rots silently ───────────────────────

def test_the_union_merge_push_except_branch_actually_calls_the_recorder():
    """A recorder nothing calls is the defect this goal was filed about, one layer up.

    guard-1943: pinning the writer says nothing about the wiring. Asserted
    against the source because the branch needs a live backend to reach.
    """
    src = (SCRIPT_DIR.parent / "owncloud_sync.py").read_text(encoding="utf-8")
    i = src.index('_record_error(stats, full, e, phase="union-merge-push")')
    tail = src[i:i + 300]
    assert "_record_merge_lane_freeze(stats, full, e)" in tail, \
        "the union-merge-push failure branch must record a class-(a) freeze"


def test_the_counter_reaches_the_sweep_summary_line():
    """The stderr line was the ONLY surface; the summary line should name the lane too."""
    src = (SCRIPT_DIR.parent / "owncloud_sync.py").read_text(encoding="utf-8")
    assert "merge-lane-frozen {stats.get('merge_lane_frozen', 0)}" in src


def test_mirror_health_still_reads_the_artifact_this_writes():
    """Pins the SHARED path, so a rename on either side fails loudly here.

    The two modules resolve the artifact independently (mirror_health.streaks_path
    documents itself as mirroring owncloud_sync._conflict_streaks_path). Two
    independent resolvers of one filename is exactly where a silent divergence
    lives, and this whole goal exists because a producer and a consumer
    disagreed about a surface.
    """
    assert mirror_health.streaks_path().name == _mod._conflict_streaks_path().name


if __name__ == "__main__":
    import tempfile

    class _MP:
        def __init__(self): self._saved = {}
        def setenv(self, k, v):
            import os
            self._saved.setdefault(k, os.environ.get(k))
            os.environ[k] = v
        def undo(self):
            import os
            for k, v in self._saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        mp = _MP()
        try:
            n = fn.__code__.co_argcount
            with tempfile.TemporaryDirectory() as td:
                if n == 2:
                    fn(Path(td), mp)
                elif n == 1:
                    fn(Path(td))
                else:
                    fn()
            print(f"PASS {name}")
        except Exception as e:  # noqa: BLE001 — standalone runner reports, never raises
            failures += 1
            print(f"FAIL {name}: {e}")
        finally:
            mp.undo()
    print(f"{'FAIL' if failures else 'PASS'}: {failures} failure(s)")
    sys.exit(1 if failures else 0)
