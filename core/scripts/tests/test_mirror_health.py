#!/usr/bin/env python3
"""test_mirror_health.py —  mirror-wedge probe pins.

Covers the pure classifier (verdict boundaries), the file-reading probe
(RUNTIME_DIR override, backend gate), and the watchdog MirrorWedgeProbe
episode state machine (N-consecutive-ticks fire-once, cleared transition,
unknown-holds-state). Goal filing is stubbed — no store writes.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import mirror_health  # noqa: E402


# ── classify() ────────────────────────────────────────────────────────────

def test_classify_healthy_empty():
    v = mirror_health.classify({}, age_min=1.0)
    assert v["verdict"] == "healthy" and v["wedged_count"] == 0


def test_classify_subthreshold_transient_is_healthy():
    v = mirror_health.classify({"a": 1, "b": 2}, age_min=1.0, threshold=3)
    assert v["verdict"] == "healthy"
    assert "transient" in v["reason"]


def test_classify_wedged_at_threshold_boundary():
    v = mirror_health.classify({"a": 3, "b": 2}, age_min=1.0, threshold=3)
    assert v["verdict"] == "wedged"
    assert v["files"] == {"a": 3}          # b (sub-threshold) excluded
    assert v["wedged_count"] == 1


def test_classify_stale_file_is_unknown_not_healthy():
    """guard-980 class: absence of signal is not health."""
    v = mirror_health.classify({"a": 9}, age_min=120.0, max_age_min=30.0)
    assert v["verdict"] == "unknown"


def test_classify_absent_is_unknown():
    assert mirror_health.classify(None, None)["verdict"] == "unknown"


# ── probe() ───────────────────────────────────────────────────────────────

def test_probe_reads_runtime_dir_override(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    (tmp_path / "owncloud-conflict-streaks.json").write_text(
        json.dumps({"world/x.jsonl": 7}), encoding="utf-8")
    v = mirror_health.probe()
    assert v["verdict"] == "wedged" and v["files"] == {"world/x.jsonl": 7}


def test_probe_absent_file_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    assert mirror_health.probe()["verdict"] == "unknown"


def test_probe_non_owncloud_backend_is_na(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    v = mirror_health.probe()
    assert v["verdict"] == "unknown" and "n/a" in v["reason"]


def test_probe_stale_mtime_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    p = tmp_path / "owncloud-conflict-streaks.json"
    p.write_text(json.dumps({"a": 9}), encoding="utf-8")
    import os
    old = time.time() - 3600
    os.utime(p, (old, old))
    assert mirror_health.probe()["verdict"] == "unknown"


# ── watchdog MirrorWedgeProbe episode state machine ───────────────────────

def _load_watchdog():
    spec = importlib.util.spec_from_file_location(
        "agent_watchdog", SCRIPTS / "agent-watchdog.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def wd():
    return _load_watchdog()


def _mk_probe(wd, tmp_path, verdicts, filed_log):
    ctx = wd.WatchdogContext(agent_name="testagent", agent_dir=tmp_path,
                             project_root_path=tmp_path)
    probe = wd.MirrorWedgeProbe(ctx)
    seq = iter(verdicts)

    class FakeMH:
        @staticmethod
        def probe():
            return next(seq)
    sys.modules["mirror_health_real"] = sys.modules.get("mirror_health")
    probe._file_wedge_goal = lambda v: (filed_log.append(v) or
                                        {"filed": True, "goal_id": "g-test-1", "error": None})
    return probe, FakeMH


def _run_ticks(probe, fake_mh, n):
    real = sys.modules.get("mirror_health")
    sys.modules["mirror_health"] = fake_mh
    try:
        out = []
        for _ in range(n):
            out.append(probe.check())
        return out
    finally:
        if real is not None:
            sys.modules["mirror_health"] = real


W = {"verdict": "wedged", "wedged_count": 1, "files": {"world/x.jsonl": 5}}
H = {"verdict": "healthy", "wedged_count": 0, "files": {}}
U = {"verdict": "unknown", "wedged_count": 0, "files": {}}


def test_probe_fires_once_after_n_consecutive_wedged(wd, tmp_path):
    filed = []
    probe, fake = _mk_probe(wd, tmp_path, [W, W, W], filed)
    t1, t2, t3 = _run_ticks(probe, fake, 3)
    assert t1 == []                                   # tick 1: below N=2
    assert len(t2) == 1 and t2[0].event == "mirror_wedged"
    assert t3 == []                                   # episode-deduped
    assert len(filed) == 1                            # goal filed exactly once


def test_unknown_holds_state_between_wedged_ticks(wd, tmp_path):
    filed = []
    probe, fake = _mk_probe(wd, tmp_path, [W, U, W], filed)
    t1, t2, t3 = _run_ticks(probe, fake, 3)
    assert t1 == [] and t2 == []                      # unknown: no advance, no reset
    assert len(t3) == 1 and t3[0].event == "mirror_wedged"


def test_healthy_resets_and_emits_cleared_after_fire(wd, tmp_path):
    filed = []
    probe, fake = _mk_probe(wd, tmp_path, [W, W, H, W, W], filed)
    ticks = _run_ticks(probe, fake, 5)
    assert ticks[1][0].event == "mirror_wedged"
    assert ticks[2][0].event == "mirror_wedge_cleared"
    assert ticks[4][0].event == "mirror_wedged"       # new episode re-fires
    assert len(filed) == 2                            # one filing per episode


def test_state_roundtrip_tick_persistence(wd, tmp_path):
    filed = []
    probe, fake = _mk_probe(wd, tmp_path, [W], filed)
    _run_ticks(probe, fake, 1)
    state = probe.to_dict()
    probe2, fake2 = _mk_probe(wd, tmp_path, [W], filed)
    probe2.from_dict(state)
    assert probe2.consecutive_wedged == 1
    t = _run_ticks(probe2, fake2, 1)
    assert len(t[0]) == 1 and t[0][0].event == "mirror_wedged"  # resumes episode


def test_file_wedge_goal_passes_override_duplication(wd, tmp_path, monkeypatch):
    """: the wedge auto-file MUST pass --override-duplication.

    The probe owns exact box-scoped dedup via open_goal_exists(origin_signal)
    (checked before filing). The goal-dup-gate's fuzzy keyword check ALSO
    false-positives on the wedge goal's generic owncloud/mirror tokens — most
    during incident-heavy windows when overlapping owncloud goals sit in-queue,
    i.e. exactly when a wedge is most likely — silently defeating the auto-file
    (filed:false observed 2026-07-18 + 2026-07-20 on zeta's box). Bypassing only
    the redundant fuzzy layer (exact dedup remains) restores the mechanism.
    """
    import importlib
    ctx = wd.WatchdogContext(agent_name="testagent", agent_dir=tmp_path,
                             project_root_path=tmp_path)
    probe = wd.MirrorWedgeProbe(ctx)
    # Exact-dedup layer says "no open wedge goal" → filing proceeds.
    pf = importlib.import_module("pointer_freshness")
    monkeypatch.setattr(pf, "open_goal_exists", lambda *a, **k: False)

    captured = {}

    class _FakeProc:
        returncode = 0
        stdout = '{"id": "g-test-99"}'
        stderr = ""

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _FakeProc()

    monkeypatch.setattr(wd.subprocess, "run", _fake_run)

    result = probe._file_wedge_goal(
        {"verdict": "wedged", "wedged_count": 1, "files": {"world/x.jsonl": 5}})

    assert result["filed"] is True and result["goal_id"] == "g-test-99", result
    argv = captured["argv"]
    assert "--override-duplication" in argv, argv
    # a non-empty reason must immediately follow the flag
    idx = argv.index("--override-duplication")
    assert idx + 1 < len(argv) and argv[idx + 1].strip(), argv


def test_filing_failure_retries_next_tick(wd, tmp_path):
    """: a filing FAILURE (filed:False) must NOT mark the episode
    fired.

    Setting fired=True unconditionally (before the fix) meant one failed
    attempt permanently lost the wedge goal for the whole episode while the
    freeze persisted. Leaving fired=False on failure re-attempts on the next
    wedged tick until the goal lands.
    """
    ctx = wd.WatchdogContext(agent_name="testagent", agent_dir=tmp_path,
                             project_root_path=tmp_path)
    probe = wd.MirrorWedgeProbe(ctx)
    outcomes = iter([
        {"filed": False, "goal_id": None, "error": "goal_duplication_blocked"},
        {"filed": False, "goal_id": None, "error": "transient"},
        {"filed": True, "goal_id": "g-test-9", "error": None},
    ])
    attempts = []

    def _fake_file(v):
        r = next(outcomes)
        attempts.append(r)
        return r
    probe._file_wedge_goal = _fake_file

    class FakeMH:
        @staticmethod
        def probe():
            return W

    # Run ticks individually (NOT _run_ticks, which runs all N before returning
    # so probe.fired would read only the FINAL state) — the fired flag must be
    # checked BETWEEN ticks. 5 ticks of a persistent wedge: tick1 below N; ticks
    # 2-3 attempt filing and FAIL (fired stays False → retry); tick4 succeeds
    # (fired=True); tick5 deduped. Exactly 3 filing attempts.
    real = sys.modules.get("mirror_health")
    sys.modules["mirror_health"] = FakeMH
    try:
        assert probe.check() == []                       # tick1: consecutive=1 < N
        assert probe.fired is False
        e2 = probe.check()                               # tick2: attempt#1 fails
        assert len(e2) == 1 and e2[0].event == "mirror_wedged"
        assert probe.fired is False                      # NOT marked fired on failure
        e3 = probe.check()                               # tick3: attempt#2 fails
        assert len(e3) == 1 and probe.fired is False
        e4 = probe.check()                               # tick4: attempt#3 lands
        assert len(e4) == 1 and probe.fired is True      # marked fired on success
        assert probe.check() == []                       # tick5: deduped after success
    finally:
        if real is not None:
            sys.modules["mirror_health"] = real
    assert len(attempts) == 3
    assert probe.fired is True


def test_dedup_return_marks_fired_no_respam(wd, tmp_path):
    """ review: a dedup return (an open wedge goal ALREADY covers this
    box) must mark the episode fired — else a wedge that clears then reappears
    while the prior goal is still open would re-emit a critical event EVERY tick.
    Only a genuine no-goal failure keeps fired=False (see
    test_filing_failure_retries_next_tick).
    """
    ctx = wd.WatchdogContext(agent_name="testagent", agent_dir=tmp_path,
                             project_root_path=tmp_path)
    probe = wd.MirrorWedgeProbe(ctx)
    attempts = []

    def _fake_file(v):
        attempts.append(1)
        return {"filed": False, "dedup": True, "goal_id": None,
                "error": "open goal exists (dedup)"}
    probe._file_wedge_goal = _fake_file

    class FakeMH:
        @staticmethod
        def probe():
            return W

    real = sys.modules.get("mirror_health")
    sys.modules["mirror_health"] = FakeMH
    try:
        assert probe.check() == []                       # tick1: consecutive=1 < N
        e2 = probe.check()                               # tick2: attempt → dedup
        assert len(e2) == 1 and e2[0].event == "mirror_wedged"
        assert probe.fired is True                       # dedup counts as covered
        assert probe.check() == []                       # tick3: deduped, no re-emit
        assert probe.check() == []                       # tick4: still deduped
    finally:
        if real is not None:
            sys.modules["mirror_health"] = real
    assert len(attempts) == 1                            # filed once, no spam


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
