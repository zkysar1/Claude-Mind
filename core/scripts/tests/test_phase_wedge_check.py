"""test_phase_wedge_check.py — unit + regression test for .

phase-wedge-check.py is recovery-gate Path D: the wedged-loop detector for the
2026-07-04 own-cloud fleet-wedge ( failures #4/#5). A loop wedged behind
a ``_fileops.acquire_lock`` storage failure keeps its DDB heartbeat FRESH while
diary writes stall, freezing the execution-diary at an unclosed ``phase_start``.
Recovery-gate Paths A/C both require the heartbeat STALE, so a fresh heartbeat
masked the wedge — Path D keys on heartbeat==fresh + THIS detector's verdict.

Core criterion (g-328-23): a stale ``phase_start`` with no ``phase_end`` past the
wedge threshold is classified WEDGED (recovery-gate then recovers it). A recent
one, or a diary whose last marker is a ``phase_end``, is CLEAN.

Regression guard (the load-bearing case): the execution-diary accumulates
historically-unclosed ``phase_start`` records across autocompact / early-return
boundaries (20+ observed live during g-328-23 development). The detector MUST key
on the LAST marker, never the oldest unclosed one — else every call
false-positives on an ancient orphan (a live diary returned wedged on a 394-min
orphan of g-115-22 before the fix). ``test_clean_old_orphan_followed_by_activity``
is that guard.

Exit-code contract (recovery-gate.sh gates on these): 0=wedged, 1=clean, 2=error
(fail-OPEN — a recovery gate must never flip a healthy agent to IDLE on a bug).

Pattern: importlib + sys.path shape matching test_pending_questions_sweep.py —
phase-wedge-check.py has a hyphenated filename.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))


def _import_wedge():
    """Load phase-wedge-check.py via importlib (hyphenated filename)."""
    spec = importlib.util.spec_from_file_location(
        "phase_wedge_check_mod",
        CORE_SCRIPTS / "phase-wedge-check.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load spec for phase-wedge-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


WEDGE = _import_wedge()

# Fixed reference time so check_wedge tests are deterministic (check_wedge takes
# an explicit `now`, so we never depend on wall-clock for the pure-detector tests).
BASE_NOW = datetime(2026, 7, 4, 20, 0, 0)


def _entry(entry_type, phase, minutes_ago, goal_id=None, base=BASE_NOW):
    ts = (base - timedelta(minutes=minutes_ago)).isoformat()
    e = {
        "entry_type": entry_type,
        "phase": phase,
        "timestamp": ts,
        "content": "%s %s" % (entry_type, phase),
    }
    if goal_id:
        e["goal_id"] = goal_id
    return e


def _write_diary(path, entries):
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return path


# --- Core criterion: stale unclosed phase_start → wedged --------------------

def test_wedged_stale_phase_start(tmp_path):
    """ core: last marker is a 60m unclosed phase_start → wedged."""
    diary = _write_diary(tmp_path / "d.jsonl", [
        _entry("phase_start", "phase-0-precheck", 60, "g-500-01"),
    ])
    r = WEDGE.check_wedge(diary, BASE_NOW, 45.0)
    assert r["verdict"] == "wedged"
    assert r["stuck_phase"] == "phase-0-precheck"
    assert r["stuck_goal_id"] == "g-500-01"
    assert r["age_minutes"] == pytest.approx(60.0, abs=0.1)


def test_clean_recent_phase_start(tmp_path):
    """Last marker is a 5m unclosed phase_start → within threshold → clean."""
    diary = _write_diary(tmp_path / "d.jsonl", [
        _entry("phase_start", "phase-4-execute", 5, "g-500-02"),
    ])
    assert WEDGE.check_wedge(diary, BASE_NOW, 45.0)["verdict"] == "clean"


def test_clean_last_marker_is_phase_end(tmp_path):
    """Matched pair, both old — but last marker is a phase_end (loop progressed)."""
    diary = _write_diary(tmp_path / "d.jsonl", [
        _entry("phase_start", "phase-4-execute", 130, "g-500-03"),
        _entry("phase_end", "phase-4-execute", 120, "g-500-03"),
    ])
    r = WEDGE.check_wedge(diary, BASE_NOW, 45.0)
    assert r["verdict"] == "clean"
    assert "phase_end" in r["reason"]


# --- THE regression guard: old orphan must NOT false-positive ---------------

def test_clean_old_orphan_followed_by_activity(tmp_path):
    """Load-bearing regression guard for the  false-positive.

    An ancient unclosed phase_start (150m, never got a phase_end), then the loop
    MOVED ON: a later phase's start+end, recent. Last marker = recent phase_end →
    clean. The oldest-unclosed-start approach false-POSITIVES here (150m > 45m);
    the last-marker approach is clean. This is the live-diary 20-orphan
    (g-115-22, 394m) false-positive that this fix eliminated.
    """
    diary = _write_diary(tmp_path / "d.jsonl", [
        _entry("phase_start", "phase-4-execute", 150, ""),   # orphan, never closed
        _entry("phase_start", "phase-0-precheck", 10, "g-500-04"),
        _entry("phase_end", "phase-0-precheck", 8, "g-500-04"),
    ])
    r = WEDGE.check_wedge(diary, BASE_NOW, 45.0)
    assert r["verdict"] == "clean", r


def test_clean_recent_start_after_old_orphan(tmp_path):
    """Same 150m orphan, but the loop is now IN a fresh phase_start (3m). Last
    marker = recent phase_start within threshold → clean, keyed on the LAST
    start (phase-2-select), never the orphan."""
    diary = _write_diary(tmp_path / "d.jsonl", [
        _entry("phase_start", "phase-4-execute", 150, "g-115-22"),
        _entry("phase_start", "phase-2-select", 3, "g-500-05"),
    ])
    r = WEDGE.check_wedge(diary, BASE_NOW, 45.0)
    assert r["verdict"] == "clean"
    assert r["stuck_phase"] == "phase-2-select"


def test_wedged_when_last_start_is_old_despite_earlier_pairs(tmp_path):
    """Loop progressed through a closed phase, then wedged INSIDE the most recent
    phase. Earlier pair is closed; last marker is a 90m unclosed phase_start →
    wedged, stuck on that phase."""
    diary = _write_diary(tmp_path / "d.jsonl", [
        _entry("phase_start", "phase-2-select", 100, "g-500-06"),
        _entry("phase_end", "phase-2-select", 95, "g-500-06"),
        _entry("phase_start", "phase-0-precheck", 90, ""),   # wedged here
    ])
    r = WEDGE.check_wedge(diary, BASE_NOW, 45.0)
    assert r["verdict"] == "wedged"
    assert r["stuck_phase"] == "phase-0-precheck"


# --- Boundary ---------------------------------------------------------------

def test_threshold_boundary_strict_greater(tmp_path):
    """Exactly at threshold → clean (strict >). Just above → wedged."""
    at = _write_diary(tmp_path / "at.jsonl", [_entry("phase_start", "p", 45, "g-1")])
    assert WEDGE.check_wedge(at, BASE_NOW, 45.0)["verdict"] == "clean"
    over = _write_diary(tmp_path / "over.jsonl", [_entry("phase_start", "p", 46, "g-1")])
    assert WEDGE.check_wedge(over, BASE_NOW, 45.0)["verdict"] == "wedged"


# --- Empty / missing / malformed --------------------------------------------

def test_clean_empty_diary(tmp_path):
    diary = _write_diary(tmp_path / "empty.jsonl", [])
    r = WEDGE.check_wedge(diary, BASE_NOW, 45.0)
    assert r["verdict"] == "clean"
    assert "no phase markers" in r["reason"]


def test_clean_nonexistent_diary(tmp_path):
    r = WEDGE.check_wedge(tmp_path / "nope.jsonl", BASE_NOW, 45.0)
    assert r["verdict"] == "clean"


def test_non_phase_lines_ignored(tmp_path):
    """Trailing non-phase / malformed rows are skipped; the last PHASE marker
    governs the verdict (proving 'last marker' means last phase marker, not last
    line on disk)."""
    path = tmp_path / "mixed.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(_entry("phase_start", "phase-0-precheck", 60, "g-1")) + "\n")
        f.write('{"entry_type": "note", "content": "not a phase marker"}\n')
        f.write("not even json\n")
    r = WEDGE.check_wedge(path, BASE_NOW, 45.0)
    assert r["verdict"] == "wedged"
    assert r["stuck_phase"] == "phase-0-precheck"


# --- Threshold resolution ---------------------------------------------------

def test_threshold_env_override(monkeypatch):
    monkeypatch.setenv("WEDGE_STALE_MINUTES", "90")
    assert WEDGE.wedge_threshold_minutes() == 90.0


def test_threshold_malformed_env_ignored(monkeypatch):
    """Malformed env is ignored — falls through to config/default (same value as
    no-env). Asserted without hardcoding the number so a config change is safe."""
    monkeypatch.delenv("WEDGE_STALE_MINUTES", raising=False)
    baseline = WEDGE.wedge_threshold_minutes()
    monkeypatch.setenv("WEDGE_STALE_MINUTES", "not-a-number")
    assert WEDGE.wedge_threshold_minutes() == baseline
    assert baseline > 0


# ---  config invariant: wedge_stale MUST exceed heartbeat stale -------

def test_config_invariant_wedge_exceeds_heartbeat_stale():
    """: wedge_stale_minutes MUST be > runner_heartbeat.stale_minutes.

    The Path D false-positive (a long non-phase-4 phase false-recovered a HEALTHY
    agent, found by fresh-eyes-code self-review of g-328-23) existed ONLY because
    wedge_stale (45) < stale_minutes (60). A healthy phase's local runner-heartbeat
    ages WITH its phase_start (both stamped near the Phase -0.5 -> Phase 0 boundary,
    no mid-phase re-tick during active work), so wedge_stale > stale_minutes lets
    recovery-gate.sh's heartbeat-FRESH gate suppress a healthy long phase (its
    heartbeat is already stale by the time phase_start crosses the wedge threshold)
    while a genuine wedge -- heartbeat re-ticked fresh while the diary freezes --
    still fires. If a future edit lowers wedge_stale back below stale_minutes, the
    45<60 false-positive window reopens; this test fails loudly to prevent that.
    """
    import yaml
    cfg = CORE_SCRIPTS.parent / "config" / "aspirations.yaml"
    with open(cfg, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    rh = data.get("runner_heartbeat") or {}
    wedge = rh.get("wedge_stale_minutes")
    stale = rh.get("stale_minutes")
    assert wedge is not None, "aspirations.yaml runner_heartbeat.wedge_stale_minutes missing"
    assert stale is not None, "aspirations.yaml runner_heartbeat.stale_minutes missing"
    assert float(wedge) > float(stale), (
        "g-328-25 invariant VIOLATED: wedge_stale_minutes (%s) must be > "
        "stale_minutes (%s), else recovery-gate Path D false-recovers a HEALTHY "
        "agent in a long non-phase-4 phase (the 45<60 window)." % (wedge, stale)
    )


# --- Exit-code contract via main() (recovery-gate.sh gates on these) --------

def test_main_exit_wedged(tmp_path, monkeypatch, capsys):
    now = datetime.now()
    diary = _write_diary(tmp_path / "w.jsonl", [{
        "entry_type": "phase_start", "phase": "phase-0-precheck",
        "timestamp": (now - timedelta(minutes=60)).isoformat(), "goal_id": "g-1",
    }])
    monkeypatch.setenv("WEDGE_DIARY_PATH", str(diary))
    monkeypatch.setenv("WEDGE_STALE_MINUTES", "45")
    rc = WEDGE.main()
    assert rc == 0
    assert json.loads(capsys.readouterr().out.strip())["verdict"] == "wedged"


def test_main_exit_clean(tmp_path, monkeypatch, capsys):
    now = datetime.now()
    diary = _write_diary(tmp_path / "c.jsonl", [{
        "entry_type": "phase_start", "phase": "phase-4-execute",
        "timestamp": (now - timedelta(minutes=5)).isoformat(), "goal_id": "g-2",
    }])
    monkeypatch.setenv("WEDGE_DIARY_PATH", str(diary))
    monkeypatch.setenv("WEDGE_STALE_MINUTES", "45")
    rc = WEDGE.main()
    assert rc == 1
    assert json.loads(capsys.readouterr().out.strip())["verdict"] == "clean"


def test_main_exit_error_fails_open(tmp_path, monkeypatch, capsys):
    """If check_wedge raises, main() must fail OPEN (rc=2, verdict clean) — a
    recovery gate must never flip a healthy agent to IDLE on a check bug."""
    monkeypatch.setenv("WEDGE_DIARY_PATH", str(tmp_path / "any.jsonl"))

    def _boom(*a, **k):
        raise ValueError("simulated")

    monkeypatch.setattr(WEDGE, "check_wedge", _boom)
    rc = WEDGE.main()
    assert rc == 2
    assert json.loads(capsys.readouterr().out.strip())["verdict"] == "clean"


def test_main_no_agent_dir_bound(tmp_path, monkeypatch, capsys):
    """No diary override and no bound AGENT_DIR → clean, rc=1 (no recovery)."""
    monkeypatch.delenv("WEDGE_DIARY_PATH", raising=False)
    monkeypatch.setattr(WEDGE, "AGENT_DIR", None)
    rc = WEDGE.main()
    assert rc == 1
    assert "no AGENT_DIR bound" in json.loads(capsys.readouterr().out.strip())["reason"]
