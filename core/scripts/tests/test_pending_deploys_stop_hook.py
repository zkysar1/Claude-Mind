"""test_pending_deploys_stop_hook.py — 8-c (pending-deploys hard gate, SG-c STOP+HARDEN).

Covers the two SG-c guarantees:

  (A) pending-deploys.py roll-handoff — at graceful stop, unresolved deploy
      obligations are COPIED into handoff.yaml (a visibility mirror the next
      session's boot surfaces) WITHOUT clearing pending-deploys.yaml. The store
      lives in the agent-wide session dir and persists across sessions, so the
      next session's SG-b all-sweep keeps it as the source of truth. The merge is
      dedup-by-(repo,sha), preserves every other handoff key, and NEVER clobbers
      an unparseable handoff.

  (B) MUTATION-PROOF that the SG-b ENFORCE gate is load-bearing: on a FAILED
      deploy the real pending-deploys-gate.sh flags not_clean:true; a pass-through
      mutation that silences not_clean flips it to false. The two verdicts differ,
      so mutating the gate to pass-through makes the load-bearing assertion fail —
      exactly the regression the SG-c outcome demands the test catch.

Part B reuses the SG-b gate harness (fake gh + stubbed daemon wrappers) from
test_pending_deploys_gate.py — one temp mind repo with the REAL gate + deps.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PD = CORE_SCRIPTS / "pending-deploys.py"
PROJECT_TMP = SCRIPT_DIR / "_tmp_pending_deploys_stop_hook_test"

sys.path.insert(0, str(SCRIPT_DIR))
# Reuse the SG-b gate harness for the mutation-proof (Part B).
from test_pending_deploys_gate import (  # noqa: E402
    _entry,
    _hermetic_env,
    _run_gate,
    _seed,
    _setup_repo,
    _summary,
)


# ── Part A helpers: roll-handoff exercises pure file ops via --store/--handoff ──

def _roll(store: Path, handoff: Path):
    """Invoke the REAL pending-deploys.py roll-handoff with explicit paths.
    sys.executable (not bash) — roll-handoff is pure Python, no shim needed."""
    return subprocess.run(
        [sys.executable, str(PD), "--store", str(store),
         "roll-handoff", "--handoff", str(handoff)],
        capture_output=True, text=True, timeout=60, env=_hermetic_env(),
    )


def _entry_d(sha, goal="g-1", repo="owner/prod", d="/tmp/x"):
    return {"repo": repo, "sha": sha, "goal_id": goal, "dir": d,
            "ts": "2026-07-19T15:00:00"}


def _seed_store(store: Path, entries):
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(yaml.safe_dump(entries))


def _last_json(result) -> dict:
    lines = [l for l in (result.stdout or "").splitlines() if l.strip().startswith("{")]
    assert lines, f"no JSON on stdout; stdout={result.stdout!r} stderr={result.stderr!r}"
    return json.loads(lines[-1])


# ── Part A tests: roll-handoff ───────────────────────────────────────────────

def test_roll_handoff_mirrors_into_handoff_without_clearing_store():
    """Unresolved entries are copied into handoff.pending_deploys; other handoff
    keys are preserved; the source store is NOT cleared (persists agent-wide)."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        store = Path(td) / "pending-deploys.yaml"
        handoff = Path(td) / "handoff.yaml"
        _seed_store(store, [_entry_d("a" * 40, "g-1"),
                            _entry_d("b" * 40, "g-2", repo="owner/other")])
        handoff.write_text(yaml.safe_dump(
            {"session_id": "s-9", "known_blockers": ["streak-ci"]}))
        r = _roll(store, handoff)
        assert r.returncode == 0, r.stderr
        s = _last_json(r)
        assert s["rolled"] == 2, s
        doc = yaml.safe_load(handoff.read_text())
        # Every pre-existing handoff key survives.
        assert doc["session_id"] == "s-9", doc
        assert doc["known_blockers"] == ["streak-ci"], doc
        # Both obligations mirrored, carrying goal_id + a rolled_at stamp.
        shas = {e["sha"] for e in doc["pending_deploys"]}
        assert shas == {"a" * 40, "b" * 40}, doc
        assert all("rolled_at" in e and e.get("goal_id") for e in doc["pending_deploys"]), doc
        # Source of truth persists — the SG-b all-sweep must still see them.
        assert len(yaml.safe_load(store.read_text())) == 2, "store must NOT be cleared"


