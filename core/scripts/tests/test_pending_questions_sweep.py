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


# --- Case 8-17: the cleanup_only verdict SPLIT ( / ) ---
#
# `cleanup_only` was ONE verdict covering TWO heuristics with OPPOSITE meanings:
# _h_already_terminal (conf 1.00, INERT — nothing to do, persists by design) and
# _h_answered_not_cleaned (conf 0.95, ACTIONABLE). Because the inert half can
# never reach zero, the combined count was a steady state that always read as
# debt, and `cleanup_available` fired permanently. Measured on cc-07 2026-08-10:
# 38 cleanup_only = 24 actionable + 14 inert.
#
# NOTE the verdict had ZERO test coverage before this block — which is part of
# why it survived eight separate filings by five agents.

def _sweep(tmp_path, entries, **flags):
    """Run the real cmd_sweep over a fixture file and return its result dict."""
    import yaml

    mod = _import_sweep()
    p = tmp_path / "pending-questions.yaml"
    p.write_text(yaml.safe_dump(entries, sort_keys=False), encoding="utf-8")

    class _Args:
        pq_path = str(p)
        apply = flags.get("apply", False)
        apply_cleanup = flags.get("apply_cleanup", False)

    return mod.cmd_sweep(_Args()), p


_INERT = {"id": "pq-inert", "question": "q", "status": "resolved",
          "resolution": "done long ago"}
_ACTIONABLE = {"id": "pq-act", "question": "q", "status": "answered",
               "answer": "the user's real answer"}


def test_inert_and_actionable_get_DIFFERENT_verdicts(tmp_path):
    res, _ = _sweep(tmp_path, [_INERT, _ACTIONABLE])
    by_id = {e["id"]: e["verdict"] for e in res["entries"]}
    assert by_id["pq-inert"] == "already_terminal"
    assert by_id["pq-act"] == "needs_transition"
    # the whole point: they are no longer the same string
    assert by_id["pq-inert"] != by_id["pq-act"]


def test_cleanup_available_does_NOT_fire_on_inert_alone(tmp_path):
    """THE defect. Under the old code this fired forever."""
    res, _ = _sweep(tmp_path, [_INERT, dict(_INERT, id="pq-inert-2")])
    assert res["counts"]["already_terminal"] == 2
    assert res["counts"]["needs_transition"] == 0
    assert "cleanup_available" not in res["flags"]


def test_cleanup_available_DOES_fire_when_work_is_outstanding(tmp_path):
    """Two-way control — without this the test above passes on a dead flag."""
    res, _ = _sweep(tmp_path, [_INERT, _ACTIONABLE])
    assert res["counts"]["needs_transition"] == 1
    assert "cleanup_available" in res["flags"]


def test_flag_clears_once_the_actionable_entry_is_discharged(tmp_path):
    """End-to-end: the count REACHES ZERO, which it previously could not."""
    res, p = _sweep(tmp_path, [_INERT, _ACTIONABLE], apply_cleanup=True)
    assert res["applied"] == 1
    res2, _ = _sweep(tmp_path, __import__("yaml").safe_load(p.read_text()))
    # the discharged entry re-classifies as inert — omni's measured prediction
    assert res2["counts"]["already_terminal"] == 2
    assert res2["counts"]["needs_transition"] == 0
    assert "cleanup_available" not in res2["flags"]


def test_plain_apply_does_NOT_discharge_needs_transition(tmp_path):
    """--apply runs unattended from precheck; these carry a real user answer."""
    import yaml

    res, p = _sweep(tmp_path, [_INERT, _ACTIONABLE], apply=True)
    assert res["applied"] == 0
    raw = yaml.safe_load(p.read_text())
    assert next(e for e in raw if e["id"] == "pq-act")["status"] == "answered"


def test_apply_cleanup_preserves_the_user_answer_verbatim(tmp_path):
    """The recorded objection to building this writer was that it would
    overwrite a user's answer. Measured: it writes status/resolved_at/resolution
    and never touches `answer`."""
    import yaml

    _sweep(tmp_path, [_ACTIONABLE], apply_cleanup=True)
    raw = yaml.safe_load((tmp_path / "pending-questions.yaml").read_text())
    e = next(x for x in raw if x["id"] == "pq-act")
    assert e["status"] == "resolved"
    assert e["answer"] == "the user's real answer"
    assert e.get("resolution")


