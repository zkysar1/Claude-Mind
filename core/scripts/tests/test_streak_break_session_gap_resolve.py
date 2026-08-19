"""test_streak_break_session_gap_resolve.py - session-gap escape hatch for
the auto-resolve sweep in streak-break-reflector.py.

The reflector's `_auto_resolve_recovered_canaries()` originally only resolved
canaries whose source recurring goal recovered within streak_mult * interval_h
of the canary's filing time. A 30h session gap on a 2.67h-interval goal
falls outside that window even though the cadence-miss was purely structural
(the agent wasn't running). The session-gap escape hatch, added 2026-05-20,
reclassifies these as session-inactivity and auto-resolves them.

This file unit-tests the two pure helpers (`_load_session_gap_threshold` and
`_has_session_gap`) that drive the new path. Integration with the resolve
loop is covered indirectly: `test_streak_break_reflector.py` already pins
the in-window branch; the helpers tested here are the only inputs the
out-of-window branch reads, so a green test here plus the existing green
integration test brackets the new path.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
REFLECTOR_PATH = CORE_SCRIPTS / "streak-break-reflector.py"


def _load_reflector_module():
    """Load streak-break-reflector.py as a module (hyphen in filename)."""
    if str(CORE_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(CORE_SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "streak_break_reflector_under_test", REFLECTOR_PATH)
    assert spec and spec.loader, "spec_from_file_location returned None"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_diary(agent_dir: Path, timestamps: list[datetime]) -> Path:
    """Write a minimal execution-diary.jsonl with given timestamps."""
    sess = agent_dir / "session"
    sess.mkdir(parents=True, exist_ok=True)
    diary = sess / "execution-diary.jsonl"
    lines = []
    for ts in timestamps:
        lines.append(
            '{"entry_type": "phase_end", "phase": "phase-12-productivity", '
            f'"timestamp": "{ts.isoformat(timespec="seconds")}", '
            '"content": "phase_end phase-12-productivity"}'
        )
    diary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return diary


def test_load_session_gap_threshold_default():
    """Knob reads from aspirations.yaml; default is 2.0 hours."""
    mod = _load_reflector_module()
    threshold = mod._load_session_gap_threshold()
    # Default in core/config/aspirations.yaml is 2.0
    assert threshold == 2.0, f"expected 2.0, got {threshold}"


def test_has_session_gap_continuous_activity():
    """Continuous diary entries inside the window -> NO gap detected."""
    mod = _load_reflector_module()
    with tempfile.TemporaryDirectory() as tmp:
        agent_dir = Path(tmp) / "agent"
        agent_dir.mkdir()
        start = datetime(2026, 5, 20, 10, 0, 0)
        end = datetime(2026, 5, 20, 14, 0, 0)
        # Entries every 15 minutes - well under 2h threshold
        ts_list = [start + timedelta(minutes=15 * i) for i in range(17)]
        _write_diary(agent_dir, ts_list)
        assert mod._has_session_gap(start, end, agent_dir, 2.0) is False


def test_has_session_gap_interior_gap():
    """An interior gap >= threshold -> True."""
    mod = _load_reflector_module()
    with tempfile.TemporaryDirectory() as tmp:
        agent_dir = Path(tmp) / "agent"
        agent_dir.mkdir()
        start = datetime(2026, 5, 19, 8, 0, 0)
        end = datetime(2026, 5, 20, 16, 0, 0)
        # Two clusters separated by a ~30h gap (mirrors bravo incident)
        cluster_a = [start + timedelta(minutes=5 * i) for i in range(6)]
        cluster_b = [end - timedelta(minutes=5 * i) for i in range(6)]
        _write_diary(agent_dir, cluster_a + cluster_b)
        assert mod._has_session_gap(start, end, agent_dir, 2.0) is True


def test_has_session_gap_leading_gap():
    """Gap from start_dt to first entry >= threshold -> True."""
    mod = _load_reflector_module()
    with tempfile.TemporaryDirectory() as tmp:
        agent_dir = Path(tmp) / "agent"
        agent_dir.mkdir()
        start = datetime(2026, 5, 20, 0, 0, 0)
        end = datetime(2026, 5, 20, 20, 0, 0)
        # First entry doesn't appear until 5h after start
        ts_list = [start + timedelta(hours=5) + timedelta(minutes=10 * i)
                   for i in range(10)]
        _write_diary(agent_dir, ts_list)
        assert mod._has_session_gap(start, end, agent_dir, 2.0) is True


def test_has_session_gap_trailing_gap():
    """Gap from last entry to end_dt >= threshold -> True."""
    mod = _load_reflector_module()
    with tempfile.TemporaryDirectory() as tmp:
        agent_dir = Path(tmp) / "agent"
        agent_dir.mkdir()
        start = datetime(2026, 5, 20, 0, 0, 0)
        end = datetime(2026, 5, 20, 20, 0, 0)
        # Activity stops 6h before end_dt
        ts_list = [start + timedelta(minutes=10 * i) for i in range(10)]
        _write_diary(agent_dir, ts_list)
        assert mod._has_session_gap(start, end, agent_dir, 2.0) is True


def test_has_session_gap_no_diary():
    """Missing diary -> False (fail-open; caller keeps 'leave open')."""
    mod = _load_reflector_module()
    with tempfile.TemporaryDirectory() as tmp:
        agent_dir = Path(tmp) / "agent"
        agent_dir.mkdir()
        start = datetime(2026, 5, 20, 10, 0, 0)
        end = datetime(2026, 5, 20, 12, 0, 0)
        # No diary written at all
        assert mod._has_session_gap(start, end, agent_dir, 2.0) is False


def test_has_session_gap_empty_diary_short_window():
    """No entries in window AND window < threshold -> False (not a gap)."""
    mod = _load_reflector_module()
    with tempfile.TemporaryDirectory() as tmp:
        agent_dir = Path(tmp) / "agent"
        agent_dir.mkdir()
        sess = agent_dir / "session"
        sess.mkdir()
        (sess / "execution-diary.jsonl").write_text("", encoding="utf-8")
        start = datetime(2026, 5, 20, 10, 0, 0)
        end = datetime(2026, 5, 20, 11, 0, 0)  # 1h window, threshold 2h
        assert mod._has_session_gap(start, end, agent_dir, 2.0) is False


def test_has_session_gap_empty_diary_long_window():
    """No entries in window AND window >= threshold -> True (whole span idle)."""
    mod = _load_reflector_module()
    with tempfile.TemporaryDirectory() as tmp:
        agent_dir = Path(tmp) / "agent"
        agent_dir.mkdir()
        sess = agent_dir / "session"
        sess.mkdir()
        (sess / "execution-diary.jsonl").write_text("", encoding="utf-8")
        start = datetime(2026, 5, 20, 10, 0, 0)
        end = datetime(2026, 5, 20, 15, 0, 0)  # 5h window, threshold 2h
        assert mod._has_session_gap(start, end, agent_dir, 2.0) is True


def test_has_session_gap_threshold_disable():
    """A very large threshold (999h) disables the gap path even with real gaps."""
    mod = _load_reflector_module()
    with tempfile.TemporaryDirectory() as tmp:
        agent_dir = Path(tmp) / "agent"
        agent_dir.mkdir()
        start = datetime(2026, 5, 19, 8, 0, 0)
        end = datetime(2026, 5, 20, 16, 0, 0)
        cluster_a = [start + timedelta(minutes=5 * i) for i in range(6)]
        cluster_b = [end - timedelta(minutes=5 * i) for i in range(6)]
        _write_diary(agent_dir, cluster_a + cluster_b)
        # Threshold = 999h => no detectable gap fits
        assert mod._has_session_gap(start, end, agent_dir, 999.0) is False


def test_has_session_gap_malformed_lines_skipped():
    """Malformed JSON lines and missing-timestamp records are skipped, not raised."""
    mod = _load_reflector_module()
    with tempfile.TemporaryDirectory() as tmp:
        agent_dir = Path(tmp) / "agent"
        agent_dir.mkdir()
        sess = agent_dir / "session"
        sess.mkdir()
        start = datetime(2026, 5, 20, 10, 0, 0)
        end = datetime(2026, 5, 20, 14, 0, 0)
        lines = [
            "not-json-at-all",
            '{"entry_type": "phase_end"}',  # missing timestamp
            '{"timestamp": "not-an-iso-date"}',
            (
                '{"entry_type": "phase_end", "timestamp": "'
                + (start + timedelta(minutes=30)).isoformat(timespec="seconds")
                + '"}'
            ),
            (
                '{"entry_type": "phase_end", "timestamp": "'
                + (start + timedelta(hours=1)).isoformat(timespec="seconds")
                + '"}'
            ),
        ]
        (sess / "execution-diary.jsonl").write_text(
            "\n".join(lines) + "\n", encoding="utf-8")
        # Only the two valid entries count; they're 30min apart and bracketed
        # by a 3h trailing gap -> True.
        assert mod._has_session_gap(start, end, agent_dir, 2.0) is True


# ---------------------------------------------------------------------------
# Retention-horizon clamp ( x , 2026-08-17). iteration-close
# trims the diary to its last 8h on every close, so a diary that starts 8h ago
# says NOTHING about the 15h before it. Read naively, that void was "agent
# inactive >= 2h" and suppressed every canary whose window exceeded ~10h —
# measured on alpha: 49/49 session-gap suppressions since the trim landed had
# windows >= 10.8h (median 92h), the 23.2h  drain-lane stall among
# them, while the reducer had been closing goals continuously.
# ---------------------------------------------------------------------------

def _trimmed_active_diary(agent_dir: Path, now: datetime, hours: float = 8.0,
                          every_min: int = 15) -> None:
    """A diary the way iteration-close leaves it on a BUSY agent: continuous
    activity, but only the last `hours` survive the trim."""
    first = now - timedelta(hours=hours)
    n = int(hours * 60 / every_min) + 1
    _write_diary(agent_dir, [first + timedelta(minutes=every_min * i)
                             for i in range(n)])


def test_trimmed_diary_continuous_activity_is_not_a_gap():
    """THE INCIDENT SHAPE. 23.2h window, diary trimmed to the last 8h with
    activity every 15 min inside it -> the pre-horizon 15h is undecidable, the
    retained 8h shows no gap -> False (canary FILES). Under the old code the
    15h leading void returned True and the canary was suppressed."""
    mod = _load_reflector_module()
    with tempfile.TemporaryDirectory() as tmp:
        agent_dir = Path(tmp) / "agent"
        agent_dir.mkdir()
        now = datetime(2026, 8, 17, 19, 52, 8)
        _trimmed_active_diary(agent_dir, now)
        start = now - timedelta(hours=23.22)
        assert mod._has_session_gap(start, now, agent_dir, 2.0) is False


def test_trimmed_diary_real_gap_inside_horizon_still_detected():
    """The clamp must not blind the detector to a REAL gap that the retained
    horizon does show: activity for 3h, silence for 4h, activity for 1h -> True."""
    mod = _load_reflector_module()
    with tempfile.TemporaryDirectory() as tmp:
        agent_dir = Path(tmp) / "agent"
        agent_dir.mkdir()
        now = datetime(2026, 8, 17, 19, 52, 8)
        first = now - timedelta(hours=8)
        cluster_a = [first + timedelta(minutes=15 * i) for i in range(13)]  # 3h
        cluster_b = [now - timedelta(minutes=15 * i) for i in range(5)]     # last 1h
        _write_diary(agent_dir, cluster_a + cluster_b)
        start = now - timedelta(hours=23.22)
        assert mod._has_session_gap(start, now, agent_dir, 2.0) is True


def test_window_entirely_before_horizon_is_undecidable():
    """A Pass-2 canary whose [filed, recovered] window closed BEFORE the oldest
    surviving entry: nothing retained can speak to it -> False (leave open),
    not the old 'no entries in window -> whole span idle -> True'."""
    mod = _load_reflector_module()
    with tempfile.TemporaryDirectory() as tmp:
        agent_dir = Path(tmp) / "agent"
        agent_dir.mkdir()
        now = datetime(2026, 8, 17, 19, 52, 8)
        _trimmed_active_diary(agent_dir, now)
        start = now - timedelta(hours=40)
        end = now - timedelta(hours=20)
        assert mod._has_session_gap(start, end, agent_dir, 2.0) is False


def test_dormant_agent_diary_is_not_clamped_away():
    """The trim only runs when the loop closes, so a DORMANT agent keeps its
    last entries: window starts inside the retained span, activity stops, then
    30h of nothing -> the trailing gap is real evidence -> True. The clamp
    must not erase the case g-115-1319 was built for."""
    mod = _load_reflector_module()
    with tempfile.TemporaryDirectory() as tmp:
        agent_dir = Path(tmp) / "agent"
        agent_dir.mkdir()
        now = datetime(2026, 8, 17, 19, 52, 8)
        last_active = now - timedelta(hours=30)
        _write_diary(agent_dir, [last_active - timedelta(minutes=15 * i)
                                 for i in range(20)])  # ~5h of activity, then silence
        start = last_active - timedelta(hours=2)
        assert mod._has_session_gap(start, now, agent_dir, 2.0) is True


def test_horizon_clamp_source_pin():
    """Source pin: the clamp keys on the oldest entry in the WHOLE diary and
    treats a fully-pre-horizon window as undecidable (False)."""
    src = REFLECTOR_PATH.read_text(encoding="utf-8")
    assert "if oldest > end_dt:" in src
    assert "if oldest > start_dt:" in src
    assert "start_dt = oldest" in src
