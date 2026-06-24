"""Pre-Phase-2 merge-correctness specs for generalize-down (body-merge.py).

These tests PIN two findings from the independent code review of Phase 1C
(g-306-63), posted to the findings board (msg-20260624-063946-alpha-2455) and
tracked by the Phase 2 QA gate g-306-67. Both are DORMANT today (generalize_down
is a no-op in single-runner) but MUST be fixed before Phase 2 worker bodies
(g-306-65) rely on the merge.

They are marked xfail because the current 2-way merge is INCORRECT once a Body
has actually forked from the reducer (so the Body's WM INCLUDES the shared
fork-baseline). The fix is a true 3-way merge: preserve the fork-baseline CONTENT
at FORK-BODY time (the manifest stores only `forked_wm_hash` today, not content),
then at merge compute each Body's delta against its baseline and apply ONLY the
delta. When the 3-way fix lands these xfails flip to xpass -> delete the xfail
markers and they become permanent regression guards.

  F1 (HIGH): numeric counters SUM, double-counting the inherited baseline.
  F3 (LOW) : array union resurrects items the reducer deleted post-fork.

DISTINCT from test_body_merge.py, whose counter tests model reducer/body as
INDEPENDENT values (e.g. 2 + 5 == 7) and so never exercise the shared-baseline
double-count. These tests use the REAL fork path (body_manifest.write_manifest)
so the baseline relationship is authentic.

Daemon-safe: hermetic tmp project_root, no daemon_integration marker, passes
project_root explicitly, never touches env-routed wm.py I/O.

Run: python -m pytest core/scripts/tests/test_body_merge_3way_baseline.py -q -rxX
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

CORE_SCRIPTS = Path(__file__).resolve().parent.parent  # core/scripts/

# Reuse the proven-valid SID shapes from test_body_merge.py (pass _valid_sid_shape).
SID_REDUCER = "11111111-1111-4111-8111-111111111111"
SID_WORKER = "22222222-2222-4222-8222-222222222222"


def _load(modname: str, filename: str):
    spec = importlib.util.spec_from_file_location(modname, CORE_SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bm = _load("body_manifest", "body-manifest.py")
merge = _load("body_merge", "body-merge.py")


def _reducer_wm_path(pr: Path, agent: str = "alpha") -> Path:
    return pr / "agents" / agent / "session" / "working-memory.yaml"


def _body_wm_path(pr: Path, sid: str, agent: str = "alpha") -> Path:
    return pr / "agents" / agent / "sessions" / sid / "working-memory.yaml"


def _setup_real_fork(tmp_path: Path, baseline_wm: dict, agent: str = "alpha") -> Path:
    """Reducer at `baseline_wm`; real-fork a worker Body from it.

    Uses body_manifest.write_manifest (running-session-id != worker SID -> the
    worker forks), so the Body WM is an authentic byte-copy of the baseline and
    forked_wm_hash is genuine. Returns project_root. The Body starts == baseline;
    the caller then diverges the Body and the reducer independently.
    """
    state = tmp_path / "agents" / agent / "session"
    state.mkdir(parents=True, exist_ok=True)
    (state / "running-session-id").write_text(SID_REDUCER, encoding="utf-8")
    with open(state / "working-memory.yaml", "w", encoding="utf-8") as f:
        yaml.dump(baseline_wm, f, default_flow_style=False, sort_keys=False)
    bm.write_manifest(SID_WORKER, agent, role="worker", project_root=tmp_path)
    return tmp_path


def _rewrite(path: Path, wm: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(wm, f, default_flow_style=False, sort_keys=False)


@pytest.mark.xfail(
    reason="F1 (g-306-65/g-306-67): 2-way SUM double-counts the fork baseline; "
           "needs 3-way delta merge",
    strict=False,
)
def test_counter_does_not_double_count_baseline(tmp_path):
    """Body forks at baseline=10. Body +2 -> 12, reducer +3 -> 13.

    Correct merged = baseline + reducer_delta + body_delta = 10 + 3 + 2 = 15.
    Current 2-way SUM = reducer + body = 13 + 12 = 25 (over by the baseline 10).
    """
    pr = _setup_real_fork(tmp_path, {"slots": {"loop_state": {"goals_completed": 10}}})
    # Diverge the Body (10 -> 12) so the merge path runs (WM != baseline hash).
    body_wm = yaml.safe_load(_body_wm_path(pr, SID_WORKER).read_text(encoding="utf-8"))
    body_wm["slots"]["loop_state"]["goals_completed"] = 12
    _rewrite(_body_wm_path(pr, SID_WORKER), body_wm)
    # Diverge the reducer independently (10 -> 13).
    _rewrite(_reducer_wm_path(pr), {"slots": {"loop_state": {"goals_completed": 13}}})
    bm.set_state(SID_WORKER, "alpha", "closed-pending-merge", project_root=pr)

    merge.generalize_down("alpha", project_root=pr)

    merged = yaml.safe_load(_reducer_wm_path(pr).read_text(encoding="utf-8"))
    assert merged["slots"]["loop_state"]["goals_completed"] == 15


@pytest.mark.xfail(
    reason="F3 (g-306-65/g-306-67): 2-way array union resurrects reducer-deleted "
           "items; needs 3-way delta merge",
    strict=False,
)
def test_array_does_not_resurrect_reducer_deleted_item(tmp_path):
    """Baseline array = [X, Y]. Reducer deletes Y -> [X]. Body leaves the array
    untouched (diverges only `tick`, so the merge still runs). Correct 3-way: the
    reducer's deletion wins -> [X]. Current 2-way union -> [X, Y] (Y resurrected).
    """
    pr = _setup_real_fork(tmp_path, {"slots": {
        "sensory_buffer": [{"id": "X"}, {"id": "Y"}], "tick": 0}})
    # Body diverges ONLY `tick` (so the merge runs); leaves the array == baseline.
    body_wm = yaml.safe_load(_body_wm_path(pr, SID_WORKER).read_text(encoding="utf-8"))
    body_wm["slots"]["tick"] = 1
    _rewrite(_body_wm_path(pr, SID_WORKER), body_wm)
    # Reducer deletes Y post-fork.
    _rewrite(_reducer_wm_path(pr), {"slots": {
        "sensory_buffer": [{"id": "X"}], "tick": 0}})
    bm.set_state(SID_WORKER, "alpha", "closed-pending-merge", project_root=pr)

    merge.generalize_down("alpha", project_root=pr)

    merged = yaml.safe_load(_reducer_wm_path(pr).read_text(encoding="utf-8"))
    ids = [i["id"] for i in merged["slots"]["sensory_buffer"]]
    assert ids == ["X"]  # Y must NOT be resurrected


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q", "-rxX"]))
