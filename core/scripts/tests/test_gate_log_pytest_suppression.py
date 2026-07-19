"""test_gate_log_pytest_suppression.py — the  pytest guard.

Background:
  _gate_log.log() had no test-mode guard, so any test importing a gate module
  and exercising a classifier appended SYNTHETIC firings to the PRODUCTION
  meta/gate-firings.jsonl (test_target_state_check_positional.py leaked ~16
  read-intent-verbs records per suite run since 2026-05-17; the first run of
  test_target_state_removal_intent.py wrote 17 removal-intent-verbs records —
  discovered during g-248-101). The evaluator ratios those stores feed
  (gate-retirement-eval) then scored synthetic activity as production signal.

Contract pinned here:
  (a) Under pytest (PYTEST_CURRENT_TEST set), log() is a silent no-op.
  (b) GATE_LOG_ALLOW_PYTEST=1 opts back in (for tests that positively assert
      on firing records against a tmp meta_dir, e.g. test_layer_d_telemetry).
  (c) Outside pytest (PYTEST_CURRENT_TEST absent), log() writes — the
      production path is unchanged.
  (d) The never-raises contract survives the guard.

All cases pass meta_dir=tmp_path so no case can touch the production store.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

GL_PATH = CORE_SCRIPTS / "_gate_log.py"
spec = importlib.util.spec_from_file_location("_gate_log_under_test", GL_PATH)
gl_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gl_mod)


def _records(tmp_path: Path) -> list[dict]:
    path = tmp_path / "gate-firings.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def test_suppressed_under_pytest(tmp_path, monkeypatch):
    """(a) PYTEST_CURRENT_TEST is set by pytest itself — log() must no-op."""
    monkeypatch.delenv("GATE_LOG_ALLOW_PYTEST", raising=False)
    gl_mod.log("suppression-test-gate", "pass",
               caller="test_gate_log_pytest_suppression",
               trigger_matched="synthetic", meta_dir=tmp_path)
    assert _records(tmp_path) == []


def test_opt_out_env_restores_writes(tmp_path, monkeypatch):
    """(b) GATE_LOG_ALLOW_PYTEST=1 re-enables writes (tmp destination)."""
    monkeypatch.setenv("GATE_LOG_ALLOW_PYTEST", "1")
    gl_mod.log("suppression-test-gate", "block",
               caller="test_gate_log_pytest_suppression",
               trigger_matched="synthetic", meta_dir=tmp_path)
    recs = _records(tmp_path)
    assert len(recs) == 1
    assert recs[0]["gate_id"] == "suppression-test-gate"
    assert recs[0]["decision"] == "block"


def test_writes_outside_pytest(tmp_path, monkeypatch):
    """(c) With PYTEST_CURRENT_TEST absent, the production path is unchanged."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("GATE_LOG_ALLOW_PYTEST", raising=False)
    gl_mod.log("suppression-test-gate", "noop",
               caller="test_gate_log_pytest_suppression", meta_dir=tmp_path)
    recs = _records(tmp_path)
    assert len(recs) == 1
    assert recs[0]["decision"] == "noop"


def test_never_raises_with_bad_payload(tmp_path, monkeypatch):
    """(d) Circular payload under opt-out: log() must swallow, not raise."""
    monkeypatch.setenv("GATE_LOG_ALLOW_PYTEST", "1")
    circular = {}
    circular["self"] = circular
    gl_mod.log("suppression-test-gate", "pass",
               payload=circular, meta_dir=tmp_path)
    # No assertion on record presence — the contract is only "no exception".
