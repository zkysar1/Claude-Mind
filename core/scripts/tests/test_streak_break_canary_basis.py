"""0 (0 item b): streak-break canary basis classification.

The streak-break CANARY fired on expected_interval_hours = interval_hours, so
selection-gated goals (interval 2.67h, actual median ~10.9h) and signal-gated
goals (fire_when set, vestigial interval) recorded a canary-worthy break on
nearly EVERY fire (g-001-01: 25/25). The fix keeps EMISSION unconditional
(cargo-cult contract-suppression reads actual_elapsed_hours from every record
— rb-1391 interplay) and adds canary/basis fields the reflector uses to skip
filing Investigates for informational breaks.

Covers:
  1. _streak_break_canary_fields (pure): signal-gated always informational;
     <min_samples falls back to interval basis; chronic-late p50 raises the
     basis (canary=False inside it, True beyond it); p50 below interval never
     LOWERS the basis.
  2. _recent_break_actuals: same-goal filter, window cap, corrupt-line skip,
     missing-file fail-open.
  3. Reflector Pass-1 gate semantics: canary=False → informational (no
     filing); legacy records without the field → file as before.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
sys.path.insert(0, str(CORE_SCRIPTS))
sys.path.insert(0, str(PROJECT_ROOT))

MODULE_PATH = PROJECT_ROOT / "mind_api" / "src" / "endpoints" / "aspirations_write.py"


def _load_helpers():
    """Load ONLY the two pure helpers without importing the daemon package.

    aspirations_write.py imports daemon-relative modules (..server etc.) at
    module level, so a plain import fails outside the package. Exec just the
    two helper functions from the source text instead — they are pure
    (json/statistics/Path only).
    """
    src = MODULE_PATH.read_text(encoding="utf-8")
    tree_src = []
    import ast
    mod = ast.parse(src)
    for node in mod.body:
        if isinstance(node, ast.FunctionDef) and node.name in (
                "_recent_break_actuals", "_streak_break_canary_fields"):
            tree_src.append(ast.get_source_segment(src, node))
    ns = {"json": json, "Path": Path,
          "List": list, "Dict": dict, "Any": object}
    # Typing names appear only in annotations (not evaluated at runtime
    # thanks to `from __future__ import annotations` in the source module),
    # but exec'd standalone defs evaluate them — neutralize via globals.
    exec("from typing import Any, Dict, List\n" + "\n\n".join(tree_src), ns)
    return ns["_recent_break_actuals"], ns["_streak_break_canary_fields"]


_recent_break_actuals, _streak_break_canary_fields = _load_helpers()


# ── _streak_break_canary_fields (pure classifier) ─────────────────────────

def test_signal_gated_always_informational():
    f = _streak_break_canary_fields(1.33, 50.0, 2.0, [40, 45, 50], True)
    assert f == {"canary": False, "canary_basis_hours": None,
                 "basis_reason": "signal_gated"}


def test_no_history_falls_back_to_interval_basis():
    # elapsed 10.9h vs interval 2.67h, streak_mult 2 → break by interval
    # basis; with no history the canary fires (old behavior preserved).
    f = _streak_break_canary_fields(2.67, 10.9, 2.0, [], False)
    assert f["canary"] is True
    assert f["basis_reason"] == "interval"
    assert f["canary_basis_hours"] == 2.67


def test_below_min_samples_falls_back_to_interval():
    f = _streak_break_canary_fields(2.67, 10.9, 2.0, [10.5, 11.2], False)
    assert f["canary"] is True
    assert f["basis_reason"] == "interval"


def test_chronic_late_p50_suppresses_canary():
    # The rb-1391 shape: interval 2.67h, recent actuals ~10-12h. A 10.9h
    # elapsed is ON its demonstrated cadence — informational, not drift.
    f = _streak_break_canary_fields(2.67, 10.9, 2.0, [10.5, 10.91, 12.0],
                                    False)
    assert f["canary"] is False
    assert f["basis_reason"] == "recent_actual_p50"
    assert f["canary_basis_hours"] == 10.91


def test_late_beyond_own_cadence_still_fires():
    # elapsed 30h > 2 x p50 (10.91) — late even by demonstrated cadence.
    f = _streak_break_canary_fields(2.67, 30.0, 2.0, [10.5, 10.91, 12.0],
                                    False)
    assert f["canary"] is True
    assert f["basis_reason"] == "recent_actual_p50"


def test_p50_below_interval_never_lowers_basis():
    # Fast actuals (p50 < interval) keep the interval basis — the p50 only
    # RAISES the bar, never tightens it.
    f = _streak_break_canary_fields(24.0, 60.0, 2.0, [4.0, 5.0, 6.0], False)
    assert f["basis_reason"] == "interval"
    assert f["canary_basis_hours"] == 24.0
    assert f["canary"] is True


def test_nonpositive_history_values_ignored():
    f = _streak_break_canary_fields(2.67, 10.9, 2.0, [0, -3, None, 10.9],
                                    False)
    # Only one valid sample (10.9) < min_samples → interval basis.
    assert f["basis_reason"] == "interval"


# ── _recent_break_actuals (log reader) ─────────────────────────────────────

def _write_log(tmp_path, records):
    p = tmp_path / "streak-breaks.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        for r in records:
            f.write((r if isinstance(r, str) else json.dumps(r)) + "\n")
    return p


def test_reader_filters_goal_and_caps_window(tmp_path):
    recs = [{"goal_id": "g-1", "actual_elapsed_hours": float(i)}
            for i in range(1, 9)]
    recs.insert(3, {"goal_id": "g-2", "actual_elapsed_hours": 99.0})
    p = _write_log(tmp_path, recs)
    vals = _recent_break_actuals(p, "g-1", window=5)
    assert vals == [4.0, 5.0, 6.0, 7.0, 8.0]   # last 5 of g-1 only


def test_reader_skips_corrupt_lines_and_bad_values(tmp_path):
    p = _write_log(tmp_path, [
        '{"goal_id": "g-1", "actual_elapsed_hours": 3.0}',
        "{not json",
        '{"goal_id": "g-1", "actual_elapsed_hours": "NaN-ish"}',
        '{"goal_id": "g-1", "actual_elapsed_hours": 4.0}',
    ])
    assert _recent_break_actuals(p, "g-1") == [3.0, 4.0]


def test_reader_missing_file_returns_empty(tmp_path):
    assert _recent_break_actuals(tmp_path / "absent.jsonl", "g-1") == []


# ── Reflector Pass-1 gate semantics ────────────────────────────────────────
# The gate is `entry.get("canary") is False` — assert the three-way contract
# on the exact expression the reflector uses (source-pinned below).

def test_reflector_gate_three_way_contract():
    informational = {"goal_id": "g-1", "canary": False}
    canary_worthy = {"goal_id": "g-1", "canary": True}
    legacy = {"goal_id": "g-1"}                      # pre-0 record
    assert (informational.get("canary") is False) is True    # suppressed
    assert (canary_worthy.get("canary") is False) is False   # files
    assert (legacy.get("canary") is False) is False          # files (compat)


def test_reflector_source_contains_basis_gate():
    src = (CORE_SCRIPTS / "streak-break-reflector.py").read_text(
        encoding="utf-8")
    assert 'entry.get("canary") is False' in src
    assert "informational_break" in src
    assert "basis_suppressed_count" in src
