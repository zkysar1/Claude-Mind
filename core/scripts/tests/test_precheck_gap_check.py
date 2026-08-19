"""test_precheck_gap_check.py — Phase 0-1 (aspirations-precheck) gap detector.

Pins the measurement (meter start/log stamp vs iterations closed in the diary),
the fail-open contract (always exit 0, never a gate), the banner threshold, and
that both call sites still invoke it — a detector nobody calls is the g-306-227
shape. Origin: cc-04 reducer 2026-08-17, precheck dark for 4+ iterations after
autocompacts while every iteration looked healthy.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "core" / "scripts" / "precheck-gap-check.py"
WRAPPER = ROOT / "core" / "scripts" / "precheck-gap-check.sh"
CLOSE = ROOT / "core" / "scripts" / "iteration-close.sh"
RESTORE = ROOT / "core" / "scripts" / "compact-restore-slots.sh"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_helpers import BASH  # noqa: E402

NOW = datetime(2026, 8, 17, 1, 0, 0)


def _load():
    spec = importlib.util.spec_from_file_location("precheck_gap_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _diary(session: Path, closes):
    lines = []
    for ts in closes:
        lines.append(json.dumps({"entry_type": "phase_start", "phase": "phase-12-productivity",
                                 "timestamp": (ts - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%S")}))
        lines.append(json.dumps({"entry_type": "phase_end", "phase": "phase-12-productivity",
                                 "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S")}))
        # noise the counter must ignore: other phases, and a productivity phase_start
        lines.append(json.dumps({"entry_type": "phase_end", "phase": "phase-5-verify",
                                 "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S")}))
    (session / "execution-diary.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _meter_start(session: Path, ts: datetime):
    st = session / "precheck-budget-state.json"
    st.write_text('{"start_ms": 1}', encoding="utf-8")
    os.utime(st, (ts.timestamp(), ts.timestamp()))


def _ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def _meter_log(session: Path, stamps, sweeps_ran=2):
    """PRODUCTION SHAPE (guard-920): the meter writes `ts` as EPOCH MILLISECONDS
    (`cur_ms=$(now_ms)`), e.g. {"ts": 1786931051030, "event": "precheck-end",
    ...}. The first fixture here wrote ISO strings, so the ISO-only parser
    passed its own tests while reading NOTHING in production."""
    (session / "precheck-drops.jsonl").write_text(
        "".join(json.dumps({"ts": _ms(t), "event": "precheck-end", "sweeps_ran": sweeps_ran,
                            "sweeps_dropped": 0, "always_run_count": 0}) + "\n" for t in stamps),
        encoding="utf-8")


def test_gap_counts_only_closes_after_the_newest_precheck_stamp(tmp_path):
    m = _load()
    s = tmp_path / "session"; s.mkdir()
    closes = [NOW - timedelta(minutes=x) for x in (200, 150, 100, 60, 30, 5)]
    _diary(s, closes)
    _meter_start(s, NOW - timedelta(minutes=120))          # start stamp
    _meter_log(s, [NOW - timedelta(minutes=170)])          # older log entry must lose
    r = m.compute(s, now=NOW)
    assert r["last_precheck_source"] == "meter-start"
    assert r["iterations_closed_since"] == 4                # 100, 60, 30, 5
    # the LOG can also be the newest stamp
    _meter_log(s, [NOW - timedelta(minutes=170), NOW - timedelta(minutes=20)])
    r2 = m.compute(s, now=NOW)
    assert r2["last_precheck_source"] == "meter-log"
    assert r2["iterations_closed_since"] == 1               # only the 5-min close


def test_completed_precheck_leaves_only_the_ms_drop_log_and_must_not_read_as_never(tmp_path):
    """The cc-04 false alarm of 2026-08-17 01:57: `end` UNLINKS the state file, so
    after a COMPLETED precheck the drop log is the only stamp — and its `ts` is
    epoch ms. An ISO-only parser sees no stamp, prints NEVER, and counts every
    close in the diary (19 here vs a true gap of 1). Pin: no state file + a
    recent ms end event => gap counts only closes after that end."""
    m = _load()
    s = tmp_path / "session"; s.mkdir()
    closes = [NOW - timedelta(minutes=x) for x in (200, 150, 100, 60, 30, 5)]
    _diary(s, closes)
    # NO precheck-budget-state.json (unlinked at `end`) — only the drop log
    _meter_log(s, [NOW - timedelta(minutes=170), NOW - timedelta(minutes=13)], sweeps_ran=2)
    r = m.compute(s, now=NOW)
    assert r["last_precheck"] is not None, "ms drop-log ts must parse — NEVER here is the false alarm"
    assert r["last_precheck_source"] == "meter-log"
    assert r["iterations_closed_since"] == 1              # only the 5-min close
    assert r["last_precheck_end"] == (NOW - timedelta(minutes=13)).strftime("%Y-%m-%dT%H:%M:%S")
    assert r["last_precheck_end_sweeps_ran"] == 2
    text = m.render(r, 1)
    assert "NEVER" not in text and "sweeps_ran=2" in text
    # and the parser accepts the literal production record shape verbatim
    assert m._parse_ts(1786931051030) == datetime.fromtimestamp(1786931051.030)
    assert m._parse_ts("1786931051030") == datetime.fromtimestamp(1786931051.030)
    assert m._parse_ts("2026-08-17T01:44:11") == datetime(2026, 8, 17, 1, 44, 11)
    assert m._parse_ts(None) is None and m._parse_ts("garbage") is None


def test_no_stamp_at_all_counts_every_close_and_says_never(tmp_path):
    m = _load()
    s = tmp_path / "session"; s.mkdir()
    _diary(s, [NOW - timedelta(minutes=10), NOW - timedelta(minutes=5)])
    r = m.compute(s, now=NOW)
    assert r["last_precheck"] is None and r["iterations_closed_since"] == 2
    text = m.render(r, 1)
    assert "NEVER" in text and "PRECHECK GAP" in text


def test_banner_only_at_threshold_and_wrapper_always_exits_zero(tmp_path):
    m = _load()
    s = tmp_path / "session"; s.mkdir()
    _diary(s, [NOW - timedelta(minutes=30)])
    _meter_start(s, NOW - timedelta(minutes=10))            # precheck AFTER the last close
    r = m.compute(s, now=NOW)
    assert r["iterations_closed_since"] == 0
    assert "PRECHECK GAP" not in m.render(r, 1)
    # wrapper: fail-open contract on a missing dir AND on a real dir
    for sd in (s, tmp_path / "does-not-exist"):
        p = subprocess.run([BASH, str(WRAPPER), "--session-dir", str(sd)],
                           capture_output=True, text=True, timeout=60, cwd=str(ROOT))
        assert p.returncode == 0, p.stderr[-500:]
        assert "[precheck-gap]" in p.stdout


def test_wired_into_iteration_close_and_compact_restore():
    close = CLOSE.read_text(encoding="utf-8")
    i_gap = close.find("precheck-gap-check.sh")
    i_imp = close.find("═══ ITERATION COMPLETE ═══")
    assert 0 < i_gap < i_imp, ("the gap check must print BEFORE the ITERATION COMPLETE imperative — "
                                "the imperative must stay the terminal line (return-protocol.md)")
    restore = RESTORE.read_text(encoding="utf-8")
    assert "precheck-gap-check.sh" in restore
    assert "exec python3" not in restore, "restore must not exec past the gap check"
    assert 'exit "$_rc"' in restore, "restore rc must be preserved"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
