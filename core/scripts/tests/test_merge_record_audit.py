"""test_merge_record_audit.py — gap-042 / g-001-789.

ANTI-VACUITY IS THE POINT OF THIS MODULE. The audit acquired EIGHT filters during
its forge, each one removing a class of false alarm that a real replay produced:
companion-archive moves, displaced_from aliases, terminal-at-pre-state, canonical
row form, precondition_unmet auto-clears, closed-since-pre-state, and supersession
via origin_signal. A filter chain tuned until everything passes is worthless — and
the acceptance run does now report 0 LOSS across 83 store files over a five-day
window, which is exactly what a broken audit would also report.

So every filter test here is PAIRED: one case the filter must swallow, and one it
must NOT. A filter that passes only the first half is indistinguishable from
`return CLEAN`.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mra = _load("_merge_record_audit", "merge_record_audit.py")


# ── the alarm path must exist at all ──────────────────────────────────────

def test_a_non_terminal_absent_record_is_an_alarm():
    pre = {"g-1": {"status": "pending", "priority": "HIGH", "title": "t"}}
    expected, alarm = mra._classify_absent(pre, ["g-1"])
    assert expected == []
    assert len(alarm) == 1 and alarm[0]["id"] == "g-1"


def test_a_terminal_absent_record_is_expected_not_an_alarm():
    """The other half: 71 of 72 absent ids on the real replay were terminal."""
    pre = {"g-2": {"status": "completed", "title": "t"}}
    expected, alarm = mra._classify_absent(pre, ["g-2"])
    assert len(expected) == 1 and alarm == []


# ── filter 5: precondition_unmet auto-clears, human_blocked does not ──────

def test_dropped_human_blocked_defer_is_a_regression():
    pre = {"g-3": {"status": "pending", "defer_reason": "human_blocked: waiting on a person"}}
    post = {"g-3": {"status": "pending"}}
    regs = mra._regressions(pre, post)
    assert [r["kind"] for r in regs] == ["dropped_field"]


def test_dropped_precondition_defer_is_not_a_regression():
    """probe-before-defer rule 4 re-probes and clears these every iteration."""
    pre = {"g-4": {"status": "pending", "defer_reason": "precondition_unmet: window not full"}}
    post = {"g-4": {"status": "pending"}}
    assert mra._regressions(pre, post) == []


def test_a_goal_that_closed_since_the_pre_state_sheds_its_defer_without_alarm():
    pre = {"g-5": {"status": "pending", "defer_reason": "human_blocked: waiting"}}
    post = {"g-5": {"status": "completed"}}
    assert mra._regressions(pre, post) == []


def test_backward_lifecycle_is_still_caught_after_all_the_softening():
    """guard-424's measured damage: completed -> pending with the metadata stripped."""
    pre = {"g-6": {"status": "completed", "completed_by": "omni"}}
    post = {"g-6": {"status": "pending"}}
    kinds = sorted(r["kind"] for r in mra._regressions(pre, post))
    assert kinds == ["backward_lifecycle", "dropped_field"]


# ── filter 8: supersession via origin_signal, matched on the FIELD only ───

def test_origin_signal_supersession_is_recognised():
    idx = {"g-014-97": {"origin_signal": "residual:g-014-84"}}
    assert "g-014-84" in mra._superseded_ids(idx)


def test_an_id_in_PROSE_does_not_count_as_supersession():
    """guard-1017: ids appear as references inside descriptions and outcome_notes.
    A substring scan over record text would mark almost anything superseded, which
    would silently disable the whole audit."""
    idx = {"g-9": {"description": "supersedes g-014-84 eventually",
                   "outcome_note": "see g-014-84",
                   "origin_signal": "idea:something-else"}}
    assert "g-014-84" not in mra._superseded_ids(idx)


# ── filter 4/6: content identity, not byte identity ───────────────────────

