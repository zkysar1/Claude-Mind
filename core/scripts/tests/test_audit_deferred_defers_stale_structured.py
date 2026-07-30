"""Tests for audit-deferred-defers.py classify() stale-structured downgrade.

Regression guard for the "well-formed is not valid" defect
(.claude/rules/reclaim-routed-work.md rule 2):

    classify() used to UNCONDITIONALLY early-return category "a" (genuine)
    for any defer_reason starting with a structured prefix, before consulting
    any other signal. Measured on the live queue 2026-07-28: 29 of 40 defers
    (72.5%) took that return, including defers frozen 83 and 95 days. The
    prefix attests that the author FORMATTED the defer, not that the reason
    is still a valid reason to stay stopped.

The fast path for FRESH structured defers is deliberately preserved — this
suite pins both halves so a future edit cannot restore the unconditional
return without going red, and cannot over-correct into flagging every
structured defer either.

Pattern: importlib + sys.path (the script name has hyphens, so it cannot be
a plain `import`) — same shape as test_defer_drift_check.py.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "audit-deferred-defers.py"


def _import():
    sys.path.insert(0, str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("audit_deferred_defers", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["audit_deferred_defers"] = mod
    spec.loader.exec_module(mod)
    return mod


MOD = _import()


def _ago(days: float) -> str:
    """A naive-ISO defer_reason_set_at `days` in the past (store format)."""
    return (dt.datetime.now() - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


STRUCTURED = "precondition_unmet: the shared service reports zero live instances"


# --- the preserved fast path ------------------------------------------------

def test_fresh_structured_defer_stays_genuine():
    """A recently-set structured defer keeps category 'a' (unchanged behavior)."""
    r = MOD.classify(STRUCTURED, ["agent"], defer_set_at=_ago(3))
    assert r["category"] == "a"
    assert any(e.startswith("structured-prefix:") for e in r["evidence"])
    assert not any("stale-structured" in e for e in r["evidence"])


def test_every_genuine_prefix_has_a_fast_path():
    """All three GENUINE_PREFIXES still short-circuit while fresh."""
    for pfx in MOD.GENUINE_PREFIXES:
        r = MOD.classify(f"{pfx} some condition", ["agent"], defer_set_at=_ago(1))
        assert r["category"] == "a", f"{pfx} lost its fresh fast path"


# --- the regression guard ---------------------------------------------------

def test_stale_structured_defer_downgrades_to_b():
    """THE guard: a well-formed but long-frozen defer must surface for re-check.

    Reverting classify() to the unconditional `return {"category": "a"}` turns
    this red — that is the mutation this test exists to catch.
    """
    r = MOD.classify(STRUCTURED, ["agent"], defer_set_at=_ago(40))
    assert r["category"] == "b", "stale structured defer was laundered as genuine"
    assert any("stale-structured" in e for e in r["evidence"])
    assert any(e.startswith("structured-prefix:") for e in r["evidence"]), \
        "the prefix must be preserved as evidence, not discarded"


def test_threshold_is_a_boundary_not_a_cliff():
    """Just under the threshold is genuine; well past it is not."""
    assert MOD.classify(STRUCTURED, ["agent"], defer_set_at=_ago(1),
                        stale_days=10)["category"] == "a"
    assert MOD.classify(STRUCTURED, ["agent"], defer_set_at=_ago(30),
                        stale_days=10)["category"] == "b"


def test_stale_days_is_tunable():
    """The same defer flips category purely on the caller's threshold."""
    aged = _ago(20)
    assert MOD.classify(STRUCTURED, ["agent"], defer_set_at=aged,
                        stale_days=60)["category"] == "a"
    assert MOD.classify(STRUCTURED, ["agent"], defer_set_at=aged,
                        stale_days=5)["category"] == "b"


# --- fail-open on a bad/absent timestamp (guard-142) ------------------------

def test_missing_timestamp_fails_open_to_genuine():
    """No defer_set_at => cannot be proven stale => keep the old verdict."""
    r = MOD.classify(STRUCTURED, ["agent"], defer_set_at=None)
    assert r["category"] == "a"
    assert "age:unknown" in r["evidence"]


def test_unparseable_timestamp_fails_open_to_genuine():
    """An audit heuristic must never manufacture staleness from a parse error."""
    r = MOD.classify(STRUCTURED, ["agent"], defer_set_at="not-a-timestamp")
    assert r["category"] == "a"
    assert "age:unknown" in r["evidence"]


