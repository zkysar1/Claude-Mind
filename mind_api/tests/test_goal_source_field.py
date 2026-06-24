"""Unit tests for goal_source field ().

Direct-import tests against core/scripts/_goal_source.py — the SSOT helper
shared by both the fallback (aspirations.py) and daemon
(mind_api/src/endpoints/aspirations_write.py) write paths. Covers:
  1. infer() maps each canonical prefix family
  2. infer() recognizes the production-variant prefixes
  3. infer() returns None on unknown / empty input
  4. validate_goal accepts null and each valid enum value
  5. validate_goal rejects invalid enum values
  6. apply_default() mutates absent / null fields and preserves explicit ones
  7. Backfill is idempotent (a goal with goal_source already set is skipped)
  8. Both write paths import the SSOT helper (parity smoke test)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "core" / "scripts"
sys.path.insert(0, str(SCRIPTS))

# Direct imports; no daemon, no env required.
import _goal_source  # type: ignore  # noqa: E402
import aspirations  # type: ignore  # noqa: E402


# ---------------------------------------------------------------------------
# _infer_goal_source_from_signal — canonical prefixes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("signal,expected", [
    ("user_directive", "user"),
    ("pending_question:pq-abc-123", "user"),
    ("recurring_cadence:g-001-01", "recurring-cycle"),
    ("failing_test:test_foo", "cycle-detector"),
    ("resolved_hypothesis:2026-05-13_h1", "cycle-detector"),
    ("low_confidence_node:framework/foo", "cycle-detector"),
    ("decomposition:g-001-02", "agent-self"),
    ("parent_aspiration:asp-001", "agent-self"),
    ("unblock:g-115-01", "agent-self"),
    ("investigate:strategic-scan-x", "agent-self"),
    ("idea:cargo-cult-detector", "agent-self"),
    ("maintain:asp-001", "agent-self"),
    ("board_post:msg-20260513-abc", "agent-self"),
    ("idle_fallback", "agent-self"),
])
def test_infer_canonical_prefixes(signal, expected):
    assert _goal_source.infer(signal) == expected


# ---------------------------------------------------------------------------
# _infer_goal_source_from_signal — production-variant prefixes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("signal,expected", [
    ("user-directed:zeta-feedback", "user"),
    ("user_directed:bravo-cli", "user"),
    ("recurring:infra-streak", "recurring-cycle"),
    ("drift_detected:asp-250", "cycle-detector"),
    ("monitor:bitnet-prod", "cycle-detector"),
    ("investigation:g-250-79", "agent-self"),
    ("apply:g-250-74", "agent-self"),
    ("brief:zeta-handoff", "agent-self"),
])
def test_infer_variant_prefixes(signal, expected):
    assert _goal_source.infer(signal) == expected


# ---------------------------------------------------------------------------
# _infer_goal_source_from_signal — uninferable inputs
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("signal", [
    None,
    "",
    "   ",
    "session-100-bug2-followup",       # genuinely non-prefixed legacy label
    "rb-466",                          # reasoning-bank ref, not a signal
    "out-of-cycle-work-capture",       # free text
    "goal-selector_unicode_decode_error_iter-4",
    123,                               # non-string
])
def test_infer_returns_none_for_uninferable(signal):
    assert _goal_source.infer(signal) is None


# ---------------------------------------------------------------------------
# validate_goal — goal_source field
# ---------------------------------------------------------------------------

def _minimal_goal(**kwargs):
    """Smallest goal record that passes validate_goal."""
    base = {"id": "g-999-01", "status": "pending"}
    base.update(kwargs)
    return base


@pytest.mark.parametrize("source", [
    None,
    "user",
    "agent-self",
    "recurring-cycle",
    "cycle-detector",
    "forge-skill",
])
def test_validate_goal_accepts_valid_source(source):
    g = _minimal_goal(goal_source=source)
    # Should not raise
    aspirations.validate_goal(g)


@pytest.mark.parametrize("bad", [
    "invalid-value",
    "USER",                   # case-sensitive enum
    "agent_self",             # underscore not hyphen
    "cycle",                  # truncated
    "forgery",                # typo of valid value
    42,                       # non-string
])
def test_validate_goal_rejects_invalid_source(bad):
    g = _minimal_goal(goal_source=bad)
    with pytest.raises(ValueError, match="goal_source"):
        aspirations.validate_goal(g)


def test_validate_goal_field_optional():
    """Goal record without goal_source key passes validation."""
    g = _minimal_goal()
    assert "goal_source" not in g
    aspirations.validate_goal(g)


# ---------------------------------------------------------------------------
# Backfill idempotency
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# apply_default — the SSOT mutation helper both write paths call
# ---------------------------------------------------------------------------

def test_apply_default_fills_absent_field():
    g = {"id": "g-999-01", "origin_signal": "user_directive"}
    _goal_source.apply_default(g)
    assert g["goal_source"] == "user"


def test_apply_default_fills_explicit_null():
    g = {"id": "g-999-01", "origin_signal": "decomposition:g-000-00",
         "goal_source": None}
    _goal_source.apply_default(g)
    assert g["goal_source"] == "agent-self"


def test_apply_default_preserves_explicit_value():
    """Explicit caller value always wins, even when origin_signal would
    infer a different source."""
    g = {"id": "g-999-01", "origin_signal": "decomposition:g-000-00",
         "goal_source": "user"}  # mismatched on purpose
    _goal_source.apply_default(g)
    assert g["goal_source"] == "user"


def test_apply_default_noop_on_uninferable_signal():
    g = {"id": "g-999-01", "origin_signal": "session-100-legacy-label"}
    _goal_source.apply_default(g)
    assert "goal_source" not in g  # untouched


# ---------------------------------------------------------------------------
# Parity smoke test — both write paths import the SSOT helper
# ---------------------------------------------------------------------------

def test_fallback_path_imports_ssot():
    """aspirations.py (fallback / CLI path) must call the shared helper.

    Daemon-side parity (mind_api/src/endpoints/aspirations_write.py) is
    asserted by its mirror `test_daemon_path_imports_ssot` below (g-305-14,
    landed 2026-06-12).
    """
    assert aspirations._apply_goal_source_default is _goal_source.apply_default


def test_daemon_path_imports_ssot():
    """mind_api daemon path (aspirations_write.py) must call the shared helper.

    Daemon-side mirror of test_fallback_path_imports_ssot (g-305-14). The
    add-goal hot path binds `_apply_goal_source_default` to the shared
    `_goal_source.apply_default` at import time (aspirations_write.py ~L99)
    and invokes it at Phase C.5 of `_run_add_goal_pipeline` (~L854). This
    parity smoke test guards against a drift where the daemon path
    re-implements or shadows the SSOT helper, which would leave daemon-filed
    goals with `goal_source=null` while CLI-filed goals get it populated.
    """
    from mind_api.src.endpoints import aspirations_write
    assert aspirations_write._apply_goal_source_default is _goal_source.apply_default


# ---------------------------------------------------------------------------
# Backfill idempotency
# ---------------------------------------------------------------------------

def test_backfill_skips_goals_with_existing_source(tmp_path):
    """Backfill must not overwrite an existing goal_source value, even if the
    origin_signal inference would yield a different one. Idempotent across
    repeated runs is the contract from the script's docstring."""
    import json
    import importlib.util

    world_path = tmp_path / "world"
    world_path.mkdir()

    asp = {
        "id": "asp-001", "title": "test", "status": "active",
        "priority": "MEDIUM", "archived": False,
        "goals": [
            {"id": "g-001-01", "status": "pending",
             "origin_signal": "decomposition:g-000-00",
             "goal_source": "user"},  # explicit, mismatched on purpose
            {"id": "g-001-02", "status": "pending",
             "origin_signal": "decomposition:"},  # unset
        ],
    }
    (world_path / "aspirations.jsonl").write_text(
        json.dumps(asp) + "\n", encoding="utf-8"
    )

    spec = importlib.util.spec_from_file_location(
        "backfill_goal_source", SCRIPTS / "backfill-goal-source.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # _backfill_source takes path as an explicit arg; no _paths patching needed.
    counters_1: dict = {}
    mod._backfill_source("world", world_path / "aspirations.jsonl", True, counters_1)

    assert counters_1["world"]["changed"] == 1   # only  should change
    assert counters_1["world"]["already"] == 1   #  skipped

    written = [json.loads(ln) for ln in
               (world_path / "aspirations.jsonl").read_text(encoding="utf-8").splitlines()
               if ln.strip()]
    by_id = {g["id"]: g for g in written[0]["goals"]}
    assert by_id[""]["goal_source"] == "user"        # preserved
    assert by_id[""]["goal_source"] == "agent-self"  # filled

    counters_2: dict = {}
    mod._backfill_source("world", world_path / "aspirations.jsonl", True, counters_2)
    assert counters_2["world"]["changed"] == 0
    assert counters_2["world"]["already"] == 2
