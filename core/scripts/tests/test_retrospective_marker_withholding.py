""": the marker may only be stamped when nothing was lost or unseen.

`retrospect` stamps `retrospective_encoded` when `wrote > 0`, and a marked goal
is NEVER retrospected again. That made two silent permanent-loss paths, both
measured before this file existed:

  1. BLIND — `_load_capture_slot` returned {} on a FAILED READ, byte-identical to
     a genuinely empty slot, so the capture lane reported SKIP. A skip does not
     withhold the marker, and the other lanes succeeding was enough to stamp it.
     A transient store fault therefore orphaned that goal's captures forever.
  2. LOSSY — `_lane_encoding` appends one entry at a time and returns on the
     first failure, so a 5-entry batch failing at 3 left 1-2 queued, 3-5 nowhere,
     and the marker fired anyway.

The tests below pin BOTH BUCKETS of each decision, which is the point
(guard-4374): it is trivial to withhold the marker correctly by never stamping
it at all, and that would silently disable the whole retrospective cadence. So
every withhold test has a twin asserting the marker STILL FIRES on the healthy
shape it must not disturb.

Daemon-safe: every lane and `_run` is stubbed, so no wrapper executes and no live
store is read or written. No `daemon_integration` marker needed.

Run:
  STORAGE_BACKEND=local python -m pytest \
    core/scripts/tests/test_retrospective_marker_withholding.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parent.parent      # core/scripts/
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

import worker_retrospective as wr  # noqa: E402

ROOT = Path("/nonexistent")
ITEM = {"goal_id": "g-306-1", "source": "world", "title": "t",
        "aspiration_id": "asp-306"}
NOW = "2026-08-22T03:00:00"


def _stub(monkeypatch, *, marker_calls: list, encoding=None, team_state=None):
    """Stub every lane green unless overridden; record marker writes."""
    def _ok(*a, **k):
        return 0, "", ""

    for name in ("_lane_team_state", "_lane_journal", "_lane_findings",
                 "_lane_experience", "_lane_impk"):
        monkeypatch.setattr(wr, name, _ok)
    if team_state is not None:
        monkeypatch.setattr(wr, "_lane_team_state", team_state)
    monkeypatch.setattr(wr, "_lane_encoding", encoding or _ok)

    def _marker(item, agent, now_iso, root):
        marker_calls.append(item["goal_id"])
        return 0, "", ""

    monkeypatch.setattr(wr, "_write_marker", _marker)


# ───────────── bucket A: the slot READ failed (blind) vs slot EMPTY ──────────

def test_unreadable_slot_withholds_the_marker(monkeypatch):
    """The titled defect: a failed READ must not be recorded as 'nothing to do'."""
    marker_calls: list = []
    _stub(monkeypatch, marker_calls=marker_calls)

    out = wr.retrospect(ITEM, "alpha", NOW, ROOT,
                        captures=wr.UNREADABLE, enc_captures={})

    assert marker_calls == [], "a blind capture lane must withhold the marker"
    assert out["marked"] is False
    assert out["marker_withheld_for"] == ["experience"]
    lane = out["lanes"]["experience"]
    assert lane["unreadable"] is True
    assert lane["ok"] is False
    assert lane["err"] == wr.SLOT_UNREADABLE
    # The other lanes still RAN — withholding the marker must not disable work.
    assert out["lanes_written"] > 0


def test_unreadable_encoding_slot_withholds_the_marker(monkeypatch):
    """Same rule on the sibling lane — both capture slots, not just one."""
    marker_calls: list = []
    _stub(monkeypatch, marker_calls=marker_calls)

    out = wr.retrospect(ITEM, "alpha", NOW, ROOT,
                        captures={}, enc_captures=wr.UNREADABLE)

    assert marker_calls == []
    assert out["marker_withheld_for"] == ["encoding"]
    assert out["lanes"]["encoding"]["unreadable"] is True


def test_genuinely_empty_slot_still_stamps_the_marker(monkeypatch):
    """THE TWIN. Withholding on 'empty' would stall the cadence permanently.

    This is the bucket a naive fix breaks: an empty slot is the COMMON case (most
    goals capture nothing), so treating it like a blind one would withhold the
    marker on nearly every goal and re-retrospect the whole queue forever.
    """
    marker_calls: list = []
    _stub(monkeypatch, marker_calls=marker_calls)

    out = wr.retrospect(ITEM, "alpha", NOW, ROOT, captures={}, enc_captures={})

    assert marker_calls == ["g-306-1"], "an empty slot is a SKIP and must mark"
    assert out["marked"] is True
    assert out["marker_withheld_for"] == []
    assert out["lanes"]["experience"]["skipped"] == wr.SKIP_NO_CAPTURE
    assert out["lanes"]["encoding"]["skipped"] == wr.SKIP_NO_ENCODING


def test_captures_none_is_not_unreadable(monkeypatch):
    """`None` means the caller supplied nothing — the pre-existing default."""
    marker_calls: list = []
    _stub(monkeypatch, marker_calls=marker_calls)

    out = wr.retrospect(ITEM, "alpha", NOW, ROOT)

    assert marker_calls == ["g-306-1"]
    assert out["marker_withheld_for"] == []


# ──────── bucket B: a capture lane FAILED with entries (lossy) vs clean ───────

def test_failed_capture_lane_with_entries_withholds_the_marker(monkeypatch):
    """The mid-batch path: entries existed, the lane failed, work was lost."""
    marker_calls: list = []

    def _boom(item, agent, now_iso, root, entries):
        return 1, "", "encoding_queue append failed — after 2 of 5 entries queued"

    _stub(monkeypatch, marker_calls=marker_calls, encoding=_boom)

    out = wr.retrospect(ITEM, "alpha", NOW, ROOT, captures={},
                        enc_captures={"g-306-1": [{"fact": f"f{i}"}
                                                  for i in range(5)]})

    assert marker_calls == [], "a lane that lost entries must withhold the marker"
    assert out["marker_withheld_for"] == ["encoding"]
    assert "after 2 of 5" in out["lanes"]["encoding"]["err"]


def test_failed_NON_capture_lane_still_stamps_the_marker(monkeypatch):
    """THE TWIN. The withholding is SCOPED, not blanket.

    team_state/journal/findings write from the GOAL RECORD, which is still on
    disk for any later run — a failure there loses nothing. Withholding on those
    too would let one persistently-failing mechanizable lane block marking
    forever, which is a different permanent stall.
    """
    marker_calls: list = []

    def _boom(item, agent, now_iso, root):
        return 1, "", "team-state write failed"

    _stub(monkeypatch, marker_calls=marker_calls, team_state=_boom)

    out = wr.retrospect(ITEM, "alpha", NOW, ROOT, captures={}, enc_captures={})

    assert marker_calls == ["g-306-1"]
    assert out["lanes"]["team_state"]["ok"] is False
    assert out["marker_withheld_for"] == []


def test_healthy_capture_lane_stamps_the_marker(monkeypatch):
    """THE TWIN for bucket B: entries present AND the lane succeeded."""
    marker_calls: list = []
    _stub(monkeypatch, marker_calls=marker_calls)

    out = wr.retrospect(ITEM, "alpha", NOW, ROOT, captures={},
                        enc_captures={"g-306-1": [{"fact": "X is Y"}]})

    assert marker_calls == ["g-306-1"]
    assert out["marked"] is True
    assert out["marker_withheld_for"] == []


# ───────────────────────── the loader and its decoder ────────────────────────

def test_load_capture_slot_returns_the_sentinel_on_a_failed_read(monkeypatch):
    monkeypatch.setattr(wr, "_run", lambda *a, **k: (1, "", "daemon unreachable"))
    assert wr._load_capture_slot(ROOT, "exp_capture") is wr.UNREADABLE


def test_load_capture_slot_returns_a_mapping_on_a_successful_read(monkeypatch):
    monkeypatch.setattr(wr, "_run", lambda *a, **k: (0, "[]", ""))
    got = wr._load_capture_slot(ROOT, "exp_capture")
    assert got is not wr.UNREADABLE
    assert isinstance(got, dict)


def test_the_sentinel_cannot_be_mistaken_for_an_empty_mapping():
    """Truthy and not a mapping BY DESIGN.

    A falsy or dict-like sentinel would let `(captures or {}).get(...)` silently
    degrade it back to "empty" — the exact conflation this change removes — at
    any call site that forgets to decode it. Being neither makes such a site fail
    loudly instead. It found two real ones in `main()` when it was introduced.
    """
    assert bool(wr.UNREADABLE) is True
    assert not isinstance(wr.UNREADABLE, dict)
    assert not hasattr(wr.UNREADABLE, "get")


def test_lane_encoding_records_how_many_entries_landed(monkeypatch):
    """A partial failure must say what was queued — a retry re-queues exactly it."""
    calls = {"n": 0}

    def _run(cmd, timeout=90, stdin=None):
        calls["n"] += 1
        if calls["n"] == 3:
            return 1, "", "wm-append exploded"
        return 0, "", ""

    monkeypatch.setattr(wr, "_run", _run)
    entries = [{"fact": f"fact number {i}"} for i in range(5)]

    rc, _out, err = wr._lane_encoding(ITEM, "alpha", NOW, ROOT, entries)

    assert rc != 0
    assert "after 2 of 5 entries queued" in err
    assert "re-queues those 2" in err


def test_unreadable_slots_names_the_blind_slot():
    assert wr._unreadable_slots({}, {}) == []
    assert wr._unreadable_slots(wr.UNREADABLE, {}) == [wr.EXP_SLOT]
    assert wr._unreadable_slots({}, wr.UNREADABLE) == [wr.ENC_SLOT]
    assert wr._unreadable_slots(wr.UNREADABLE, wr.UNREADABLE) == [wr.EXP_SLOT,
                                                                 wr.ENC_SLOT]


def test_slot_goal_ids_does_not_crash_on_the_sentinel():
    """`main()` summarises the slot; the sentinel must not raise there."""
    assert wr._slot_goal_ids(wr.UNREADABLE) == []
    assert wr._slot_goal_ids({"g-1": [], "g-0": []}) == ["g-0", "g-1"]
    assert wr._slot_goal_ids(None) == []
