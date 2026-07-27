"""test_pending_questions_sweep.py — regression test for .

Asserts that pending-questions-sweep.py's `_h_source_goal_completed` heuristic
EXEMPTS self.md Decision-Authority decision-logs from source-goal-completion
auto-resolve, while non-decision-log pending-questions still auto-resolve when
their source goal completes (regression guard).

Background (g-115-1369): decision-logs are the self.md retroactive-review
mechanism — filed AT goal completion with the decision already executed
(default_action prefixed "Already executed:"), MEANT to outlive the source goal
so the user can review and override. The pre-fix heuristic auto-resolved
pq-ollama-numparallel-2026-06-08 at 0.95 confidence the same iteration its
source goal g-321-06 completed, silently defeating the oversight.

Cases covered:
  1. _is_decision_log recognizes the "Already executed:" marker (case/space tolerant)
  2. _is_decision_log recognizes "*-decision" / explicit decision-log types
  3. _is_decision_log rejects non-decision-log entries
  4. Canonical incident shape (marker + completed source goal) → exempt (None)
  5. Type-only decision-log + completed source goal → exempt (None)
  6. Non-decision-log + completed source goal → still auto_resolve (regression guard)
  7. Full chain (_evaluate) on a fresh decision-log with completed source goal
     → verdict is NOT auto_resolve

Pattern: same importlib + sys.path shape as test_parent_supersession_sweep.py /
test_defer_recheck_patterns.py. pending-questions-sweep.py uses a hyphenated
filename so we load it via spec_from_file_location with a hyphen-free name.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))


def _import_sweep():
    """Load pending-questions-sweep.py via importlib."""
    spec = importlib.util.spec_from_file_location(
        "pending_questions_sweep_mod",
        CORE_SCRIPTS / "pending-questions-sweep.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load spec for pending-questions-sweep.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- Case 1-3: _is_decision_log predicate --------------------------------

def test_is_decision_log_marker_prefix():
    mod = _import_sweep()
    assert mod._is_decision_log(
        {"default_action": "Already executed: tested NUM_PARALLEL=2, reverted"}
    )
    # case-insensitive + leading-whitespace tolerant
    assert mod._is_decision_log({"default_action": "  already executed: foo"})


def test_is_decision_log_type():
    mod = _import_sweep()
    # observed real-data types both end "-decision"
    assert mod._is_decision_log({"type": "infrastructure-decision"})
    assert mod._is_decision_log({"type": "architecture-decision"})
    # explicit set members
    assert mod._is_decision_log({"type": "decision-log"})
    assert mod._is_decision_log({"type": "decision"})


def test_is_decision_log_negatives():
    mod = _import_sweep()
    assert not mod._is_decision_log({"default_action": "no change", "type": "infrastructure"})
    assert not mod._is_decision_log({})
    assert not mod._is_decision_log({"default_action": "kill the orphan process", "type": "general"})
    # "decision" must be a type-token boundary, not a substring of unrelated prose
    assert not mod._is_decision_log({"default_action": "make a decision later", "type": "general"})


# --- Case 4-6: _h_source_goal_completed exemption + regression guard ------

def test_canonical_incident_exempt_via_marker():
    """pq-ollama-numparallel-2026-06-08 shape: marker + completed source goal → exempt."""
    mod = _import_sweep()
    entry = {
        "id": "pq-ollama-numparallel-2026-06-08",
        "status": "pending",
        "source_goal": "g-321-06",
        "default_action": "Already executed: tested NUM_PARALLEL=2, found no benefit, reverted",
        "type": "infrastructure-decision",
    }
    ctx = {"completed_goal_ids": {"g-321-06"}}
    # EXEMPT — must NOT auto_resolve on source-goal completion
    assert mod._h_source_goal_completed(entry, datetime.now(), ctx) is None


def test_decision_log_type_only_exempt():
    """Type-only decision-log (default_action lacks the marker) is still exempt."""
    mod = _import_sweep()
    entry = {
        "id": "pq-arch",
        "status": "pending",
        "source_goal": "g-1",
        "type": "architecture-decision",
        "default_action": "chose option C — committed e023e99",
    }
    ctx = {"completed_goal_ids": {"g-1"}}
    assert mod._h_source_goal_completed(entry, datetime.now(), ctx) is None


def test_non_decision_log_still_auto_resolves():
    """Regression guard: a BLOCKING pending-question still auto-resolves when its
    source goal completes (the heuristic's legitimate original behavior)."""
    mod = _import_sweep()
    entry = {
        "id": "pq-blocking",
        "status": "pending",
        "source_goal": "g-2",
        "default_action": "kill the orphan process",
        "type": "general",
    }
    ctx = {"completed_goal_ids": {"g-2"}}
    result = mod._h_source_goal_completed(entry, datetime.now(), ctx)
    assert result is not None
    assert result[0] == "auto_resolve"


# --- Case 7: full-chain end-to-end ---------------------------------------

def test_decision_log_full_chain_not_auto_resolve():
    """A fresh decision-log whose source goal completed must NOT land
    auto_resolve through the full HEURISTIC_CHAIN (it should be no_action while
    fresh, reaching flag_for_review only after the 30d staleness threshold)."""
    mod = _import_sweep()
    now = datetime.now()
    entry = {
        "id": "pq-decision-fresh",
        "status": "pending",
        "source_goal": "g-3",
        "default_action": "Already executed: chose classpath transport, committed",
        "type": "architecture-decision",
        "created": now.strftime("%Y-%m-%dT%H:%M:%S"),  # fresh
    }
    ctx = {"all_entries": [entry], "completed_goal_ids": {"g-3"}}
    verdict = mod._evaluate(entry, now, ctx)
    assert verdict["verdict"] != "auto_resolve"
