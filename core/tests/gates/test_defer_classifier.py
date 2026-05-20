"""Behavior tests for gates.defer_classifier (PR 7e/1).

Pure predicate: is_narrative_defer(field, value) -> bool. No I/O, no env
reads. Tests cover the three exclusion classes (non-defer field, clear,
structured prefix) and the narrative-positive case.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "core" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from gates.defer_classifier import is_narrative_defer, STRUCTURED_DEFER_PREFIXES


# --- Non-defer fields never qualify --------------------------------------

def test_non_defer_field_returns_false():
    assert is_narrative_defer("status", "any value") is False
    assert is_narrative_defer("title", "Deferred work") is False
    assert is_narrative_defer("description", "blocked on user") is False


# --- Clears never qualify (unblock paths must not need overrides) --------

def test_none_value_returns_false():
    assert is_narrative_defer("defer_reason", None) is False


def test_empty_string_returns_false():
    assert is_narrative_defer("defer_reason", "") is False


# --- Structured prefixes bypass (machine-written internal markers) -------

def test_precondition_unmet_prefix_bypasses():
    assert is_narrative_defer(
        "defer_reason", "precondition_unmet: g-001-01 status != pending") is False


def test_blocked_on_dependency_bypasses():
    assert is_narrative_defer(
        "defer_reason", "blocked_on_dependency g-001-02") is False


def test_circuit_breaker_prefix_bypasses():
    assert is_narrative_defer(
        "defer_reason", "Circuit breaker: 3 consecutive failures") is False


# --- Case-insensitive matching (LLM drift protection, rb-246) ------------

def test_circuit_breaker_lowercase_bypasses():
    assert is_narrative_defer(
        "defer_reason", "circuit breaker: foo") is False


def test_circuit_breaker_titlecase_bypasses():
    """Each prefix must match regardless of LLM casing drift."""
    assert is_narrative_defer(
        "defer_reason", "Circuit Breaker: foo") is False


def test_precondition_unmet_titlecase_bypasses():
    assert is_narrative_defer(
        "defer_reason", "Precondition_unmet: g-001-01") is False


# --- Narrative defers qualify (the gate-firing case) ---------------------

def test_narrative_blocked_on_user_qualifies():
    assert is_narrative_defer(
        "defer_reason", "blocked on user-initiated commit") is True


def test_narrative_awaiting_signal_qualifies():
    assert is_narrative_defer(
        "defer_reason", "awaiting user feedback on the proposal") is True


def test_non_string_value_qualifies_if_truthy():
    """str(value) is used internally, so truthy non-strings flow through.
    Realistic only if a caller mistakenly sends a number/list; test exists
    to pin behavior so future refactors don't silently flip it."""
    assert is_narrative_defer("defer_reason", 42) is True


# --- Single-source-of-truth: the prefix list is published --------------

def test_structured_prefixes_published():
    """The canonical-case tuple is the public contract. If the framework
    changes its prefix names, the version test below catches the change
    so docs (goal-schemas.md, probe-before-defer.md) can be updated."""
    assert STRUCTURED_DEFER_PREFIXES == (
        "precondition_unmet:",
        "blocked_on_dependency",
        "Circuit breaker:",
    )
