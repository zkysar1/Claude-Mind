"""test_phase_cost_report_malformed_rows.py — .

TWO defects in phase-cost-report.py, one shared loader, both latent-not-active.

  (1) ``_load_markers`` did a bare ``e.get("entry_type")`` on every parsed line.
      A line can be VALID JSON and still not be an object -- bare array, string,
      number, null -- and all four raise AttributeError. The exception escaped
      through ``check_wedge`` to ``main()``, which exits 2, and recovery-gate.sh
      reads any nonzero as no-recovery. So ONE malformed row disabled wedged-loop
      detection (recovery-gate Path D) until it aged out of the 8h trim window.

  (2) ``_parse_ts`` called ``datetime.fromisoformat`` directly, yielding an AWARE
      datetime for an offset-bearing stamp while every consumer does naive
      arithmetic -- ``TypeError: can't compare offset-naive and offset-aware``.
      guard-1398 designates ``_dt.parse_naive_iso`` as the SSOT for this parse.

WHY THE TESTS DRIVE ``check_wedge`` AND NOT ONLY ``_load_markers`` (guard-920):
the acceptance is "check_wedge returns a verdict rather than raising", and
``check_wedge`` is the production caller that turns the raise into exit 2. Testing
the loader alone would pin the fix in a shape recovery-gate never executes.

WHY THE `_still_detects_` CASES ARE NOT OPTIONAL (guard-1220): a guard that
skipped EVERY row would satisfy every "does not raise" case here. Those cases pin
that the malformed row is dropped and the real markers around it still produce
the correct verdict -- they are what makes the no-raise cases mean anything.
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


def _import(mod_name: str, filename: str):
    """Load a hyphenated script by path (pattern from test_phase_wedge_check.py)."""
    spec = importlib.util.spec_from_file_location(mod_name, CORE_SCRIPTS / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load spec for {filename}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PCR = _import("phase_cost_report_mod", "phase-cost-report.py")
WEDGE = _import("phase_wedge_check_mod", "phase-wedge-check.py")

BASE_NOW = datetime(2026, 8, 8, 20, 0, 0)

# The four shapes probed on cc-02 2026-08-08. Each parses as valid JSON and each
# made the pre-fix `.get` raise. `null` is the quiet one: it is the shape a
# half-written or truncated row is most likely to leave behind.
NON_DICT_ROWS = ['[]', '["a","b"]', '"a bare string"', '42', 'null']


def _marker(entry_type, phase, minutes_ago, goal_id=None, tz_suffix=""):
    ts = (BASE_NOW - timedelta(minutes=minutes_ago)).isoformat() + tz_suffix
    e = {"entry_type": entry_type, "phase": phase, "timestamp": ts,
         "content": f"{entry_type} {phase}"}
    if goal_id:
        e["goal_id"] = goal_id
    return json.dumps(e)


def _diary(tmp_path, raw_lines, name="d.jsonl"):
    p = tmp_path / name
    p.write_text("".join(l + "\n" for l in raw_lines), encoding="utf-8")
    return p


# --- (1) the acceptance criterion, in the production path ------------------

@pytest.mark.parametrize("row", NON_DICT_ROWS)
def test_check_wedge_returns_a_verdict_on_every_non_dict_shape(tmp_path, row):
    """ACCEPTANCE. Pre-fix each of these raised AttributeError out of
    _load_markers -> main() exit 2 -> recovery-gate reads nonzero as
    no-recovery, silently disabling Path D."""
    diary = _diary(tmp_path, [row])
    r = WEDGE.check_wedge(diary, BASE_NOW, 45.0)
    assert isinstance(r, dict) and "verdict" in r


@pytest.mark.parametrize("row", NON_DICT_ROWS)
def test_still_detects_a_wedge_with_a_malformed_row_present(tmp_path, row):
    """MUTATION PROOF for the guard's WIDTH. A guard that dropped every row
    would pass the no-raise cases above while permanently reporting 'clean' --
    i.e. it would silence the detector instead of fixing it, which is the same
    outcome as the defect. The malformed row must be dropped and the real marker
    around it must still produce 'wedged'."""
    diary = _diary(tmp_path, [row, _marker("phase_start", "phase-0-precheck", 60, "g-500-01")])
    r = WEDGE.check_wedge(diary, BASE_NOW, 45.0)
    assert r["verdict"] == "wedged"
    assert r["stuck_phase"] == "phase-0-precheck"


def test_still_reports_clean_with_a_malformed_row_present(tmp_path):
    """The other direction: the guard must not manufacture a wedge either."""
    diary = _diary(tmp_path, ["null",
                              _marker("phase_start", "phase-4-execute", 130, "g-500-02"),
                              _marker("phase_end", "phase-4-execute", 120, "g-500-02")])
    assert WEDGE.check_wedge(diary, BASE_NOW, 45.0)["verdict"] == "clean"


def test_load_markers_drops_non_dicts_and_keeps_the_real_markers(tmp_path):
    """The loader's own contract, stated once at the level it lives at."""
    diary = _diary(tmp_path, NON_DICT_ROWS + [
        _marker("phase_start", "phase-1", 30, "g-1"),
        _marker("phase_end", "phase-1", 20, "g-1"),
    ])
    markers = PCR._load_markers(diary)
    assert [m["entry_type"] for m in markers] == ["phase_start", "phase_end"]
    assert all(isinstance(m, dict) for m in markers)


def test_undecodable_line_is_still_skipped(tmp_path):
    """The pre-existing JSONDecodeError path must survive the new guard --
    the two skips are independent and a non-dict guard placed wrongly could
    shadow it."""
    diary = _diary(tmp_path, ["{not json", _marker("phase_start", "phase-1", 60, "g-1")])
    assert WEDGE.check_wedge(diary, BASE_NOW, 45.0)["verdict"] == "wedged"


# --- (2) the tz-aware half (guard-1398 SSOT) --------------------------------

def test_parse_ts_returns_naive_for_an_offset_bearing_stamp():
    """Pre-fix this returned an AWARE datetime, and the TypeError surfaced in
    whichever consumer did the arithmetic rather than here."""
    ts = PCR._parse_ts({"timestamp": "2026-08-08T20:00:00+00:00"})
    assert ts is not None and ts.tzinfo is None


def test_parse_ts_handles_the_z_suffix_without_the_replace_bug():
    """`.replace("Z","")` silently reinterprets a UTC stamp as local time. The
    SSOT converts Z to +00:00 and strips tzinfo AFTER parsing, so the wall-clock
    value is preserved rather than shifted."""
    assert PCR._parse_ts({"timestamp": "2026-08-08T20:00:00Z"}) == datetime(2026, 8, 8, 20, 0, 0)


@pytest.mark.parametrize("bad", [None, "", "null", "not-a-timestamp", 42, []])
def test_parse_ts_returns_none_rather_than_raising(bad):
    """The SSOT never raises; a row with an unusable stamp is skipped, not fatal."""
    assert PCR._parse_ts({"timestamp": bad}) is None


def test_parse_ts_missing_key_is_none():
    assert PCR._parse_ts({}) is None


def test_check_wedge_survives_a_tz_aware_marker(tmp_path):
    """End-to-end for defect (2) in the production path: an offset-bearing
    marker must not take the detector down."""
    diary = _diary(tmp_path, [_marker("phase_start", "phase-0-precheck", 60, "g-1",
                                      tz_suffix="+00:00")])
    r = WEDGE.check_wedge(diary, BASE_NOW, 45.0)
    assert isinstance(r, dict) and "verdict" in r
