"""Tests for pre-apply-consult-drift-gate.py ().

The gate is the CONSUMER for iteration-close.sh's `retrieval-summary:
performed=false` signal: on N consecutive framework-touching DEEP closes with
retrieval performed=false it directs iteration-close.sh to set the
`force_pre_apply_consult` WM sentinel (aspirations-precheck Phase 0-pre6 then
enforces the code-review-protocol step-4 consult).

These tests pin the ACCEPTANCE criteria from g-115-2201:
  - 2 consecutive framework-deep misses raise the sentinel (trip at threshold).
  - A ROUTINE goal, or a deep goal touching NO framework paths, must NOT trip
    AND must not perturb the streak (the "no tax on every close" guarantee —
    interpretation B: routine/non-framework closes are transparent).
  - A framework-deep close that DID consult (performed=true) resets the streak.
  - Fail-open: bad input yields a no-op decision, never a spurious trip.

Pattern: same importlib + sys.path shape as test_defer_drift_check.py (the
script name has hyphens, so it cannot be a plain `import`).
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "pre-apply-consult-drift-gate.py"


def _import():
    spec = importlib.util.spec_from_file_location("pre_apply_consult_drift_gate", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["pre_apply_consult_drift_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


MOD = _import()


# ── decide(): the pure ACCEPTANCE core ───────────────────────────────────────

def test_framework_deep_miss_increments_below_threshold():
    d = MOD.decide("deep", performed=False, streak=0, work_class="framework", threshold=2)
    assert d["new_streak"] == 1
    assert d["set_sentinel"] is False   # 1 < 2, not yet
    assert d["is_framework"] is True
    assert d["trips"] is True            # it IS a counted framework-deep miss


def test_framework_deep_miss_trips_at_threshold():
    d = MOD.decide("deep", performed=False, streak=1, work_class="framework", threshold=2)
    assert d["new_streak"] == 2
    assert d["set_sentinel"] is True     # 2 >= 2 -> force the consult
    assert d["is_framework"] is True


def test_framework_deep_miss_keeps_firing_past_threshold():
    # Continued drift keeps setting the sentinel (precheck one-shot-consumes it).
    d = MOD.decide("deep", performed=False, streak=5, work_class="framework", threshold=2)
    assert d["new_streak"] == 6
    assert d["set_sentinel"] is True


def test_framework_deep_consulted_resets():
    # performed=true — they ran the consult. Streak resets, no sentinel.
    d = MOD.decide("deep", performed=True, streak=3, work_class="framework", threshold=2)
    assert d["new_streak"] == 0
    assert d["set_sentinel"] is False
    assert d["trips"] is False


def test_routine_goal_is_transparent_no_trip_no_reset():
    # ACCEPTANCE: a routine goal must NOT trip AND must not become a tax /
    # reset a real framework-deep drift run (interpretation B: unchanged).
    d = MOD.decide("routine", performed=False, streak=1, work_class="framework", threshold=2)
    assert d["set_sentinel"] is False
    assert d["trips"] is False
    assert d["new_streak"] == 1          # UNCHANGED, not reset to 0


def test_non_framework_deep_is_transparent_no_trip_no_reset():
    # ACCEPTANCE: a deep goal touching NO framework paths must NOT trip AND
    # must not perturb the streak.
    d = MOD.decide("deep", performed=False, streak=1, work_class="product", threshold=2)
    assert d["set_sentinel"] is False
    assert d["trips"] is False
    assert d["new_streak"] == 1          # UNCHANGED
    assert d["is_framework"] is False


def test_non_framework_deep_with_empty_work_class_transparent():
    d = MOD.decide("deep", performed=False, streak=2, work_class="", threshold=2)
    assert d["set_sentinel"] is False
    assert d["new_streak"] == 2          # unchanged — empty class is not framework


def test_interpretation_b_routine_between_misses_still_trips():
    # Two framework-deep misses with a routine close interleaved must still
    # reach the threshold (the routine close left the streak untouched).
    d1 = MOD.decide("deep", performed=False, streak=0, work_class="framework", threshold=2)
    assert d1["new_streak"] == 1
    d_routine = MOD.decide("routine", performed=False, streak=d1["new_streak"],
                           work_class="framework", threshold=2)
    assert d_routine["new_streak"] == 1   # untouched by the routine close
    d2 = MOD.decide("deep", performed=False, streak=d_routine["new_streak"],
                    work_class="framework", threshold=2)
    assert d2["new_streak"] == 2
    assert d2["set_sentinel"] is True


def test_threshold_of_one_trips_on_first_miss():
    d = MOD.decide("deep", performed=False, streak=0, work_class="framework", threshold=1)
    assert d["new_streak"] == 1
    assert d["set_sentinel"] is True


def test_outcome_case_insensitive():
    d = MOD.decide("DEEP", performed=False, streak=1, work_class="framework", threshold=2)
    assert d["set_sentinel"] is True


def test_negative_streak_clamped():
    d = MOD.decide("deep", performed=False, streak=-5, work_class="framework", threshold=2)
    assert d["new_streak"] == 1          # max(0, -5) + 1


def test_threshold_floor_is_one():
    # threshold <= 0 is clamped to 1 so the gate can still fire.
    d = MOD.decide("deep", performed=False, streak=0, work_class="framework", threshold=0)
    assert d["set_sentinel"] is True


# ── framework_edited gate () ───────────────────────────────────────
# A framework-CLASSIFIED deep close that edited NO framework file (a read-only
# diagnostic — tree scan, gate audit) must be transparent: it has nothing to
# pre-apply-consult FOR, so it must neither increment the streak nor trip the
# sentinel. Only a framework-deep close that ACTUALLY edited a framework file is
# a real miss. This is the false-positive class  fixes while
# preserving the  real-drift catch.

def test_framework_deep_no_file_edited_is_transparent():
    # THE  fix: framework + deep + performed=false, but edited no
    # framework file -> UNCHANGED, no trip, no sentinel.
    d = MOD.decide("deep", performed=False, streak=1, work_class="framework",
                   threshold=2, framework_edited=False)
    assert d["new_streak"] == 1          # UNCHANGED — not incremented
    assert d["set_sentinel"] is False
    assert d["trips"] is False
    assert d["is_framework"] is True
    assert d["framework_edited"] is False


def test_framework_deep_no_file_edited_does_not_reset():
    # A read-only diagnostic that DID run retrieve.sh must NOT reset a real
    # framework-deep drift run — it is not part of the framework-file-editing
    # population, so it is transparent (not a reset).
    d = MOD.decide("deep", performed=True, streak=3, work_class="framework",
                   threshold=2, framework_edited=False)
    assert d["new_streak"] == 3          # UNCHANGED — neither incremented nor reset
    assert d["set_sentinel"] is False
    assert d["trips"] is False


def test_framework_deep_file_edited_still_increments():
    # Real-drift catch preserved: framework + deep + performed=false + edited a
    # framework file -> increments and trips at threshold, exactly as .
    d = MOD.decide("deep", performed=False, streak=1, work_class="framework",
                   threshold=2, framework_edited=True)
    assert d["new_streak"] == 2
    assert d["set_sentinel"] is True
    assert d["trips"] is True


def test_framework_deep_file_edited_consulted_resets():
    # Genuine framework edit that consulted -> reset (the  reset path).
    d = MOD.decide("deep", performed=True, streak=4, work_class="framework",
                   threshold=2, framework_edited=True)
    assert d["new_streak"] == 0
    assert d["set_sentinel"] is False


def test_framework_edited_defaults_true_backward_compat():
    # Absent framework_edited -> True, so every pre- call site and the
    # git-signal-unavailable path preserve the original increment behavior.
    d = MOD.decide("deep", performed=False, streak=1, work_class="framework", threshold=2)
    assert d["framework_edited"] is True
    assert d["new_streak"] == 2          # increments as before
    assert d["set_sentinel"] is True


def test_run_of_diagnostics_never_climbs_streak():
    # ACCEPTANCE (the observed symptom): a RUN of framework-classified read-only
    # diagnostic closes must leave the streak flat and never trip. Before the
    # fix this run climbed the streak to threshold and re-fired every iteration.
    streak = 0
    for _ in range(6):
        d = MOD.decide("deep", performed=False, streak=streak, work_class="framework",
                       threshold=2, framework_edited=False)
        assert d["set_sentinel"] is False
        assert d["trips"] is False
        streak = d["new_streak"]
    assert streak == 0                   # never moved across 6 diagnostic closes


def test_framework_edited_in_return_contract():
    d = MOD.decide("deep", performed=False, streak=0, work_class="framework",
                   threshold=2, framework_edited=True)
    assert "framework_edited" in d


# ── work_class resolution ────────────────────────────────────────────────────

def test_resolve_work_class_explicit_bypasses_mapping():
    # Explicit work_class wins without touching the _work_class mapping.
    assert MOD._resolve_work_class("any-category", "framework") == "framework"
    assert MOD._resolve_work_class("", "product") == "product"


def test_resolve_work_class_empty_category_is_empty():
    assert MOD._resolve_work_class("", "") == ""


def test_resolve_work_class_via_mapping_monkeypatched(monkeypatch):
    # Hermetic: inject a fake _work_class so the test does not depend on the
    # deployment's mapping files. A framework-mapped category resolves to
    # "framework"; "unclassified" collapses to "".
    fake = types.ModuleType("_work_class")
    fake.resolve = lambda cat: {"cat-fw": "framework", "cat-x": "unclassified"}.get(cat, "unclassified")
    monkeypatch.setitem(sys.modules, "_work_class", fake)
    assert MOD._resolve_work_class("cat-fw", "") == "framework"
    assert MOD._resolve_work_class("cat-x", "") == ""     # unclassified -> ""


def test_resolve_work_class_framework_category_then_trips(monkeypatch):
    fake = types.ModuleType("_work_class")
    fake.resolve = lambda cat: "framework" if cat == "framework-architecture" else "unclassified"
    monkeypatch.setitem(sys.modules, "_work_class", fake)
    wc = MOD._resolve_work_class("framework-architecture", "")
    d = MOD.decide("deep", performed=False, streak=1, work_class=wc, threshold=2)
    assert d["is_framework"] is True
    assert d["set_sentinel"] is True


def test_resolve_explicit_unclassified_falls_through_to_category(monkeypatch):
    #  regression pin (incident ): the goal-creation
    # stamper bakes work_class="unclassified" onto records whose category is
    # unmapped at creation time. An explicit "unclassified" must NOT win
    # verbatim — it falls through to live category resolution so a later
    # mapping extension self-heals the stale stamp.
    fake = types.ModuleType("_work_class")
    fake.resolve = lambda cat: {"framework-guardrails-and-gates": "framework"}.get(cat, "unclassified")
    monkeypatch.setitem(sys.modules, "_work_class", fake)
    wc = MOD._resolve_work_class("framework-guardrails-and-gates", "unclassified")
    assert wc == "framework"


def test_consulted_close_resets_streak_despite_unclassified_stamp(monkeypatch):
    # END-TO-END incident shape ( acceptance): a framework-category
    # deep close stamped work_class="unclassified" that DID consult
    # (performed=true) must RESET the miss streak — before the fix it was
    # transparent (streak unchanged), so the sentinel re-tripped on the next
    # framework close even though the discipline was followed.
    fake = types.ModuleType("_work_class")
    fake.resolve = lambda cat: {"framework-guardrails-and-gates": "framework"}.get(cat, "unclassified")
    monkeypatch.setitem(sys.modules, "_work_class", fake)
    wc = MOD._resolve_work_class("framework-guardrails-and-gates", "unclassified")
    d = MOD.decide("deep", performed=True, streak=2, work_class=wc, threshold=2)
    assert d["is_framework"] is True
    assert d["new_streak"] == 0          # the reset the incident never got
    assert d["set_sentinel"] is False


def test_live_mapping_covers_incident_category():
    # Layer-2 pin: the REAL core mapping must know the incident category (it
    # was unmapped at 's creation — that gap created the stale
    # stamp). Uses the deployment mapping deliberately (no monkeypatch).
    import importlib
    sys.modules.pop("_work_class", None)
    wc_mod = importlib.import_module("_work_class")
    assert wc_mod.resolve("framework-guardrails-and-gates") == "framework"


# ── fail-open ────────────────────────────────────────────────────────────────

def test_decide_returns_full_contract_keys():
    d = MOD.decide("deep", performed=False, streak=0, work_class="framework", threshold=2)
    for k in ("new_streak", "set_sentinel", "work_class", "is_framework",
              "framework_edited", "trips", "reason"):
        assert k in d


# ── CLI wiring for --framework-edited () ───────────────────────────

def _run_cli(*args):
    """Invoke the gate as a subprocess (STORAGE_BACKEND=local per guard-955) and
    return the parsed JSON stdout."""
    import json
    import os
    import subprocess
    env = dict(os.environ, STORAGE_BACKEND="local")
    out = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, env=env, check=True,
    ).stdout
    return json.loads(out)


def test_cli_framework_edited_false_is_transparent():
    # The new flag threads through main()->decide(): explicit false makes a
    # framework-deep miss transparent.
    d = _run_cli("--outcome", "deep", "--performed", "false", "--streak", "1",
                 "--work-class", "framework", "--threshold", "2",
                 "--framework-edited", "false")
    assert d["framework_edited"] is False
    assert d["new_streak"] == 1          # unchanged
    assert d["set_sentinel"] is False


def test_cli_framework_edited_absent_defaults_true():
    # Absent flag -> True (backward-compat / fail-safe): the miss still counts.
    d = _run_cli("--outcome", "deep", "--performed", "false", "--streak", "1",
                 "--work-class", "framework", "--threshold", "2")
    assert d["framework_edited"] is True
    assert d["new_streak"] == 2
    assert d["set_sentinel"] is True


def test_cli_framework_edited_true_still_trips():
    d = _run_cli("--outcome", "deep", "--performed", "false", "--streak", "1",
                 "--work-class", "framework", "--threshold", "2",
                 "--framework-edited", "true")
    assert d["set_sentinel"] is True


def test_resolve_work_class_failopen_on_import_error(monkeypatch):
    # If _work_class cannot be imported/loaded, resolution fails open to "" so
    # the gate can never trip on a resolution error.
    def _boom(cat):
        raise RuntimeError("mapping unavailable")
    fake = types.ModuleType("_work_class")
    fake.resolve = _boom
    monkeypatch.setitem(sys.modules, "_work_class", fake)
    assert MOD._resolve_work_class("cat-fw", "") == ""
