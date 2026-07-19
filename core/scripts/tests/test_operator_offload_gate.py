"""test_operator_offload_gate.py -- unit coverage for the operator-offload
gate (gates/operator_offload.py), the Layer-B backstop for gh-005.

Contract under test: evaluate() is a pure decision function.
  - Non-recurring goals NEVER fire (would_block=False, fired=False) -- the
    no-op hot path for every ordinary goal add.
  - Recurring goals (recurring:true OR interval_hours present) BLOCK unless
    they carry a non-empty `offload_decision` string or an override reason.
  - Whitespace-only / non-string offload_decision does NOT satisfy the gate.
  - Non-dict input is safe (no exception, no block).

Pattern: direct evaluate() unit tests; _gate_log is monkeypatched to a no-op
so telemetry is not written to the real meta/gate-firings.jsonl during the
run (same pattern as test_stale_read_gate.py).
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

from gates import operator_offload  # noqa: E402

# Neutralize telemetry (module-global reassignment; evaluate() resolves
# _gate_log from module namespace at call time).
operator_offload._gate_log = lambda *a, **k: None  # noqa: E305

evaluate = operator_offload.evaluate


# ─── Non-recurring goals: pure no-op ─────────────────────────────────────

def test_plain_goal_never_fires():
    r = evaluate({"title": "Idea: one-shot cleanup", "priority": "MEDIUM"})
    assert r["would_block"] is False
    assert r["fired"] is False
    assert r["reason"] == "not-recurring"


def test_recurring_false_never_fires():
    r = evaluate({"title": "x", "recurring": False})
    assert r["would_block"] is False
    assert r["fired"] is False


def test_recurring_truthy_but_not_true_never_fires():
    # Only the boolean True counts as recurring; string "true" is a schema
    # error the goal validator owns, not this gate.
    r = evaluate({"title": "x", "recurring": "true"})
    assert r["fired"] is False


# ─── Recurring goals without a decision: BLOCK ───────────────────────────

def test_recurring_true_bare_blocks():
    r = evaluate({"title": "Recurring: sweep inbox", "recurring": True,
                  "interval_hours": 6})
    assert r["would_block"] is True
    assert r["fired"] is True
    assert "offload_decision" in r["reason"]  # educational block message
    assert "gh-005" in r["reason"]


def test_interval_hours_alone_triggers():
    # interval_hours without recurring:true still marks a standing claim on
    # LLM iterations -- the gate keys on either signal.
    r = evaluate({"title": "x", "interval_hours": 24})
    assert r["would_block"] is True
    assert r["fired"] is True


def test_empty_string_decision_blocks():
    r = evaluate({"title": "x", "recurring": True, "offload_decision": ""})
    assert r["would_block"] is True


def test_whitespace_decision_blocks():
    r = evaluate({"title": "x", "recurring": True, "offload_decision": "   "})
    assert r["would_block"] is True


def test_non_string_decision_blocks():
    r = evaluate({"title": "x", "recurring": True,
                  "offload_decision": {"reason": "nested"}})
    assert r["would_block"] is True


# ─── Recurring goals with a decision: PASS ───────────────────────────────

def test_stays_mind_decision_passes():
    r = evaluate({"title": "Recurring: tree curation", "recurring": True,
                  "interval_hours": 168,
                  "offload_decision": "stays-mind: judgement/retrieval-heavy (tree curation)"})
    assert r["would_block"] is False
    assert r["fired"] is True
    assert r["reason"] == "offload-decision-recorded"
    assert r["offload_decision"].startswith("stays-mind")


def test_operator_pull_decision_passes():
    r = evaluate({"title": "Pull: inbox verdicts", "recurring": True,
                  "interval_hours": 24,
                  "offload_decision": "operator-pull: reads InboxWatch audit rows"})
    assert r["would_block"] is False
    assert r["fired"] is True


# ─── Override ────────────────────────────────────────────────────────────

def test_override_passes_without_decision():
    r = evaluate({"title": "x", "recurring": True},
                 override_offload="migration in flight, decision tracked in g-115-2076")
    assert r["would_block"] is False
    assert r["fired"] is True
    assert r["reason"] == "override"
    assert r["override"].startswith("migration in flight")


def test_override_ignored_for_non_recurring():
    # Override on a non-recurring goal is inert -- gate never fires.
    r = evaluate({"title": "x"}, override_offload="whatever")
    assert r["fired"] is False


# ─── Input safety ────────────────────────────────────────────────────────

def test_non_dict_input_is_safe():
    for bad in (None, "goal", 42, ["recurring"], True):
        r = evaluate(bad)  # type: ignore[arg-type]
        assert r["would_block"] is False
        assert r["fired"] is False


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
