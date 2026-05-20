"""test_post_state_update_attribution.py — regression test for .

Exercises core/scripts/_cross_agent_attribution_filter.py — the helper
wired into post-state-update-gate.sh:122 — so the canonical incident it
prevents (bravo session 69, 2026-05-13T18:16, fired with 4 zeta files)
cannot silently recur.

Restored under g-115-741 alongside the helper; 5 cases reconstructed
from the cpython-312 test bytecode (same case names + scenarios).
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from _cross_agent_attribution_filter import filter_paths  # noqa: E402


# ── Test staging helpers ────────────────────────────────────────────────────


def _stage_project(tmp_path, self_agent, partners):
    """Build a minimal PROJECT_ROOT with agent dirs + world dir.

    Phase 2.5.D layout: agent dirs live under PROJECT_ROOT/agents/<name>/.
    Returns (project_root, world_dir).
    """
    project_root = tmp_path / "repo"
    world = tmp_path / "world"
    world.mkdir(parents=True, exist_ok=True)
    for agent in [self_agent, *partners]:
        adir = project_root / "agents" / agent
        adir.mkdir(parents=True, exist_ok=True)
        (adir / "local-paths.conf").write_text(
            "WORLD_PATH=" + world.as_posix() + "\n", encoding="utf-8"
        )
        (adir / "session").mkdir(parents=True, exist_ok=True)
    return project_root, world


def _write_team_state(world, agent_status):
    import yaml

    (world / "team-state.yaml").write_text(
        yaml.safe_dump({"agent_status": agent_status}), encoding="utf-8"
    )


def _touch_file(repo_root, rel_path, mtime_epoch):
    full = repo_root / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("# test\n", encoding="utf-8")
    os.utime(full, (mtime_epoch, mtime_epoch))


def _write_uncommitted_log(agent_dir, recorded_paths):
    (agent_dir / "session" / "uncommitted-edits.jsonl").write_text(
        "\n".join(json.dumps({"file": p}) for p in recorded_paths),
        encoding="utf-8",
    )


def _epoch_to_iso(ep):
    return datetime.datetime.fromtimestamp(ep).isoformat()


# ── Cases ───────────────────────────────────────────────────────────────────


def test_canonical_incident_zeta_files_dropped(tmp_path):
    """Case 1: bravo session 69 reproduction — 4 zeta-attributed files."""
    base = 1700000000
    project_root, world = _stage_project(tmp_path, "bravo", ["alpha", "zeta"])
    _write_team_state(
        world,
        {
            "bravo": {"in_flight": {"goal_id": "g-bravo", "claimed_at": _epoch_to_iso(base + 60)}},
            "zeta": {"in_flight": {"goal_id": "g-zeta", "claimed_at": _epoch_to_iso(base + 10)}},
        },
    )
    zeta_files = [
        "core/scripts/zeta-a.py",
        "core/scripts/zeta-b.py",
        "core/config/zeta-c.yaml",
        "mind_api/src/zeta-d.py",
    ]
    for f in zeta_files:
        _touch_file(project_root, f, base + 30)  # after zeta claim, before bravo claim

    kept, dropped = filter_paths(zeta_files, "bravo", str(project_root), str(world))

    assert kept == [], f"Expected all zeta files dropped, got kept={kept}"
    assert len(dropped) == 4
    assert all(d["owner"] == "zeta" for d in dropped)
    assert all(d["reason"] == "concurrent-partner" for d in dropped)


def test_own_authored_files_retained(tmp_path):
    """Case 2: alpha agent's 4 self-authored files — all retained."""
    base = 1700000000
    project_root, world = _stage_project(tmp_path, "alpha", ["bravo", "zeta"])
    _write_team_state(
        world,
        {
            "alpha": {"in_flight": {"goal_id": "g-alpha", "claimed_at": _epoch_to_iso(base)}},
        },
    )
    self_files = [
        "core/scripts/alpha-1.py",
        "core/scripts/alpha-2.py",
        "core/config/alpha-3.yaml",
        "mind_api/src/alpha-4.py",
    ]
    for f in self_files:
        _touch_file(project_root, f, base + 30)  # after alpha's own claim

    kept, dropped = filter_paths(self_files, "alpha", str(project_root), str(world))

    assert sorted(kept) == sorted(self_files)
    assert dropped == []


def test_mixed_authorship(tmp_path):
    """Case 3: 2 self + 3 partner = 5 total, only 2 self retained."""
    base = 1700000000
    project_root, world = _stage_project(tmp_path, "alpha", ["bravo", "zeta"])
    _write_team_state(
        world,
        {
            "alpha": {"in_flight": {"goal_id": "g-alpha", "claimed_at": _epoch_to_iso(base + 120)}},
            "bravo": {"in_flight": {"goal_id": "g-bravo", "claimed_at": _epoch_to_iso(base + 10)}},
        },
    )
    self_files = ["core/scripts/file-a.py", "core/scripts/file-b.py"]
    for f in self_files:
        _touch_file(project_root, f, base + 150)  # after alpha claim → retained

    partner_concurrent = ["core/scripts/bravo-x.py", "core/config/bravo-y.yaml"]
    for f in partner_concurrent:
        _touch_file(project_root, f, base + 30)  # after bravo claim, before alpha claim

    partner_logged = "mind_api/src/bravo-z.py"
    _touch_file(project_root, partner_logged, base + 200)
    _write_uncommitted_log(project_root / "agents" / "bravo", [partner_logged])

    candidates = self_files + partner_concurrent + [partner_logged]
    kept, dropped = filter_paths(candidates, "alpha", str(project_root), str(world))

    assert sorted(kept) == sorted(self_files), f"Expected {self_files}, got {kept}"
    assert len(dropped) == 3
    dropped_paths = {d["path"] for d in dropped}
    assert dropped_paths == set(partner_concurrent + [partner_logged])


def test_fail_open_missing_team_state(tmp_path):
    """Case 4: world/team-state.yaml missing — all files retained."""
    base = 1700000000
    project_root, world = _stage_project(tmp_path, "alpha", ["bravo"])
    # Deliberately do NOT call _write_team_state — exercise fail-open.
    candidates = ["core/scripts/x.py", "core/scripts/y.py"]
    for f in candidates:
        _touch_file(project_root, f, base)

    kept, dropped = filter_paths(candidates, "alpha", str(project_root), str(world))

    assert sorted(kept) == sorted(candidates)
    assert dropped == []


def test_pre_claim_mtime_catches_partner_wip(tmp_path):
    """Case 5: file mtime predates self.claimed_at — dropped as pre-claim WIP."""
    base = 1700000000
    project_root, world = _stage_project(tmp_path, "alpha", ["bravo"])
    _write_team_state(
        world,
        {
            "alpha": {"in_flight": {"goal_id": "g-alpha", "claimed_at": _epoch_to_iso(base + 3600)}},
        },
    )
    pre_claim = ["core/scripts/partner-wip-1.py", "core/scripts/partner-wip-2.py"]
    for f in pre_claim:
        _touch_file(project_root, f, base)  # 3600s before alpha's claim

    own = "core/scripts/own-work.py"
    _touch_file(project_root, own, base + 3600 + 30)  # after alpha's claim

    candidates = pre_claim + [own]
    kept, dropped = filter_paths(candidates, "alpha", str(project_root), str(world))

    assert kept == [own], f"Expected only {[own]} retained, got {kept}"
    assert len(dropped) == 2
    assert all(d["reason"] == "pre-claim-mtime" for d in dropped)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
