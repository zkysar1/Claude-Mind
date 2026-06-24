"""Generalize-down body-WM merge tests (Phase 1C, ).

Covers the JOIN half of the Body lifecycle: the reducer's Step -1
`generalize_down(agent)` that merges every `closed-pending-merge` Body's WM
into the reducer's WM under per-slot policies, then marks each `merged`.

Backward-compat anchor: the 1-body / 0-body case is a no-op (no
closed-pending-merge manifest exists in single-runner) — `test_no_bodies_noop`
and `test_active_body_not_merged` pin that the dormant scaffolding never touches
the reducer WM until a real worker Body closes.

Daemon-safe (no daemon_integration marker — pure path + file arithmetic; passes
project_root explicitly, never touches wm.py env-routed I/O, so no BODY_WM_PATH
redirection is needed — guard-862 is N/A here).

Run:
  python -m pytest core/scripts/tests/test_body_merge.py -q
"""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import yaml

CORE_SCRIPTS = Path(__file__).resolve().parent.parent  # core/scripts/

SID_REDUCER = "11111111-1111-4111-8111-111111111111"
SID_WORKER = "22222222-2222-4222-8222-222222222222"
SID_WORKER2 = "33333333-3333-4333-8333-333333333333"
_BAD_HASH = "0" * 64  # never matches a real WM -> forces the merge path (not no-op)


