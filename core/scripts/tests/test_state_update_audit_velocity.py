"""Regression pins for the 1 velocity-meter honesty fixes.

Defect chain: iteration-close.sh state-update forwards the § STATE-UPDATE
quality flags only when the LLM passes them; state-update-audit.py's old
argparse defaults (0/0.0/0) made compute_learning_value return 0.0 for every
UNFLAGGED deep close, and cmd_velocity recorded that false zero into
meta/improvement-velocity.yaml — poisoning every rolling window (observed:
0.0 for unambiguously deep goals g-115-2442/g-115-2438 beside flagged siblings
at 0.6-0.79; pre-g-115-228 history was 206/206 dead-zero).

Fixes pinned here:
  - None argparse sentinels distinguish "not passed" from "explicit zero".
  - cmd_velocity SKIPS the imp@k snapshot on unmeasured closes
    (flag velocity_unmeasured_skipped, learning_value None, no subprocess).
  - An EXPLICIT --encoding-score 0.0 still records a real measured zero.
  - cmd_run_all skips the whole velocity cascade (backpressure/temporal-credit/
    relative-advantage) on unmeasured — feeding them a fabricated 0.0 would
    recreate the same poison one layer down.
  - compute_learning_value coerces None inputs (no TypeError).
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
SCRIPT = SCRIPTS_DIR / "state-update-audit.py"


def _import():
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location("state_update_audit", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["state_update_audit"] = mod
    spec.loader.exec_module(mod)
    return mod


MOD = _import()


def _args(**kw):
    base = dict(goal="g-test", outcome_class="deep", category="cat",
                experience_id=None, tree_updated=False, artifacts_count=None,
                encoding_score=None, findings_count=None, exploration=False,
                learning_value=0.0)
    base.update(kw)
    return argparse.Namespace(**base)


# ── compute_learning_value ───────────────────────────────────────────────────

def test_compute_learning_value_none_inputs_coerce():
    assert MOD.compute_learning_value(False, None, None, None) == 0.0


def test_compute_learning_value_weights_unchanged():
    # 4-component weighted sum: tree .3 + artifacts .3 + encoding .2 + findings .2
    assert MOD.compute_learning_value(True, 5, 1.0, 4) == 1.0
    assert MOD.compute_learning_value(True, 0, 0.0, 0) == 0.3


# ── cmd_velocity unmeasured-skip ─────────────────────────────────────────────

def test_velocity_unmeasured_skips_snapshot(monkeypatch):
    # No quality input at all -> UNMEASURED: no snapshot subprocess, flag set,
    # learning_value None (not a fabricated 0.0).
    calls = []
    monkeypatch.setattr(MOD, "_run",
                        lambda argv, **kw: calls.append(argv) or ("", "", 0))
    r = MOD.cmd_velocity(_args())
    assert "velocity_unmeasured_skipped" in r["flags"]
    assert r["learning_value"] is None
    assert calls == []          # neither backpressure-status nor impk snapshot ran


def test_velocity_explicit_zero_is_measured(monkeypatch):
    # --encoding-score 0.0 passed explicitly (ritual outcome): measured,
    # snapshot fires with learning_value 0.0.
    calls = []
    monkeypatch.setattr(MOD, "_run",
                        lambda argv, **kw: calls.append(argv) or ("", "", 0))
    r = MOD.cmd_velocity(_args(encoding_score=0.0))
    assert "velocity_unmeasured_skipped" not in r["flags"]
    assert r["learning_value"] == 0.0
    snap = [a for a in calls if a[:2] == ["meta-impk.sh", "snapshot"]]
    assert len(snap) == 1


def test_velocity_tree_updated_alone_is_measured(monkeypatch):
    calls = []
    monkeypatch.setattr(MOD, "_run",
                        lambda argv, **kw: calls.append(argv) or ("", "", 0))
    r = MOD.cmd_velocity(_args(tree_updated=True))
    assert "velocity_unmeasured_skipped" not in r["flags"]
    assert r["learning_value"] == 0.3
    assert any(a[:2] == ["meta-impk.sh", "snapshot"] for a in calls)


# ── cmd_run_all cascade skip ─────────────────────────────────────────────────

def test_run_all_unmeasured_skips_cascade(monkeypatch):
    calls = []
    monkeypatch.setattr(MOD, "_run",
                        lambda argv, **kw: calls.append(argv) or ("", "", 0))
    r = MOD.cmd_run_all(_args())
    assert r["flags"] == ["velocity:velocity_unmeasured_skipped"]
    assert list(r["results"].keys()) == ["velocity"]
    assert calls == []          # no backpressure/temporal-credit/relative-advantage


def test_run_all_routine_short_circuit_unchanged(monkeypatch):
    monkeypatch.setattr(MOD, "_run",
                        lambda argv, **kw: (_ for _ in ()).throw(AssertionError))
    r = MOD.cmd_run_all(_args(outcome_class="routine"))
    assert "skipped (outcome_class=routine)" in r["summary"]


def test_run_all_measured_runs_cascade(monkeypatch):
    monkeypatch.setattr(MOD, "_run", lambda argv, **kw: ("{}", "", 0))
    r = MOD.cmd_run_all(_args(tree_updated=True, encoding_score=0.7,
                              artifacts_count=1, findings_count=2))
    assert set(r["results"].keys()) == {"velocity", "backpressure",
                                        "temporal_credit", "relative_advantage"}
