"""test_quiescence_drainable.py — B6.8 drainable-evidence regression ().

Exercises the drainable-evidence helpers added to quiescence-gate.py for the
B6.8 symmetric quiescence-approved-with-debt drain branch:
  - _drainable_summary: priority order decompose > hypothesis > finding
  - _collect_drainable_evidence: 3-field shape
  - _count_* helpers: parse JSON-array / JSONL bodies, fail-open to 0 on _rt error

Pattern: importlib-load the gate module, monkey-patch the module-level _rt
(daemon client). No real daemon, no file I/O. Uses real asserts so pytest
genuinely validates — a bool-returning test passes vacuously under pytest, so
the loop_state-style return-bool pattern is intentionally NOT used here.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

GATE_PATH = CORE_SCRIPTS / "quiescence-gate.py"
spec = importlib.util.spec_from_file_location("quiescence_gate", GATE_PATH)
qg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qg)


class _FakeRt:
    """Stub for the _rt daemon client. `routes` maps a path-substring to a
    response str; a callable response is invoked (so a test can simulate the
    daemon raising)."""

    def __init__(self, routes):
        self._routes = routes

    def rt_call(self, method, path, query=None, body=None, headers=None):
        for needle, resp in self._routes.items():
            if needle in path:
                return resp() if callable(resp) else resp
        return ""


def _install_rt(routes):
    qg._rt = _FakeRt(routes)


def _install_decompose(raw):
    """Stub the decompose-candidate subprocess fetch (NOT daemon-served — it
    runs tree.py via sys.executable; tests mock the raw-fetch, not _rt). A
    callable raw simulates the subprocess raising."""
    qg._read_decompose_candidates_raw = (raw if callable(raw) else (lambda: raw))


# ---- _drainable_summary priority order ----

def test_summary_priority_decompose_wins():
    ev = {"tree_decompose_candidates": 47, "unreflected_hypotheses": 3,
          "actionable_findings_without_goal": 1}
    s = qg._drainable_summary(ev)
    assert s["primary_target"] == "decompose"
    assert s["primary_target_count"] == 47
    assert s["any_target_available"] is True


def test_summary_priority_hypothesis_when_no_decompose():
    # hypothesis is next after decompose.
    ev = {"tree_decompose_candidates": 0, "unreflected_hypotheses": 2,
          "actionable_findings_without_goal": 9}
    s = qg._drainable_summary(ev)
    assert s["primary_target"] == "hypothesis"
    assert s["primary_target_count"] == 2
    assert s["any_target_available"] is True


def test_summary_priority_finding_last():
    ev = {"tree_decompose_candidates": 0, "unreflected_hypotheses": 0,
          "actionable_findings_without_goal": 5}
    s = qg._drainable_summary(ev)
    assert s["primary_target"] == "finding"
    assert s["primary_target_count"] == 5


def test_summary_none_available():
    ev = {"tree_decompose_candidates": 0, "unreflected_hypotheses": 0,
          "actionable_findings_without_goal": 0}
    s = qg._drainable_summary(ev)
    assert s["primary_target"] is None
    assert s["primary_target_count"] == 0
    assert s["any_target_available"] is False


# ---- _collect_drainable_evidence shape ----

def test_collect_evidence_shape():
    _install_decompose('[{"key":"a"},{"key":"b"}]')
    _install_rt({
        "/v1/pipeline/read": "[]",
        "/v1/board/read": "",
    })
    ev = qg._collect_drainable_evidence({})
    assert set(ev.keys()) == {
        "tree_decompose_candidates", "unreflected_hypotheses",
        "actionable_findings_without_goal",
    }
    assert ev["tree_decompose_candidates"] == 2
    assert ev["unreflected_hypotheses"] == 0


# ---- count helpers: parse + fail-open ----

def test_count_decompose_array():
    _install_decompose('[{"key":"a"},{"key":"b"},{"key":"c"}]')
    assert qg._count_tree_decompose_candidates() == 3


def test_count_decompose_empty():
    _install_decompose("[]")
    assert qg._count_tree_decompose_candidates() == 0


def test_count_decompose_failopen_on_subprocess_error():
    def _boom():
        raise RuntimeError("tree.py rc=1")
    _install_decompose(_boom)
    assert qg._count_tree_decompose_candidates() == 0  # fail-open, never raises


def test_count_unreflected_failopen_on_daemon_error():
    def _boom():
        raise RuntimeError("daemon unreachable")
    _install_rt({"/v1/pipeline/read": _boom})
    assert qg._count_unreflected_hypotheses() == 0  # fail-open, never raises


def test_count_findings_filter_jsonl():
    # 4 posts: m1 counts (actionable, no goal tag, other author);
    # m2 has a goal_id tag (skip); m3 authored by self (skip);
    # m4 not actionable (skip).
    prior = os.environ.get("MIND_AGENT")
    os.environ["MIND_AGENT"] = "alpha"
    try:
        lines = "\n".join([
            '{"id":"m1","author":"zeta","tags":["actionable"]}',
            '{"id":"m2","author":"zeta","tags":["actionable","goal_id:g-1"]}',
            '{"id":"m3","author":"alpha","tags":["actionable"]}',
            '{"id":"m4","author":"bravo","tags":["finding"]}',
        ])
        _install_rt({"/v1/board/read": lines})
        assert qg._count_actionable_findings_without_goal() == 1
    finally:
        if prior is None:
            os.environ.pop("MIND_AGENT", None)
        else:
            os.environ["MIND_AGENT"] = prior


def test_count_findings_array_form():
    prior = os.environ.get("MIND_AGENT")
    os.environ["MIND_AGENT"] = "alpha"
    try:
        arr = ('[{"id":"m1","author":"zeta","tags":["actionable"]},'
               '{"id":"m2","author":"bravo","tags":["actionable"]}]')
        _install_rt({"/v1/board/read": arr})
        assert qg._count_actionable_findings_without_goal() == 2
    finally:
        if prior is None:
            os.environ.pop("MIND_AGENT", None)
        else:
            os.environ["MIND_AGENT"] = prior


def main() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failures = []
    for t in tests:
        try:
            t()
            print(f"  [PASS] {t.__name__}")
        except Exception as e:  # noqa: BLE001 — aggregator reports, doesn't raise
            failures.append(f"[FAIL] {t.__name__}: {type(e).__name__}: {e}")
    print()
    if failures:
        for f in failures:
            print(f)
        print(f"\n{len(failures)}/{len(tests)} test(s) failed")
        return 1
    print(f"All {len(tests)} drainable-evidence cases verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
