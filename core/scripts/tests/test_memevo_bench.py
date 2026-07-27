"""MemEvoBench (G8-S3c) — standing gate-ladder regression for the
memory-misevolution governance eval and the shared active-forgetting predicate.

Pins four things that a silent regression would break:
  1. Gate ladder — DEFENSE holds the self-poisoning fraction at/below the
     pre-registered max; NULL exceeds the pre-registered floor (null-arm pairing
     proves the eval detects a real governance failure, not nothing).
  2. Non-degeneracy (rb-4485) — the defense is not a retire-all degenerate
     (falseRetirements == 0) and not an allow-all degenerate (control set's
     genuine-dead is caught).
  3. Determinism — seeded + wall-clock-free, so two runs are byte-identical.
  4. Single source of truth — bulk-retire-dead-entries.py's _is_candidate IS
     the shared _curation_predicate.is_dead_entry (the g-336-56 extraction), so
     the production retirement tool and the eval can never drift apart.

Also acts as the drift-guard pinning the predicate's canonical verdicts, so an
edit to _curation_predicate.py that changes retirement behavior fails loudly
here.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

import memevo_bench as mb  # noqa: E402
from _curation_predicate import is_dead_entry  # noqa: E402


def _load(alias, filename):
    path = CORE_SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(alias, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Gate ladder ──────────────────────────────────────────────────────────────
def test_bench_passes():
    assert mb.run_memevo_bench()["passed"] is True


def test_defense_holds_drift_at_or_below_max():
    v = mb.run_memevo_bench()
    assert v["defense"]["driftFraction"] <= mb.MEMEVO_DEFENSE_DRIFT_MAX


def test_null_exceeds_failure_floor():
    # Null-arm pairing (spec §5): a NO-curation store MUST drift past the floor,
    # else the bench is measuring nothing.
    v = mb.run_memevo_bench()
    assert v["null"]["driftFraction"] >= mb.MEMEVO_NULL_DRIFT_MIN


def test_defense_beats_null_by_a_clear_gap():
    # The whole point: active forgetting materially separates from no curation.
    v = mb.run_memevo_bench()
    assert v["defense"]["driftFraction"] < v["null"]["driftFraction"]
    assert (v["null"]["driftFraction"] - v["defense"]["driftFraction"]) >= 0.20


# ── Non-degeneracy (rb-4485) ─────────────────────────────────────────────────
def test_defense_is_not_a_retire_all_degenerate():
    # A retire-everything degenerate would also drive drift to 0 — but it would
    # retire legitimate entries. Real predicate: zero false retirements.
    v = mb.run_memevo_bench()
    assert v["defense"]["falseRetirements"] == 0


def test_control_set_legitimate_entries_survive():
    v = mb.run_memevo_bench()
    assert v["nonDegeneracy"]["false_retirements"] == 0


def test_control_set_genuine_dead_is_caught():
    # An allow-all degenerate would MISS the positive control.
    v = mb.run_memevo_bench()
    assert v["nonDegeneracy"]["missed_dead"] == 0


def test_control_detail_matches_predicate_intent():
    detail = mb.run_control_set()["detail"]
    assert detail["heavily-used-and-helpful"] is False
    assert detail["young-but-dead-looking"] is False
    assert detail["inferred-helpful-only"] is False  #  protection
    assert detail["genuine-dead"] is True


# ── Determinism ──────────────────────────────────────────────────────────────
def test_deterministic_across_runs():
    assert mb.run_memevo_bench() == mb.run_memevo_bench()


def test_seed_is_pinned():
    # A silent seed change would make the corpus (and thus the verdict) drift.
    assert mb.MEMEVO_SEED == 0x6D_65_6D_6F
    assert mb.run_memevo_bench()["seed"] == 0x6D_65_6D_6F


# ── Drift-guard: pin the shared predicate's canonical verdicts ───────────────
def test_predicate_canonical_dead_entry():
    today = date(2026, 6, 1)
    dead = {
        "status": "active",
        "created": (today - timedelta(days=90)).isoformat(),
        "utilization": {"retrieval_count": 250, "times_helpful": 0,
                        "times_cited": 0, "times_inferred_helpful": 0},
    }
    assert is_dead_entry(dead, 100, 30, today) is True


def test_predicate_canonical_live_entries():
    today = date(2026, 6, 1)
    base_created = (today - timedelta(days=90)).isoformat()
    # helpful → spared
    assert is_dead_entry(
        {"status": "active", "created": base_created,
         "utilization": {"retrieval_count": 250, "times_helpful": 1,
                         "times_cited": 0, "times_inferred_helpful": 0}},
        100, 30, today) is False
    # too young → spared
    assert is_dead_entry(
        {"status": "active", "created": (today - timedelta(days=5)).isoformat(),
         "utilization": {"retrieval_count": 250, "times_helpful": 0,
                         "times_cited": 0, "times_inferred_helpful": 0}},
        100, 30, today) is False
    # below retrieval bar → spared
    assert is_dead_entry(
        {"status": "active", "created": base_created,
         "utilization": {"retrieval_count": 50, "times_helpful": 0,
                         "times_cited": 0, "times_inferred_helpful": 0}},
        100, 30, today) is False
    # non-active → spared
    assert is_dead_entry(
        {"status": "retired", "created": base_created,
         "utilization": {"retrieval_count": 250, "times_helpful": 0,
                         "times_cited": 0, "times_inferred_helpful": 0}},
        100, 30, today) is False


# ── Single source of truth (the  extraction) ─────────────────────────
def test_bulk_retire_uses_the_shared_predicate():
    # bulk-retire-dead-entries.py's _is_candidate MUST be the shared predicate
    # object — that identity is what guarantees the production retirement tool
    # and MemEvoBench can never diverge.
    bre = _load("bulk_retire_dead_entries_memevo_t", "bulk-retire-dead-entries.py")
    assert bre._is_candidate is is_dead_entry
