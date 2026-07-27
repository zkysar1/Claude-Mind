"""test_role_multiplier_coverage_audit.py —  regression.

THE GAP (g-115-2858): a newly-added or merged agent (foxtrot after the
charlie+delta->foxtrot merge, 2026-07-07) can be ABSENT from
agent_role_multipliers for weeks. goal-selector.py compute_role_affinity then
returns 0.0 for ALL that agent's goals — a silent scorer bug that surfaces only
as an anomalous self-abstention pattern. No check caught it.

THE CHECK: role-multiplier-coverage-audit.py asserts every ACTIVE agent (a
non-retired team-state agent_status row) has an agent_role_multipliers entry.
MISSING is blocking (ok=False); a STALE entry for a retired agent is advisory
only (does not flip ok).

This test exercises audit(world_dir, meta_dir) directly against seeded team-state
shards + a seeded goal-selection-strategy.yaml:
  1. ALL active covered (one retired agent present in multipliers) -> ok=True,
     missing=[], stale=[<retired>].
  2. An active agent MISSING from multipliers                      -> ok=False,
     missing=[<that agent>].
  3. A retired agent absent from multipliers                       -> NOT missing
     (retired agents are excluded from the active roster).

STORAGE_BACKEND=local pins any backend overlay to the local filesystem
(own-cloud S3-key-collision guard, guard-955).
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

os.environ["STORAGE_BACKEND"] = "local"


def _load_audit_module():
    """Import the hyphenated script by path (module name has hyphens)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "role_multiplier_coverage_audit",
        CORE_SCRIPTS / "role-multiplier-coverage-audit.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed_shard(rows_dir: Path, name: str, row: dict) -> None:
    import yaml
    lines = [f"{k}: {v!r}\n" if not isinstance(v, str) else f'{k}: "{v}"\n'
             for k, v in row.items()]
    rows_dir.mkdir(parents=True, exist_ok=True)
    (rows_dir / f"{name}.yaml").write_text(yaml.safe_dump(row), encoding="utf-8")


def _seed_meta(meta_dir: Path, multiplier_agents: list[str]) -> None:
    import yaml
    meta_dir.mkdir(parents=True, exist_ok=True)
    arm = {a: {"framework": 1.0} for a in multiplier_agents}
    (meta_dir / "goal-selection-strategy.yaml").write_text(
        yaml.safe_dump({"agent_role_multipliers": arm}), encoding="utf-8")


def main() -> int:
    mod = _load_audit_module()
    tmp = Path(tempfile.mkdtemp(prefix="role-mult-audit-test-"))
    failed: list[str] = []
    try:
        world = tmp / "world"
        meta = tmp / "meta"
        rows = world / "team-state" / "agents"

        # Active agents: alpha, foxtrot. Retired: charlie (retired_at newer than
        # its last_active -> excluded from active roster).
        _seed_shard(rows, "alpha", {"last_active": "2026-07-22T00:00:00"})
        _seed_shard(rows, "foxtrot", {"last_active": "2026-07-22T00:00:00"})
        _seed_shard(rows, "charlie", {
            "retired": True,
            "retired_at": "2026-07-10T00:00:00",
            "last_active": "2026-07-08T00:00:00",
        })

        # Case 1: all active covered; charlie (retired) present -> stale advisory.
        _seed_meta(meta, ["alpha", "foxtrot", "charlie"])
        r1 = mod.audit(world, meta)
        if not r1["ok"]:
            failed.append(f"case1: expected ok=True, got {r1}")
        if r1["missing"]:
            failed.append(f"case1: expected missing=[], got {r1['missing']}")
        if r1["stale"] != ["charlie"]:
            failed.append(f"case1: expected stale=[charlie], got {r1['stale']}")
        if sorted(r1["active_agents"]) != ["alpha", "foxtrot"]:
            failed.append(f"case1: active roster wrong: {r1['active_agents']}")

        # Case 2: foxtrot ACTIVE but MISSING from multipliers -> blocking.
        _seed_meta(meta, ["alpha", "charlie"])
        r2 = mod.audit(world, meta)
        if r2["ok"]:
            failed.append(f"case2: expected ok=False (foxtrot missing), got {r2}")
        if r2["missing"] != ["foxtrot"]:
            failed.append(f"case2: expected missing=[foxtrot], got {r2['missing']}")

        # Case 3: retired charlie absent from multipliers -> NOT counted missing.
        _seed_meta(meta, ["alpha", "foxtrot"])
        r3 = mod.audit(world, meta)
        if not r3["ok"]:
            failed.append(f"case3: retired-agent absence must not fail: {r3}")
        if "charlie" in r3["missing"]:
            failed.append(f"case3: retired charlie wrongly flagged missing: {r3['missing']}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failed:
        for f in failed:
            print("FAIL:", f)
        return 1
    print("PASS: role_multiplier coverage audit (covered / missing-active / retired-excluded)")
    return 0


def test_role_multiplier_coverage_audit():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
