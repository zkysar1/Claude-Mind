"""test_phase_wedge_check.py — unit + regression test for .

phase-wedge-check.py is recovery-gate Path D: the wedged-loop detector for the
2026-07-04 own-cloud fleet-wedge (g-328-19 failures #4/#5). A loop wedged behind
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
from datetime import datetime, timedelta, timezone
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
        _entry("phase_start", "phase-4-execute", 150, "g-115-22"),   # orphan, never closed
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
        _entry("phase_start", "phase-0-precheck", 90, "g-500-06"),   # wedged here
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


# ---  liveness veto: a churning diary is not a frozen one ----------
#
# Measured 2026-08-07 on zeta/cc-02: recovery-gate Path D flipped a HEALTHY loop
# to IDLE 70 minutes into a deep goal. The phase-open marker was real and 70m
# old, but the loop was writing ordinary progress entries the whole time --
# invisible here because _load_markers filters to phase_start/phase_end. The
# detector's own scope note says it targets the FROZEN-diary wedge; an ordinary
# write inside the window falsifies "frozen" directly.
#
# The pair below is the differential (guard-1268): identical marker, one with a
# later ordinary write and one without. If a future edit breaks the veto the
# first fails; if an edit over-broadens it into "never wedge", the second fails.
# Neither test alone can catch both directions.

def test_liveness_veto_recent_ordinary_write(tmp_path):
    """THE 2026-08-07 shape: 70m open marker + a 20m ordinary write -> NOT wedged."""
    diary = _write_diary(tmp_path / "live.jsonl", [
        _entry("phase_start", "phase-4-execute", 70, "g-115-5227"),
        _entry("finding", "", 20, "g-115-5227"),
    ])
    r = WEDGE.check_wedge(diary, BASE_NOW, 65.0)
    assert r["verdict"] == "clean", r
    assert r["liveness_veto"] == "recent_diary_write"
    assert r["minutes_since_last_write"] == pytest.approx(20.0, abs=0.1)
    # The age that WOULD have fired is still reported, so an operator reading the
    # verdict can see how close it came instead of a bare "clean".
    assert r["age_minutes"] == pytest.approx(70.0, abs=0.1)
    assert r["stuck_phase"] == "phase-4-execute"


def test_frozen_diary_still_wedged_without_veto(tmp_path):
    """Differential twin: same 70m marker, NO later write -> still wedged.

    This is the 2026-07-04 incident shape the detector exists for (writes blocked
    behind a wedged lock, so the phase_start IS the newest line). The veto must
    not reach it -- if this ever goes clean, the liveness fix has disabled the
    original detection rather than narrowed it.
    """
    diary = _write_diary(tmp_path / "frozen.jsonl", [
        _entry("phase_start", "phase-0-precheck", 70, "g-115-5227"),
    ])
    r = WEDGE.check_wedge(diary, BASE_NOW, 65.0)
    assert r["verdict"] == "wedged", r
    assert "liveness_veto" not in r


def test_liveness_veto_boundary_inclusive(tmp_path):
    """Write exactly AT the threshold vetoes; one minute past does not.

    Deliberately inclusive (<=) where the age check is strict (>). Both mean the
    same thing at the boundary -- exactly-at-threshold does not recover -- so the
    two comparisons stay consistent rather than opening a one-minute seam.
    """
    at = _write_diary(tmp_path / "at.jsonl", [
        _entry("phase_start", "phase-4-execute", 100, "g-1"),
        _entry("observation", "", 65, "g-1"),
    ])
    assert WEDGE.check_wedge(at, BASE_NOW, 65.0)["verdict"] == "clean"
    over = _write_diary(tmp_path / "over.jsonl", [
        _entry("phase_start", "phase-4-execute", 100, "g-1"),
        _entry("observation", "", 66, "g-1"),
    ])
    assert WEDGE.check_wedge(over, BASE_NOW, 65.0)["verdict"] == "wedged"


def test_liveness_veto_unreadable_suppresses(tmp_path, monkeypatch):
    """An activity read that raises suppresses recovery, never enables it.

    guard-487 (suppression inputs fail CLOSED) and this script's documented
    fail-open-to-no-recovery contract point the same way here, so an unreadable
    liveness signal must land on clean -- not on wedged-by-default.
    """
    diary = _write_diary(tmp_path / "boom.jsonl", [
        _entry("phase_start", "phase-4-execute", 70, "g-1"),
    ])

    def _boom(_path, _now):
        raise OSError("simulated unreadable diary")

    monkeypatch.setattr(WEDGE, "last_diary_activity", _boom)
    r = WEDGE.check_wedge(diary, BASE_NOW, 65.0)
    assert r["verdict"] == "clean", r
    assert r["liveness_veto"] == "unreadable"


def test_last_diary_activity_spans_all_entry_types(tmp_path):
    """The activity read must NOT reuse _load_markers' phase-only filter.

    That filter is the entire reason ordinary writes were invisible to this
    detector, so the newest ORDINARY entry has to win over an older phase marker.
    """
    diary = _write_diary(tmp_path / "mix.jsonl", [
        _entry("phase_start", "phase-4-execute", 70, "g-1"),
        _entry("decision", "", 30, "g-1"),
        _entry("finding", "", 12, "g-1"),
    ])
    newest = WEDGE.last_diary_activity(diary, BASE_NOW)
    assert newest == BASE_NOW - timedelta(minutes=12)


def test_last_diary_activity_none_when_absent_or_untimestamped(tmp_path):
    """Missing file -> None. Timestamp-less / malformed rows are skipped, not fatal."""
    assert WEDGE.last_diary_activity(tmp_path / "nope.jsonl", BASE_NOW) is None
    path = tmp_path / "junk.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"entry_type": "note", "content": "no timestamp"}\n')
        f.write("not even json\n")
    assert WEDGE.last_diary_activity(path, BASE_NOW) is None


def test_future_dated_row_does_not_veto(tmp_path):
    """A future-dated row is NOT liveness evidence and must not suppress the wedge.

    Found by fresh-eyes review of this same change. The veto originally tested
    only the upper bound, so a row dated a day ahead gave since_min = -1440,
    which trivially satisfies `<= threshold` -- one skewed row silently disabled
    Path D forever. The diary is sync_tier: continuity (cross-box), so a peer with
    a bad clock can write one into a diary whose clock it does not own.

    The control below is the load-bearing half: the same diary WITHOUT the stray
    row must still be wedged, or this test would pass on a detector that had
    simply stopped working.
    """
    rows = [_entry("phase_start", "phase-0-precheck", 200, "g-1")]
    control = _write_diary(tmp_path / "control.jsonl", rows)
    assert WEDGE.check_wedge(control, BASE_NOW, 65.0)["verdict"] == "wedged"

    skewed = _write_diary(tmp_path / "skewed.jsonl", [
        _entry("observation", "", -1440, "g-1"),   # 1 day in the FUTURE
        rows[0],
    ])
    r = WEDGE.check_wedge(skewed, BASE_NOW, 65.0)
    assert r["verdict"] == "wedged", r
    assert "liveness_veto" not in r
    # And the future row must not become the activity timestamp either.
    assert WEDGE.last_diary_activity(skewed, BASE_NOW) == BASE_NOW - timedelta(minutes=200)


def test_future_row_alongside_genuine_recent_write_still_vetoes(tmp_path):
    """The filter drops only the future row -- a real recent write still vetoes.

    Guards the over-correction: dropping future entries must not drop the
    credible ones next to them.
    """
    diary = _write_diary(tmp_path / "mixed.jsonl", [
        _entry("phase_start", "phase-4-execute", 200, "g-1"),
        _entry("observation", "", -600, "g-1"),   # future, ignored
        _entry("finding", "", 10, "g-1"),         # genuine, recent
    ])
    r = WEDGE.check_wedge(diary, BASE_NOW, 65.0)
    assert r["verdict"] == "clean", r
    assert r["liveness_veto"] == "recent_diary_write"
    assert r["minutes_since_last_write"] == pytest.approx(10.0, abs=0.1)


def test_tz_aware_row_does_not_suppress_the_wedge(tmp_path):
    """A tz-AWARE ordinary row must not raise, and must not veto a real wedge.

    Second-pass fresh-eyes finding on the same fix as the future-dated row: that
    one guarded the VALUE, this one the TYPE. `_pcr._parse_ts` returns whatever
    `fromisoformat` gives, so an offset-bearing stamp came back AWARE and
    `ts > now` raised TypeError -- which check_wedge catches as
    `liveness_veto: unreadable` -> clean. One such row therefore disabled Path D
    permanently. Before this veto existed an aware ordinary row was never read at
    all, so the veto INTRODUCED the regression. Fixed by parsing through
    `_dt.parse_naive_iso` (guard-1398 SSOT), which strips tzinfo and never raises.

    The control is load-bearing: without it this passes on a detector that has
    simply stopped vetoing anything.
    """
    old_marker = _entry("phase_start", "phase-0-precheck", 200, "g-1")
    control = _write_diary(tmp_path / "ctl.jsonl", [old_marker])
    assert WEDGE.check_wedge(control, BASE_NOW, 65.0)["verdict"] == "wedged"

    aware_old = {
        "entry_type": "finding",
        "timestamp": (BASE_NOW - timedelta(minutes=300)).replace(
            tzinfo=timezone.utc).isoformat(),
        "content": "aware, and older than the threshold",
    }
    diary = _write_diary(tmp_path / "aware.jsonl", [aware_old, old_marker])
    r = WEDGE.check_wedge(diary, BASE_NOW, 65.0)
    assert r["verdict"] == "wedged", r
    assert "liveness_veto" not in r
    # The aware row is READ (not dropped) -- it is simply too old to veto.
    assert WEDGE.last_diary_activity(diary, BASE_NOW) == BASE_NOW - timedelta(minutes=200)


def test_tz_aware_recent_row_vetoes_like_a_naive_one(tmp_path):
    """The tzinfo strip must PRESERVE the instant, not merely stop the raise.

    Guards the over-correction twin: dropping aware rows entirely would also
    'fix' the TypeError while silently discarding genuine liveness evidence.
    """
    diary = _write_diary(tmp_path / "aware_recent.jsonl", [
        _entry("phase_start", "phase-4-execute", 200, "g-1"),
        {
            "entry_type": "observation",
            "timestamp": (BASE_NOW - timedelta(minutes=15)).replace(
                tzinfo=timezone.utc).isoformat(),
            "content": "aware and recent",
        },
    ])
    r = WEDGE.check_wedge(diary, BASE_NOW, 65.0)
    assert r["verdict"] == "clean", r
    assert r["liveness_veto"] == "recent_diary_write"
    assert r["minutes_since_last_write"] == pytest.approx(15.0, abs=0.1)


def test_last_diary_activity_skips_non_dict_rows(tmp_path):
    """A bare JSON scalar/array row must be skipped, not raise AttributeError.

    Unit-scoped ON PURPOSE. check_wedge still raises on such a row via
    `_load_markers`' bare `e.get("entry_type")`, which runs BEFORE this function
    (guard-3001 -- a guard cannot protect what executes ahead of it). That defect
    is pre-existing and lives in a SHARED loader, so it is filed separately; this
    pins only that THIS function no longer contributes to it.
    """
    path = tmp_path / "nondict.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for bad in ("[1,2,3]", '"hello"', "null", "42"):
            f.write(bad + "\n")
        f.write(json.dumps(_entry("finding", "", 30, "g-1")) + "\n")
    assert WEDGE.last_diary_activity(path, BASE_NOW) == BASE_NOW - timedelta(minutes=30)


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
