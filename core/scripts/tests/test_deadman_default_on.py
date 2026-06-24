"""test_deadman_default_on.py — Stage-5 regression guard (2026-06-23).

Pins the deadman-switch DEFAULT-ON invariant after the Stage-5 flip
(opt-IN `deadman-enabled` -> opt-OUT `deadman-disabled`). A silent
re-inversion back to opt-in would remove fleet-wide silent-loop-death
protection without any other signal, so this guard exists to fail loud.

Three pins:
  1. Both close scripts gate on the OPT-OUT flag `deadman-disabled`, and
     no longer reference the retired opt-IN flag `deadman-enabled`.
  2. Both close scripts still carry the resurrection arm (the sentinel +
     ScheduleWakeup) — i.e. the pair imperative survives the flip.
  3. deadman-arm-audit.py::_flagged_agents implements default-ON semantics:
     an agent with NO flag is ACTIVE; only `deadman-disabled` turns it off.

Refs: core/config/rationale/deadman-switch.md (Rollout: flag-gated ->
default-ON), g-115-1622 (Stage-5 tracker), commit b31b7a6f (Stage-3 audit).
"""
from __future__ import annotations

import importlib.util as _ilu
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
ITER_CLOSE = CORE_SCRIPTS / "iteration-close.sh"
RECUR_CLOSE = CORE_SCRIPTS / "recurring-close.sh"
AUDIT = CORE_SCRIPTS / "deadman-arm-audit.py"
SENTINEL = "<<autonomous-loop-dynamic>>"


@pytest.mark.parametrize("script", [ITER_CLOSE, RECUR_CLOSE], ids=["iteration-close", "recurring-close"])
def test_close_script_gates_on_optout_flag(script):
    """Each close script gates the deadman branch on `deadman-disabled`
    (opt-OUT) and no longer references the retired `deadman-enabled`
    (opt-IN). Historical mentions of the old flag are confined to the
    rationale doc and the audit docstring, NOT the close scripts."""
    text = script.read_text(encoding="utf-8")
    assert "deadman-disabled" in text, (
        f"{script.name} must gate on the opt-OUT flag deadman-disabled "
        f"(Stage-5 default-ON); not found"
    )
    assert "deadman-enabled" not in text, (
        f"{script.name} still references the retired opt-IN flag "
        f"deadman-enabled — Stage-5 inverted the gate to opt-OUT. A "
        f"re-introduction is a silent regression to opt-in (fleet loses "
        f"default protection)."
    )


@pytest.mark.parametrize("script", [ITER_CLOSE, RECUR_CLOSE], ids=["iteration-close", "recurring-close"])
def test_close_script_retains_resurrection_arm(script):
    """The pair imperative (sentinel + ScheduleWakeup) must survive the
    flip — default-ON is meaningless if the arm text was dropped."""
    text = script.read_text(encoding="utf-8")
    assert SENTINEL in text, f"{script.name} lost the resurrection sentinel"
    assert "ScheduleWakeup" in text, f"{script.name} lost the ScheduleWakeup arm"


def _load_audit():
    spec = _ilu.spec_from_file_location("_deadman_audit", AUDIT)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_flagged_agents_default_on(tmp_path):
    """_flagged_agents returns ACTIVE for an agent with no flag, and
    INACTIVE only when `deadman-disabled` is present (default-ON)."""
    audit = _load_audit()
    # Build a synthetic project root: two agents with a session dir, one
    # carrying the opt-out flag, one not.
    (tmp_path / "agents" / "on" / "session").mkdir(parents=True)
    (tmp_path / "agents" / "off" / "session").mkdir(parents=True)
    (tmp_path / "agents" / "off" / "session" / "deadman-disabled").write_text("", encoding="utf-8")

    flagged = audit._flagged_agents(tmp_path)
    assert flagged.get("on") is True, "agent with no flag must be deadman-ACTIVE by default"
    assert flagged.get("off") is False, "agent with deadman-disabled must be INACTIVE"


def test_classify_arms_single_skill_one_assignment():
    """A Skill is assigned to its MOST-RECENT preceding non-batched arm, so two
    arms clustered within the follow window cannot both claim it (which silently
    undercounts orphans). Regression for the deadman-review fix (2026-06-24)."""
    from datetime import datetime, timezone, timedelta
    audit = _load_audit()
    t0 = datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc)
    # arm A (true orphan — its own re-entry is missing) + arm B 5s later whose
    # Skill lands 10s after A. The single Skill belongs to B (latest preceding).
    arm_events = [
        {"ts_dt": t0, "ts_str": "A", "msg_id": "mA"},
        {"ts_dt": t0 + timedelta(seconds=5), "ts_str": "B", "msg_id": "mB"},
    ]
    res = {r["ts"]: r["klass"]
           for r in audit._classify_arms(arm_events, set(), [(t0 + timedelta(seconds=10), "mS")])}
    assert res["A"] == "orphan", f"A should be orphan (its Skill is missing); got {res['A']}"
    assert res["B"] == "followed", f"B should be followed (owns the Skill); got {res['B']}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