def test_reserialized_rows_are_the_same_row():
    """26 'lost' meta-log rows on the real replay were key-reordered re-emissions."""
    a = '{"b": 2, "a": 1}'
    b = '{"a":1,"b":2}'
    assert mra._canon_row(a) == mra._canon_row(b)


def test_an_unparseable_row_falls_back_to_its_raw_text():
    """Conservative direction: it can over-report, never hide."""
    assert mra._canon_row("  not json  ") == "not json"


def test_a_genuinely_different_row_stays_different():
    assert mra._canon_row('{"a":1}') != mra._canon_row('{"a":2}')


# ── nested-goal indexing (guard-1017's structural half) ───────────────────

def test_aspirations_are_indexed_by_GOAL_not_by_line():
    """aspirations.jsonl is one line per ASPIRATION; the unit a line-wise merge
    reverts is the nested GOAL, so that is what must be indexed."""
    recs = [{"id": "asp-1", "goals": [{"id": "g-a"}, {"id": "g-b"}]},
            {"id": "asp-2", "goals": [{"id": "g-c"}]}]
    idx = mra._goal_index(recs)
    assert set(idx) == {"g-a", "g-b", "g-c"}


# ── the store list is discovered, never hardcoded ─────────────────────────

def test_store_list_comes_from_the_live_merge_handler_registry():
    h = mra._load_handlers()
    assert len(h) > 50, "registry looks truncated"
    assert "aspirations.jsonl" in h and "pipeline.jsonl" in h
    assert getattr(h["aspirations.jsonl"], "__name__", "") == "merge_aspirations"


def test_exit_codes_are_distinguishable():
    codes = [mra.RC_OK, mra.RC_LOSS, mra.RC_USAGE]
    assert len(set(codes)) == len(codes)


# ── fresh-eyes findings on this same module (F1, F2) ──────────────────────

def test_an_internal_fault_does_not_report_as_LOSS():
    """CPython exits 1 on an uncaught exception — which IS RC_LOSS. For a script
    whose exit code is meant to gate a post-merge path, a registry import failure
    or a missing git must not read as data loss (that is the fail-CLOSED direction
    iteration-push's own comments warn freezes framework sync for the box)."""
    import unittest.mock as mock
    with mock.patch.object(mra, "_load_handlers", side_effect=RuntimeError("boom")):
        rc = mra.main(["HEAD"])
    assert rc == mra.RC_INTERNAL
    assert mra.RC_INTERNAL != mra.RC_LOSS


def test_a_real_loss_still_reports_LOSS_after_that_change():
    """Anti-vacuity for the test above: catching everything must not catch the
    verdict itself."""
    pre = {"g-x": {"status": "pending", "priority": "HIGH", "title": "t"}}
    _, alarm = mra._classify_absent(pre, ["g-x"])
    assert len(alarm) == 1


def test_backward_lifecycle_is_caught_on_stage_keyed_stores_too():
    """pipeline records key on `stage` and carry NO `status`. A hardcoded
    .get("status") made this check silently inert on a store guard-424 names
    explicitly."""
    pre = {"h1": {"stage": "archived"}}
    post = {"h1": {"stage": "active"}}
    regs = mra._regressions(pre, post)
    assert [r["kind"] for r in regs] == ["backward_lifecycle"]
    assert regs[0]["field"] == "stage"


def test_a_forward_stage_move_is_not_a_regression():
    """The pair: the stage-aware check must not fire on normal progression."""
    assert mra._regressions({"h2": {"stage": "active"}},
                            {"h2": {"stage": "archived"}}) == []


def test_absent_classification_reads_stage_when_there_is_no_status():
    pre = {"h3": {"stage": "archived"}, "h4": {"stage": "active"}}
    expected, alarm = mra._classify_absent(pre, ["h3", "h4"])
    assert [e["id"] for e in expected] == ["h3"]
    assert [a["id"] for a in alarm] == ["h4"]