def test_roll_handoff_dedups_on_repeat():
    """Rolling twice does not duplicate an entry already in the handoff mirror."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        store = Path(td) / "pending-deploys.yaml"
        handoff = Path(td) / "handoff.yaml"
        _seed_store(store, [_entry_d("c" * 40)])
        first = _last_json(_roll(store, handoff))
        assert first["rolled"] == 1, first
        second = _last_json(_roll(store, handoff))
        assert second["rolled"] == 0, second
        doc = yaml.safe_load(handoff.read_text())
        assert len(doc["pending_deploys"]) == 1, doc


def test_roll_handoff_empty_store_is_noop():
    """No pending entries -> rolled:0 and no handoff is written."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        store = Path(td) / "pending-deploys.yaml"   # absent
        handoff = Path(td) / "handoff.yaml"          # absent
        s = _last_json(_roll(store, handoff))
        assert s["rolled"] == 0, s
        assert not handoff.exists(), "empty roll must not create a handoff"


def test_roll_handoff_preserves_unparseable_handoff():
    """A malformed handoff is left byte-for-byte intact (never clobbered)."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        store = Path(td) / "pending-deploys.yaml"
        handoff = Path(td) / "handoff.yaml"
        _seed_store(store, [_entry_d("d" * 40)])
        garbage = "{[ this: is not: valid yaml ][ }: :\n\t- broken"
        handoff.write_text(garbage)
        s = _last_json(_roll(store, handoff))
        assert s["rolled"] == 0 and "error" in s, s
        assert handoff.read_text() == garbage, "unparseable handoff must be untouched"
        # And the store is still intact.
        assert len(yaml.safe_load(store.read_text())) == 1


def test_roll_handoff_routes_through_locked_writer():
    """Regression guard (9, fresh-eyes SG-c Finding 2): the handoff
    write in cmd_roll_handoff MUST go through the canonical locked_write_yaml
    (history snapshot + changelog + atomic rename + surrogate validation), NOT a
    raw Path.write_text that bypasses all of them. The write is a subprocess-only
    code path, so this asserts on the source the way the Part B mutation-proof
    does: a revert to the raw yaml-branch write fails this."""
    src = PD.read_text(encoding="utf-8")
    start = src.index("def cmd_roll_handoff(")
    end = src.index("\ndef ", start + 1)   # next top-level def bounds the body
    body = src[start:end]
    assert "locked_write_yaml(hp, doc)" in body, \
        "cmd_roll_handoff must write handoff via locked_write_yaml"
    # The raw yaml-branch write_text must be gone (the json.dumps fallback for
    # the yaml-is-None branch is allowed to remain — it never uses the dumper).
    assert "yaml.safe_dump(doc" not in body, \
        "raw yaml.safe_dump(doc)->write_text bypasses the locked writer"


# ── Part B: mutation-proof that the SG-b ENFORCE gate is load-bearing ─────────

def test_enforce_gate_is_load_bearing_mutation_proof():
    """The SG-b ENFORCE gate MUST flag not_clean on a FAILED deploy. A
    pass-through mutation that silences the not_clean signal flips the verdict to
    false — so the real gate and the mutated gate DISAGREE. That disagreement is
    the proof the gate is load-bearing: mutating it to pass-through makes the
    'not_clean is True on failure' assertion fail."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        repo = _setup_repo(Path(td))
        filed = Path(td) / "filed.jsonl"

        # (1) REAL gate on a failed deploy -> not_clean MUST be true.
        # Entry goal_id MUST match the --goal filter or has-pending fast-exits.
        _seed(repo, [_entry(sha="e" * 40, goal="g-x")])
        real = _run_gate(repo, "--goal", "g-x",
                         FAKE_GH_CONCLUSION="failure", FILED_GOALS=str(filed))
        s_real = _summary(real)
        assert s_real["not_clean"] is True, f"real gate must flag not_clean on failure: {s_real}"
        assert s_real["failed"] == 1, s_real

        # (2) MUTATE the gate copy to pass-through: silence the not_clean signal.
        gate = repo / "core" / "scripts" / "pending-deploys-gate.sh"
        src = gate.read_text()
        mutated = src.replace("not_clean=1", "not_clean=0")
        assert mutated != src, "mutation anchor 'not_clean=1' not found — gate refactored?"
        gate.write_text(mutated)

        # (3) Same failed deploy through the MUTATED gate -> not_clean:false.
        _seed(repo, [_entry(sha="f" * 40, goal="g-x")])
        mut = _run_gate(repo, "--goal", "g-x",
                        FAKE_GH_CONCLUSION="failure", FILED_GOALS=str(filed))
        s_mut = _summary(mut)
        assert s_mut["not_clean"] is False, f"pass-through mutation should clear not_clean: {s_mut}"

        # The verdicts differ — the test discriminates the mutation, so the gate
        # is proven load-bearing (a silent pass-through does NOT survive this test).
        assert s_real["not_clean"] != s_mut["not_clean"], "test fails to discriminate the mutation"