def test_default_call_shape_is_backward_compatible():
    """Two-arg callers (the pre-change signature) must still work."""
    r = MOD.classify(STRUCTURED, ["agent"])
    assert r["category"] == "a"
    assert "age:unknown" in r["evidence"]


# --- unrelated classification paths are untouched ---------------------------

def test_empty_defer_reason_still_unknown():
    assert MOD.classify("", ["agent"], defer_set_at=_ago(99))["category"] == "unknown"


def test_non_structured_defer_ignores_age():
    """Age only gates the structured-prefix branch; other paths are unchanged.

    An old free-text defer that matches nothing still lands in the
    'unmatched: review-by-hand' bucket, not the stale-structured one.
    """
    r = MOD.classify("some free-text reason with no marker", ["agent"],
                     defer_set_at=_ago(99))
    assert not any("stale-structured" in e for e in r["evidence"])


def test_age_days_helper_is_fail_open():
    assert MOD._defer_age_days(None) is None
    assert MOD._defer_age_days("garbage") is None
    assert MOD._defer_age_days(_ago(10)) > 9.0


# ---- load_deferred(): terminal-status goals are not routed-away work --------
#
# A defer on an already-terminal goal is not reclaimable — there is nothing left
# to route. Reporting it anyway is worse than noise: measured on the live queue
# 2026-07-29, lane B reported 3 stale-structured defers of which 2 were `retired`,
# so 67% of the lane's output was permanent residue that reappears identically
# every sweep. That is what trains a reader to stop checking the lane, which is
# how the ONE real item (foxtrot's ) stayed surfaced-and-never-routed.


def _write_world(tmp_path, goals):
    """Build a one-aspiration tmp world and point the module at it."""
    world = tmp_path / "world"
    world.mkdir()
    (world / "aspirations.jsonl").write_text(
        json.dumps({"id": "asp-999", "goals": goals}) + "\n", encoding="utf-8"
    )
    return world


def _load_from(monkeypatch, world):
    monkeypatch.setattr(MOD, "WORLD_DIR", world)
    # Agent queues are a separate source; isolate the world path under test.
    monkeypatch.setattr(MOD, "_enumerate_agents", lambda: [])
    return MOD.load_deferred()


def _goal(gid, status):
    return {
        "id": gid,
        "title": f"goal {gid}",
        "status": status,
        "defer_reason": "precondition_unmet:something",
        "defer_reason_set_at": _ago(90),
        "participants": ["agent"],
    }


def test_terminal_status_defers_are_excluded(tmp_path, monkeypatch):
    """The mutation target: drop the filter and these four come back."""
    world = _write_world(tmp_path, [
        _goal("g-1-1", "pending"),
        _goal("g-1-2", "retired"),      # both live phantoms carried this
        _goal("g-1-3", "completed"),
        _goal("g-1-4", "skipped"),
        _goal("g-1-5", "expired"),
    ])
    ids = {r["goal_id"] for r in _load_from(monkeypatch, world)}
    assert ids == {"g-1-1"}, f"terminal-status defers leaked: {ids - {'g-1-1'}}"


def test_non_terminal_defers_all_survive(tmp_path, monkeypatch):
    """SPECIFICITY control — the filter must not over-reach.

    Stays GREEN under the mutation above, so a file-level pass cannot be
    mistaken for this case discriminating anything (guard-1660).
    """
    world = _write_world(tmp_path, [
        _goal("g-2-1", "pending"),
        _goal("g-2-2", "in-progress"),
        _goal("g-2-3", "blocked"),
    ])
    ids = {r["goal_id"] for r in _load_from(monkeypatch, world)}
    assert ids == {"g-2-1", "g-2-2", "g-2-3"}


def test_missing_status_is_kept_not_dropped(tmp_path, monkeypatch):
    """Absent status is unknown, not terminal — dropping it would hide real work."""
    g = _goal("g-3-1", "pending")
    del g["status"]
    ids = {r["goal_id"] for r in _load_from(monkeypatch, _write_world(tmp_path, [g]))}
    assert ids == {"g-3-1"}


def test_terminal_match_tolerates_case_and_whitespace(tmp_path, monkeypatch):
    world = _write_world(tmp_path, [
        _goal("g-4-1", "  Completed "),
        _goal("g-4-2", "RETIRED"),
        _goal("g-4-3", "pending"),
    ])
    ids = {r["goal_id"] for r in _load_from(monkeypatch, world)}
    assert ids == {"g-4-3"}