def _load(modname: str, filename: str):
    spec = importlib.util.spec_from_file_location(modname, CORE_SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bm = _load("body_manifest", "body-manifest.py")
merge = _load("body_merge", "body-merge.py")


# ─────────────────────────── fixtures ───────────────────────────

def _mk_agent(tmp_path: Path, name: str = "alpha",
              reducer_wm: dict | None = None,
              running_sid: str | None = None) -> Path:
    """Build agents/<name>/session/ with an optional reducer WM + running-session-id.
    Returns project_root."""
    state = tmp_path / "agents" / name / "session"
    state.mkdir(parents=True, exist_ok=True)
    if running_sid is not None:
        (state / "running-session-id").write_text(running_sid, encoding="utf-8")
    if reducer_wm is not None:
        with open(state / "working-memory.yaml", "w", encoding="utf-8") as f:
            yaml.dump(reducer_wm, f, default_flow_style=False, sort_keys=False)
    return tmp_path


def _mk_pending_body(pr: Path, agent: str, sid: str, body_wm: dict,
                     forked_hash: str = _BAD_HASH,
                     state: str = "closed-pending-merge") -> Path:
    """Create a sessions/<sid>/ Body dir with a manifest + diverged WM."""
    sess = pr / "agents" / agent / "sessions" / sid
    sess.mkdir(parents=True, exist_ok=True)
    with open(sess / "working-memory.yaml", "w", encoding="utf-8") as f:
        yaml.dump(body_wm, f, default_flow_style=False, sort_keys=False)
    manifest = {
        "unitKey": sid, "mindKey": agent, "env_id": "local", "role": "worker",
        "body_state": state, "started_at": "2026-06-24T00:00:00",
        "forked_wm_hash": forked_hash,
    }
    with open(sess / "body-manifest.yaml", "w", encoding="utf-8") as f:
        yaml.dump(manifest, f, default_flow_style=False, sort_keys=False)
    return sess


def _read_reducer(pr: Path, agent: str = "alpha") -> dict:
    p = pr / "agents" / agent / "session" / "working-memory.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _body_state(pr: Path, agent: str, sid: str) -> str:
    return bm.read_manifest(sid, agent, project_root=pr).get("body_state")


# ─────────────────────────── 1-body / 0-body no-op (dormant) ───────────────────────────

def test_no_bodies_noop(tmp_path):
    """No sessions dir at all -> empty summary, reducer WM untouched."""
    pr = _mk_agent(tmp_path, reducer_wm={"slots": {"x": 1}})
    summary = merge.generalize_down("alpha", project_root=pr)
    assert summary["merged"] == [] and summary["scanned"] == 0
    assert _read_reducer(pr) == {"slots": {"x": 1}}


def test_active_body_not_merged(tmp_path):
    """A Body still 'active' (not closed-pending-merge) is not enumerated."""
    pr = _mk_agent(tmp_path, reducer_wm={"slots": {"x": 1}})
    _mk_pending_body(pr, "alpha", SID_WORKER, {"slots": {"x": 99}}, state="active")
    summary = merge.generalize_down("alpha", project_root=pr)
    assert summary["merged"] == [] and summary["scanned"] == 0
    assert _read_reducer(pr)["slots"]["x"] == 1  # reducer untouched


# ─────────────────────────── 2-body merge (the activation case) ───────────────────────────

def test_two_body_merge_arrays_and_counters(tmp_path):
    """A closed-pending-merge Body: arrays append+dedup, counters SUM."""
    reducer = {
        "slots": {
            "sensory_buffer": [{"id": "A"}],
            "counter_scalar": 2,
        }
    }
    body = {
        "slots": {
            "sensory_buffer": [{"id": "A"}, {"id": "B"}],  # A dup, B new
            "counter_scalar": 5,
        }
    }
    pr = _mk_agent(tmp_path, reducer_wm=reducer)
    _mk_pending_body(pr, "alpha", SID_WORKER, body)
    summary = merge.generalize_down("alpha", project_root=pr)

    assert summary["merged"] == [SID_WORKER]
    out = _read_reducer(pr)
    # array: union with A deduped, B appended after.
    assert out["slots"]["sensory_buffer"] == [{"id": "A"}, {"id": "B"}]
    # counter: SUM.
    assert out["slots"]["counter_scalar"] == 7
    # manifest marked merged.
    assert _body_state(pr, "alpha", SID_WORKER) == "merged"


def test_active_context_reducer_wins(tmp_path):
    """active_context (and session identity) keep the reducer's value."""
    reducer = {"slots": {"active_context": {"summary": "reducer-ctx"}},
               "session_id": "reducer-session"}
    body = {"slots": {"active_context": {"summary": "body-ctx"}},
            "session_id": "body-session"}
    pr = _mk_agent(tmp_path, reducer_wm=reducer)
    _mk_pending_body(pr, "alpha", SID_WORKER, body)
    merge.generalize_down("alpha", project_root=pr)
    out = _read_reducer(pr)
    assert out["slots"]["active_context"]["summary"] == "reducer-ctx"
    assert out["session_id"] == "reducer-session"


def test_timestamp_latest_wins(tmp_path):
    """Cadence-tracker ISO timestamps take the later of reducer/body."""
    reducer = {"slots": {"last_strategic_scan": "2026-06-24T01:00:00"}}
    body = {"slots": {"last_strategic_scan": "2026-06-24T05:00:00"}}
    pr = _mk_agent(tmp_path, reducer_wm=reducer)
    _mk_pending_body(pr, "alpha", SID_WORKER, body)
    merge.generalize_down("alpha", project_root=pr)
    assert _read_reducer(pr)["slots"]["last_strategic_scan"] == "2026-06-24T05:00:00"


def test_loop_state_recurse_counters_sum(tmp_path):
    """Nested dict (loop_state.signals) recurses: nested counters SUM."""
    reducer = {"slots": {"loop_state": {
        "goals_completed": 10,
        "signals": {"routine_count_total": 2, "productive_streak": 1},
    }}}
    body = {"slots": {"loop_state": {
        "goals_completed": 3,
        "signals": {"routine_count_total": 5, "productive_streak": 4},
    }}}
    pr = _mk_agent(tmp_path, reducer_wm=reducer)
    _mk_pending_body(pr, "alpha", SID_WORKER, body)
    merge.generalize_down("alpha", project_root=pr)
    ls = _read_reducer(pr)["slots"]["loop_state"]
    assert ls["goals_completed"] == 13
    assert ls["signals"]["routine_count_total"] == 7
    assert ls["signals"]["productive_streak"] == 5


def test_body_only_slot_carried_in(tmp_path):
    """A slot the reducer lacks but the Body has is carried into the reducer."""
    pr = _mk_agent(tmp_path, reducer_wm={"slots": {"a": 1}})
    _mk_pending_body(pr, "alpha", SID_WORKER, {"slots": {"a": 1, "b_new": [1, 2]}})
    merge.generalize_down("alpha", project_root=pr)
    out = _read_reducer(pr)
    assert out["slots"]["b_new"] == [1, 2]


# ─────────────────────────── no-op hash short-circuit (real fork path) ───────────────────────────

def test_undiverged_body_noop_via_real_fork(tmp_path):
    """A Body forked but never diverged (WM == baseline hash) -> no-op merge.

    Uses the REAL fork path (body_manifest.write_manifest) so the
    forked_wm_hash is authentic, then closes the Body WITHOUT diverging it.
    generalize_down must detect WM == baseline and mark merged WITHOUT touching
    the reducer WM.
    """
    reducer_text = "slots:\n  sensory_buffer:\n  - id: A\n"
    state = tmp_path / "agents" / "alpha" / "session"
    state.mkdir(parents=True, exist_ok=True)
    (state / "running-session-id").write_text(SID_REDUCER, encoding="utf-8")
    (state / "working-memory.yaml").write_bytes(reducer_text.encode("utf-8"))
    pr = tmp_path
    # Real fork: SID_WORKER != reducer SID -> forks, baseline = sha256(reducer WM).
    bm.write_manifest(SID_WORKER, "alpha", role="worker", project_root=pr)
    forked = bm.read_manifest(SID_WORKER, "alpha", project_root=pr)
    assert forked["forked_wm_hash"] == hashlib.sha256(
        reducer_text.encode("utf-8")).hexdigest()
    # Close WITHOUT diverging the body WM.
    bm.set_state(SID_WORKER, "alpha", "closed-pending-merge", project_root=pr)

    summary = merge.generalize_down("alpha", project_root=pr)
    assert summary["noop"] == [SID_WORKER]
    assert summary["merged"] == []
    assert _body_state(pr, "alpha", SID_WORKER) == "merged"
    # Reducer WM byte-identical (no merge applied).
    assert (state / "working-memory.yaml").read_bytes() == reducer_text.encode("utf-8")


# ─────────────────────────── multi-body in one pass ───────────────────────────

def test_two_pending_bodies_both_merge(tmp_path):
    pr = _mk_agent(tmp_path, reducer_wm={"slots": {"counter_scalar": 0,
                                                   "sensory_buffer": []}})
    _mk_pending_body(pr, "alpha", SID_WORKER,
                     {"slots": {"counter_scalar": 4, "sensory_buffer": [{"id": "X"}]}})
    _mk_pending_body(pr, "alpha", SID_WORKER2,
                     {"slots": {"counter_scalar": 6, "sensory_buffer": [{"id": "Y"}]}})
    summary = merge.generalize_down("alpha", project_root=pr)
    assert set(summary["merged"]) == {SID_WORKER, SID_WORKER2}
    out = _read_reducer(pr)
    assert out["slots"]["counter_scalar"] == 10  # 0 + 4 + 6
    ids = {item["id"] for item in out["slots"]["sensory_buffer"]}
    assert ids == {"X", "Y"}
    assert _body_state(pr, "alpha", SID_WORKER) == "merged"
    assert _body_state(pr, "alpha", SID_WORKER2) == "merged"


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
