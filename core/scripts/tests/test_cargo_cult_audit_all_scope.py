"""8: cargo-cult-detector --audit-all row ownership annotation.

cmd_audit_all builds the batch-calibration markdown table. Before g-115-1448
the table showed each row's OWNER (world / agent: X) but not the apply POLICY,
so the claiming agent had to infer ownership scope manually (the friction
bravo hit during g-115-1447). This test pins the Scope column + legend:

  - world-queue rows           -> Scope "shared"            (direct-apply)
  - agent-queue rows (owned)   -> Scope "owned:<agent>"     (surface-to-owner
                                  unless the claimer IS that agent)
  - footer carries the guard-762 apply-rule legend

The table-building block is inline in cmd_audit_all, so the test drives the
whole function with the boundary dependencies monkeypatched to controlled
fixtures and captures the filed idea's description.

Run: py -3 -m pytest core/scripts/tests/test_cargo_cult_audit_all_scope.py -v
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
DETECTOR_PY = CORE_SCRIPTS / "cargo-cult-detector.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("cargo_cult_detector", DETECTOR_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_audit_all_emits_scope_column_and_legend(monkeypatch):
    mod = _load_module()
    captured: dict = {}

    # Bypass self-dedup (cfg={} already yields 0, but be explicit).
    monkeypatch.setattr(mod, "_recent_audit_all_batch", lambda hours: False)
    # Two scan passes: world + one agent.
    monkeypatch.setattr(mod, "discover_agents", lambda: ["alpha"])
    monkeypatch.setattr(mod, "is_artifact_producing", lambda g: (False, None))
    monkeypatch.setattr(mod, "_gate_log", lambda *a, **k: None)

    def fake_iter(source, agent_override=None):
        if source == "world":
            yield ({"id": "asp-115"},
                   {"id": "g-115-20", "title": "World benchmark", "interval_hours": 1378})
        elif source == "agent" and agent_override == "alpha":
            yield ({"id": "asp-001"},
                   {"id": "g-001-03", "title": "Tree maintenance", "interval_hours": 40})

    monkeypatch.setattr(mod, "_iter_recurring_goals", fake_iter)

    def fake_score(g):
        return {
            "goal_id": g["id"], "title": g["title"],
            "interval_hours": g["interval_hours"], "achievedCount": 10,
            "consecutive_routine": 2, "signal_ratio": 0.9,
            # : _score_recurring now also returns the lifetime tally;
            # the fake must match the contract (cmd_audit_all reads these).
            "substantive_hits": 0, "substantive_runs": 0,
            "lifetime_hit_rate": 1.0, "last_substantive_at": None,
            "score": 4.0,
        }

    monkeypatch.setattr(mod, "_score_recurring", fake_score)
    monkeypatch.setattr(mod, "_propose_new_interval",
                        lambda g, cfg: g["interval_hours"] * 1.5)
    # source_path("world") -> nonexistent so the asp-001 verify branch is skipped.
    monkeypatch.setattr(mod, "source_path", lambda s: Path("nonexistent-zzz-test"))
    monkeypatch.setattr(mod, "file_idea",
                        lambda asp, src, idea: captured.update(idea) or "g-115-NEW")

    class Args:
        dry_run = False

    rc = mod.cmd_audit_all(Args(), {})
    assert rc in (0, None), f"expected file success, got rc={rc}"

    desc = captured.get("description", "")
    assert desc, "no description captured — file_idea not called"

    # Header carries the Scope column.
    assert "| Scope |" in desc, f"Scope column missing from header:\n{desc}"
    # World row -> shared; agent row -> owned:alpha.
    assert "| shared " in desc, f"world row not marked 'shared':\n{desc}"
    assert "| owned:alpha " in desc, f"agent row not marked 'owned:alpha':\n{desc}"
    # Legend present with the guard-762 apply-rule anchor.
    assert "guard-762" in desc, "scope legend (guard-762 anchor) missing"
    assert "SURFACE" in desc, "surface-to-owner rule missing from legend"

    print("PASS (scope column + owned/shared rows + guard-762 legend)")
