"""Phase 2C (): two-body fork activation + parity harness.

Proves the END-TO-END Mind/Body lifecycle that Phases 1-2 built piece by piece:
two worker Bodies of ONE Mind fork, diverge their WM independently (per-Body
routing active, NO cross-contamination), then generalize-down at the single
reducer merges BOTH into the reducer's WM = the convergence target: each Body
acts independently, ONE reducer not N.

ACTIVATION: this is the first test that actually forks a 2nd worker Body and
runs the full fork -> diverge -> generalize-down cycle live. Phases 1A-2B were
dormant scaffolding in single-runner (one Body == the reducer, routing collapses
to agent-wide); here a 2nd non-reducer worker forks its WM, the body-WM-file's
existence flips per-Body routing on, and the reducer's generalize-down
(body-merge.py, the aspirations-consolidate Step -1 engine) reduces both Bodies
into one. claimed_by stays the mindKey throughout (not exercised here -- claim
is a queue concern, orthogonal to the WM merge).

Daemon-safe (no daemon_integration marker -- pure body-manifest + body-merge +
file arithmetic against a tmp project_root; no live daemon, no subprocess).

Run:
  python -m pytest core/scripts/tests/test_two_body_parity.py -q
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml

CORE_SCRIPTS = Path(__file__).resolve().parent.parent      # core/scripts/
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))


def _load(modname: str, filename: str):
    spec = importlib.util.spec_from_file_location(modname, CORE_SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


bm = _load("body_manifest", "body-manifest.py")
merge = _load("body_merge", "body-merge.py")

# Two SID-shaped unitKeys: the REDUCER (holds running-session-id) + a 2nd WORKER.
REDUCER_SID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
WORKER_SID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _mk_mind(tmp_path: Path, reducer_wm: dict, name: str = "alpha") -> Path:
    """agents/<name>/session/ with the Mind/reducer WM + running-session-id =
    REDUCER_SID (so REDUCER_SID is the DERIVED reducer). Returns project_root."""
    state = tmp_path / "agents" / name / "session"
    state.mkdir(parents=True, exist_ok=True)
    # write_bytes (not write_text) so forked_wm_hash is byte-stable cross-platform
    # (guard-863: text-mode LF->CRLF on Windows would corrupt the hash invariant).
    (state / "working-memory.yaml").write_bytes(
        yaml.dump(reducer_wm, default_flow_style=False, sort_keys=False).encode("utf-8"))
    (state / "running-session-id").write_text(REDUCER_SID, encoding="utf-8")
    return tmp_path


def _body_wm(pr: Path, sid: str, name: str = "alpha") -> Path:
    return pr / "agents" / name / "sessions" / sid / "working-memory.yaml"


def _reducer_wm(pr: Path, name: str = "alpha") -> dict:
    return yaml.safe_load(
        (pr / "agents" / name / "session" / "working-memory.yaml").read_text(encoding="utf-8"))


# ───────────────── the full fork -> diverge -> generalize-down cycle ─────────────────

def test_two_body_parity_end_to_end(tmp_path):
    # Mind/reducer WM: a numeric counter (proves the 3-way delta) + an array slot
    # (proves union+dedup) at fork value 5 / ["seed"].
    pr = _mk_mind(tmp_path, {"slots": {"loop_state": {"goals_completed": 5},
                                       "encoding_queue": ["seed"]}})

    # FORK Body 1 = reducer (running-session-id == its SID -> does NOT fork).
    bm.write_manifest(REDUCER_SID, "alpha", role="worker", project_root=pr)
    assert bm.is_reducer(REDUCER_SID, "alpha", project_root=pr) is True
    assert not _body_wm(pr, REDUCER_SID).exists()          # no body-WM-file -> routing agent-wide
    assert bm.read_manifest(REDUCER_SID, "alpha", project_root=pr)["forked_wm_hash"] is None

    # FORK Body 2 = non-reducer worker (running-session-id != its SID -> FORKS).
    bm.write_manifest(WORKER_SID, "alpha", role="worker", project_root=pr)
    assert bm.is_reducer(WORKER_SID, "alpha", project_root=pr) is False
    worker_wm = _body_wm(pr, WORKER_SID)
    assert worker_wm.exists()                              # the activation signal: 2nd body-WM-file
    baseline = worker_wm.parent / bm._BASELINE_FILENAME
    assert baseline.exists()                               # immutable 3-way common ancestor
    assert worker_wm.read_bytes() == baseline.read_bytes()  # forked byte-faithfully, identical at t0

    # DIVERGE: each Body advances its OWN WM independently.
    # Worker: counter 5 -> 8 (net +3), appends a body-only array item.
    worker_wm.write_text(
        "slots:\n  loop_state:\n    goals_completed: 8\n  encoding_queue:\n  - seed\n  - worker_item\n",
        encoding="utf-8")
    # Reducer: counter 5 -> 7 (net +2), independently, on the agent-wide WM.
    rwm = _reducer_wm(pr)
    rwm["slots"]["loop_state"]["goals_completed"] = 7
    (pr / "agents" / "alpha" / "session" / "working-memory.yaml").write_text(
        yaml.dump(rwm, default_flow_style=False, sort_keys=False), encoding="utf-8")
    # NO cross-contamination: the worker's WM write did not touch the reducer WM.
    assert _reducer_wm(pr)["slots"]["loop_state"]["goals_completed"] == 7
    assert "worker_item" not in _reducer_wm(pr)["slots"]["encoding_queue"]

    # GENUINE CLOSE: the worker finished its work -> marked closed-pending-merge.
    bm.set_state(WORKER_SID, "alpha", "closed-pending-merge", project_root=pr)

    # GENERALIZE-DOWN: the single reducer merges Body 2 into its WM.
    summary = merge.generalize_down("alpha", project_root=pr)
    assert WORKER_SID in summary["merged"]
    assert REDUCER_SID not in summary["merged"]            # reducer never forked -> not enumerated

    merged = _reducer_wm(pr)
    # 3-way delta on the counter: reducer 7 + (worker 8 - baseline 5) = 10 (NOT 2-way 7+8=15).
    assert merged["slots"]["loop_state"]["goals_completed"] == 10
    # array union+dedup: the shared "seed" is deduped, the worker's item carried.
    assert merged["slots"]["encoding_queue"] == ["seed", "worker_item"]
    # Body 2 manifest marked merged -> the next generalize-down won't re-merge it.
    assert bm.read_manifest(WORKER_SID, "alpha", project_root=pr)["body_state"] == "merged"


# ───────────────── the convergence invariant: N Bodies, exactly ONE reducer ─────────────────

def test_one_reducer_not_n(tmp_path):
    # running-session-id holds exactly one SID, so is_reducer is True for exactly
    # one Body of the Mind -- "one reducer, not N" (running two learning loops is
    # the defect the convergence forbids).
    pr = _mk_mind(tmp_path, {"slots": {"x": 1}})
    bm.write_manifest(REDUCER_SID, "alpha", role="worker", project_root=pr)
    bm.write_manifest(WORKER_SID, "alpha", role="worker", project_root=pr)
    reducers = [s for s in (REDUCER_SID, WORKER_SID)
                if bm.is_reducer(s, "alpha", project_root=pr)]
    assert reducers == [REDUCER_SID]                       # exactly one; the rsid holder


def test_two_workers_both_merge_into_single_reducer(tmp_path):
    # Three Bodies: 1 reducer + 2 non-reducer workers. BOTH workers fork, diverge,
    # and generalize-down reduces BOTH into the ONE reducer = the N-Bodies-one-reduce
    # design: each Body acts independently and the reducer reduces ONCE per Mind,
    # NOT N reducers.
    third_sid = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    pr = _mk_mind(tmp_path, {"slots": {"loop_state": {"goals_completed": 5}}})
    bm.write_manifest(REDUCER_SID, "alpha", role="worker", project_root=pr)   # reducer, no fork
    for sid, val in ((WORKER_SID, 8), (third_sid, 6)):
        bm.write_manifest(sid, "alpha", role="worker", project_root=pr)       # each forks (baseline 5)
        _body_wm(pr, sid).write_text(
            f"slots:\n  loop_state:\n    goals_completed: {val}\n", encoding="utf-8")
        bm.set_state(sid, "alpha", "closed-pending-merge", project_root=pr)
    summary = merge.generalize_down("alpha", project_root=pr)
    assert set(summary["merged"]) == {WORKER_SID, third_sid}
    # 3-way deltas accumulate into the ONE reducer: 5 + (8-5) + (6-5) = 9.
    assert _reducer_wm(pr)["slots"]["loop_state"]["goals_completed"] == 9
    # both worker manifests marked merged; the reducer's stays active (never queued).
    assert bm.read_manifest(WORKER_SID, "alpha", project_root=pr)["body_state"] == "merged"
    assert bm.read_manifest(third_sid, "alpha", project_root=pr)["body_state"] == "merged"
    assert bm.read_manifest(REDUCER_SID, "alpha", project_root=pr)["body_state"] == "active"


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))