def test_apply_cleanup_never_touches_already_terminal(tmp_path):
    """The inert class is inert under BOTH flags."""
    res, _ = _sweep(tmp_path, [_INERT, dict(_INERT, id="pq-inert-2")],
                    apply=True, apply_cleanup=True)
    assert res["applied"] == 0


def test_cleanup_only_survives_in_counts_as_the_SUM(tmp_path):
    """Back-compat: an older consumer reading counts['cleanup_only'] sees the
    same number it always did rather than a KeyError."""
    res, _ = _sweep(tmp_path, [_INERT, _ACTIONABLE, dict(_ACTIONABLE, id="pq-act-2")])
    c = res["counts"]
    assert c["cleanup_only"] == c["already_terminal"] + c["needs_transition"] == 3


def test_split_is_a_PARTITION_not_a_reclassification(tmp_path):
    """Every entry that used to be cleanup_only lands in exactly one half —
    none escapes to no_action and none is double-counted."""
    entries = [_INERT, dict(_INERT, id="i2"), _ACTIONABLE, dict(_ACTIONABLE, id="a2")]
    res, _ = _sweep(tmp_path, entries)
    c = res["counts"]
    assert c["already_terminal"] == 2 and c["needs_transition"] == 2
    assert c["no_action"] == 0
    assert c["cleanup_only"] == 4 == c["total"]


def test_summary_reports_the_two_halves_separately(tmp_path):
    """Reporting their sum is the defect; the summary line must not re-merge."""
    res, _ = _sweep(tmp_path, [_INERT, _ACTIONABLE])
    assert "1 needs-transition" in res["summary"]
    assert "1 already-terminal (inert)" in res["summary"]


def test_inert_half_is_protected_by_TWO_independent_mechanisms(tmp_path):
    """Pins WHY a mutation that adds already_terminal to the apply id-set does
    not change behaviour — measured, not assumed.

    Discovered by mutation M5 during g-115-3753: widening the `--apply-cleanup`
    id-set to include `already_terminal` killed ZERO tests. That is not a gap in
    the outcome test above; it is a genuine double defence, and the two halves
    are logically coupled:

      1. `_h_already_terminal` can only fire when `status in TERMINAL_STATUSES`.
      2. `_apply_auto_resolve` skips any entry whose `status in TERMINAL_STATUSES`.

    So an `already_terminal` entry is unreachable by the writer BY CONSTRUCTION,
    whatever the verdict filter says. That is worth pinning rather than leaving
    as a coincidence: widening either predicate without the other would expose
    the already-done half to the writer, which is exactly the error the whole
    eight-goal cluster warned an applier would make.
    """
    mod = _import_sweep()
    terminal = mod.TERMINAL_STATUSES
    assert terminal, "TERMINAL_STATUSES must be non-empty or both defences vanish"

    # (1) every status that can yield `already_terminal` is a terminal status
    now = datetime.now()
    for status in terminal:
        got = mod._h_already_terminal(
            {"status": status, "resolution": "r"}, now, {})
        assert got is not None and got[0] == "already_terminal", status
    # and a NON-terminal status never yields it
    assert mod._h_already_terminal(
        {"status": "answered", "resolution": "r"}, now, {}) is None

    # (2) the writer refuses that same status set, even when handed the ids
    import yaml
    p = tmp_path / "pending-questions.yaml"
    rows = [{"id": f"pq-{s}", "question": "q", "status": s, "resolution": "r"}
            for s in sorted(terminal)]
    p.write_text(yaml.safe_dump(rows, sort_keys=False), encoding="utf-8")
    applied = mod._apply_auto_resolve(
        p, {r["id"] for r in rows}, {r["id"]: {"reason": "x"} for r in rows})
    assert applied == 0, "writer must refuse terminal-status entries outright"
