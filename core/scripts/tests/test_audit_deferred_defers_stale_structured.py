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
