"""test_team_state_retire.py —  sanctioned agent-row retirement.

g-115-1909 root-caused a capability gap: team-state had NO way to REMOVE an
agent_status row. route_field sends agent_status.<name> to a per-agent SHARD
(missing for un-sharded legacy agents); the generic --operation remove /
_remove_nested only drops LIST items (agent_status is a DICT -> no-op); the
daemon whole-row path refuses remove; there was no retire subcommand. So a
user-retired agent's core-residual row (charlie/delta after the 2026-07-07
foxtrot merge) could only be removed by forbidden raw-YAML surgery.

retire_agent is that missing op — archive-before-delete gated
(ENUMERATE -> ARCHIVE to world/team-state/.graveyard/ -> VERIFY -> DELETE
core-key + shard -> RECEIPT). One implementation in _team_state, called by
BOTH the CLI (team-state.py retire-agent) and the daemon
(team_state_write.py) — guard-742 parity by construction.

Covers:
  - enumerate_retire: core-only / shard-only / both / neither(present=False)
  - retire_agent: core-only pop, shard-only unlink, both, idempotent no-op
  - archive-before-delete: graveyard file written + content matches enumeration
  - archive NOT globbed as a shard (graveyard is outside team-state/agents)
  - dry_run reports without archiving or deleting
  - hostile agent name rejected
  - CLI subprocess: retire-agent removes a core residual; composed read
    excludes the retired agent; other agents + shared fields preserved
  - CLI dry-run leaves state intact

CLI exercised via subprocess against an ISOLATED tmp world (MIND_WORLD
override — never touches the live team-state).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

import yaml  # noqa: E402

from _team_state import (  # noqa: E402
    compose_state,
    enumerate_belief_sweep,
    enumerate_retire,
    graveyard_dir,
    retire_agent,
    row_agent_names,
    row_path,
    rows_dir,
)

TEAM_STATE_PY = CORE_SCRIPTS / "team-state.py"

NOW = "2026-07-11T12:00:00"


def _run(world: Path, agent: str, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["MIND_AGENT"] = agent
    env["MIND_WORLD"] = str(world)
    cmd = [sys.executable, str(TEAM_STATE_PY), *args]
    return subprocess.run(cmd, capture_output=True, text=True, env=env,
                          timeout=60)


def _load(p: Path) -> dict:
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _seed_core(world: Path, agent_status: dict, **shared) -> Path:
    """Write a core team-state.yaml with the given agent_status + shared fields."""
    core = world / "team-state.yaml"
    doc = {"agent_status": dict(agent_status)}
    doc.update(shared)
    core.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return core


def _seed_shard(world: Path, agent: str, row: dict) -> Path:
    rows_dir(world).mkdir(parents=True, exist_ok=True)
    rp = row_path(world, agent)
    rp.write_text(yaml.safe_dump(row), encoding="utf-8")
    return rp


# --- unit: enumerate_retire --------------------------------------------------

def test_enumerate_core_only():
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd)
        core = _seed_core(world, {"charlie": {"last_active": "2026-07-03T09:51:06"}})
        plan = enumerate_retire(world, core, "charlie")
        assert plan["present"] is True
        assert plan["core_residual"] == {"last_active": "2026-07-03T09:51:06"}
        assert plan["row_content"] is None


def test_enumerate_shard_only():
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd)
        core = _seed_core(world, {})
        _seed_shard(world, "foxtrot", {"last_active": "2026-07-08T00:00:00"})
        plan = enumerate_retire(world, core, "foxtrot")
        assert plan["present"] is True
        assert plan["core_residual"] is None
        assert plan["row_content"]["last_active"] == "2026-07-08T00:00:00"


def test_enumerate_both():
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd)
        core = _seed_core(world, {"alpha": {"last_active": "2026-07-01T00:00:00"}})
        _seed_shard(world, "alpha", {"last_active": "2026-07-11T00:00:00"})
        plan = enumerate_retire(world, core, "alpha")
        assert plan["present"] is True
        assert plan["core_residual"] is not None
        assert plan["row_content"] is not None


def test_enumerate_absent_is_not_present():
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd)
        core = _seed_core(world, {"alpha": {"last_active": "x"}})
        plan = enumerate_retire(world, core, "ghost")
        assert plan["present"] is False


# --- unit: retire_agent ------------------------------------------------------

def test_retire_core_only_pops_key_and_archives():
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd)
        core = _seed_core(
            world,
            {"charlie": {"last_active": "2026-07-03T09:51:06", "current_focus": "x"},
             "alpha": {"last_active": "2026-07-11T00:00:00"}},
            strategic_focus={"primary": "keep-me"})
        result = retire_agent(world, core, "charlie", "alpha", NOW,
                              source="g-115-1965-test")
        assert result["ok"] is True and result["removed"] is True
        assert result["removed_core_residual"] is True
        assert result["removed_shard"] is False
        # Core key popped; other agent + shared field preserved.
        after = _load(core)
        assert "charlie" not in after["agent_status"]
        assert "alpha" in after["agent_status"]
        assert after["strategic_focus"]["primary"] == "keep-me"
        assert after["last_updated"] == NOW
        assert after["last_updated_by"] == "alpha"
        # Archive written + content matches enumeration.
        arch = Path(result["archive"])
        assert arch.exists()
        payload = _load(arch)
        assert payload["agent"] == "charlie"
        assert payload["core_residual"]["current_focus"] == "x"
        assert payload["source"] == "g-115-1965-test"
        assert payload["retired_by"] == "alpha"


def test_retire_shard_only_unlinks_and_leaves_core():
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd)
        core = _seed_core(world, {"alpha": {"last_active": "2026-07-11T00:00:00"}})
        core_before = _load(core)
        _seed_shard(world, "foxtrot", {"last_active": "2026-07-08T00:00:00"})
        result = retire_agent(world, core, "foxtrot", "alpha", NOW)
        assert result["removed"] is True
        assert result["removed_shard"] is True
        assert result["removed_core_residual"] is False
        assert not row_path(world, "foxtrot").exists()
        # Core file untouched (no core residual for foxtrot).
        assert _load(core) == core_before


def test_retire_both_stores():
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd)
        core = _seed_core(world, {"alpha": {"last_active": "2026-07-01T00:00:00"}})
        _seed_shard(world, "alpha", {"last_active": "2026-07-11T00:00:00"})
        result = retire_agent(world, core, "alpha", "bravo", NOW)
        assert result["removed"] is True
        assert result["removed_core_residual"] is True
        assert result["removed_shard"] is True
        assert "alpha" not in _load(core)["agent_status"]
        assert not row_path(world, "alpha").exists()


def test_retire_absent_agent_is_idempotent_noop():
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd)
        core = _seed_core(world, {"alpha": {"last_active": "x"}})
        result = retire_agent(world, core, "ghost", "alpha", NOW)
        assert result["ok"] is True
        assert result["removed"] is False
        assert result["reason"] == "not_present"
        # Nothing archived, nothing deleted.
        assert not graveyard_dir(world).exists() or not list(graveyard_dir(world).iterdir())
        assert "alpha" in _load(core)["agent_status"]


def test_retire_second_run_is_noop():
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd)
        core = _seed_core(world, {"charlie": {"last_active": "2026-07-03T09:51:06"}})
        r1 = retire_agent(world, core, "charlie", "alpha", NOW)
        assert r1["removed"] is True
        r2 = retire_agent(world, core, "charlie", "alpha", "2026-07-11T12:00:01")
        assert r2["removed"] is False
        assert r2["reason"] == "not_present"


def test_archive_not_globbed_as_shard():
    """The graveyard must live OUTSIDE team-state/agents so load_rows'
    shard glob never resurrects a retired agent as a live row."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd)
        core = _seed_core(world, {"alpha": {"last_active": "2026-07-11T00:00:00"}})
        _seed_shard(world, "delta", {"last_active": "2026-07-03T00:00:00"})
        retire_agent(world, core, "delta", "alpha", NOW)
        # Graveyard file exists but is NOT under team-state/agents.
        gfiles = list(graveyard_dir(world).iterdir())
        assert len(gfiles) == 1
        assert graveyard_dir(world) != rows_dir(world)
        assert "delta" not in row_agent_names(world)


def test_dry_run_reports_without_mutating():
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd)
        core = _seed_core(world, {"charlie": {"last_active": "2026-07-03T09:51:06"}})
        _seed_shard(world, "charlie", {"last_active": "2026-07-03T10:00:00"})
        result = retire_agent(world, core, "charlie", "alpha", NOW, dry_run=True)
        assert result["removed"] is False and result["dry_run"] is True
        assert result["would_remove"]["core_residual"] is True
        assert result["would_remove"]["shard"] is True
        # Nothing changed on disk.
        assert "charlie" in _load(core)["agent_status"]
        assert row_path(world, "charlie").exists()
        assert not graveyard_dir(world).exists() or not list(graveyard_dir(world).iterdir())


def test_retire_rejects_hostile_agent_name():
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd)
        core = _seed_core(world, {})
        for bad in ("", ".", "..", "a/b", "a\\b"):
            try:
                retire_agent(world, core, bad, "alpha", NOW)
                raise AssertionError(f"retire_agent accepted {bad!r}")
            except ValueError:
                pass


# --- belief sweep () -----------------------------------------------

def _belief(about, text="stale", conf=0.5):
    return {"about": about, "belief": text, "confidence": conf,
            "last_observed": "2026-06-25T22:24:38", "domain": None,
            "valid_from": "2026-06-25T22:24:38", "valid_to": None}


def test_enumerate_belief_sweep_finds_core_and_shard_holders():
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd)
        core = _seed_core(world, {
            "echo": {"last_active": "x", "beliefs": [_belief("delta"),
                                                     _belief("alpha")]},
            "zeta": {"last_active": "x", "beliefs": [_belief("delta")]},
            "alpha": {"last_active": "x"}})   # no beliefs
        _seed_shard(world, "bravo", {"last_active": "y",
                                     "beliefs": [_belief("delta"), _belief("echo")]})
        sweep = enumerate_belief_sweep(world, core, "delta")
        assert set(sweep["core"]) == {"echo", "zeta"}
        assert len(sweep["core"]["echo"]) == 1  # only the about:delta entry
        assert set(sweep["shards"]) == {"bravo"}
        assert len(sweep["shards"]["bravo"]) == 1


def test_enumerate_belief_sweep_empty_when_no_holders():
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd)
        core = _seed_core(world, {"alpha": {"last_active": "x"}})
        assert enumerate_belief_sweep(world, core, "delta") == {"core": {}, "shards": {}}


def test_retire_sweeps_core_and_shard_beliefs_about_retiree():
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd)
        core = _seed_core(world, {
            "delta": {"last_active": "2026-07-03T00:00:00"},   # retiree present
            "echo": {"last_active": "x", "beliefs": [_belief("delta"),
                                                     _belief("alpha")]},
            "alpha": {"last_active": "x"}})
        _seed_shard(world, "bravo", {"last_active": "y", "beliefs": [_belief("delta")]})
        result = retire_agent(world, core, "delta", "alpha", NOW)
        assert result["removed"] is True
        assert result["beliefs_swept"]["core"] == {"echo": 1}
        assert result["beliefs_swept"]["shards"] == {"bravo": 1}
        after = _load(core)
        assert "delta" not in after["agent_status"]
        # echo keeps its about:alpha belief, loses about:delta.
        assert [b["about"] for b in after["agent_status"]["echo"]["beliefs"]] == ["alpha"]
        # bravo shard swept but NOT unlinked.
        assert row_path(world, "bravo").exists()
        assert _load(row_path(world, "bravo"))["beliefs"] == []


def test_retire_not_present_but_beliefs_linger_still_sweeps():
    """The delta case (): the retiree's OWN row is already gone
    (g-115-1965 removed it) but partner-beliefs about it linger. A re-run of
    retire must still sweep them — the not_present guard is belief-aware."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd)
        core = _seed_core(world, {
            "echo": {"last_active": "x", "beliefs": [_belief("delta")]},
            "zeta": {"last_active": "x", "beliefs": [_belief("delta")]}})
        result = retire_agent(world, core, "delta", "alpha", NOW)
        assert result["removed"] is True          # beliefs were removed
        assert result["removed_core_residual"] is False
        assert result["removed_shard"] is False
        assert result["beliefs_swept"]["core"] == {"echo": 1, "zeta": 1}
        after = _load(core)
        assert after["agent_status"]["echo"]["beliefs"] == []
        assert after["agent_status"]["zeta"]["beliefs"] == []
        # The sweep is archived (archive-before-delete).
        payload = _load(Path(result["archive"]))
        assert set(payload["swept_beliefs"]["core"]) == {"echo", "zeta"}


def test_retire_absent_and_no_beliefs_is_noop():
    """Retiree absent AND no beliefs about it → not_present no-op, no archive,
    other agents' beliefs untouched."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd)
        core = _seed_core(world, {"alpha": {"last_active": "x",
                                            "beliefs": [_belief("bravo")]}})
        result = retire_agent(world, core, "delta", "alpha", NOW)
        assert result["removed"] is False
        assert result["reason"] == "not_present"
        assert _load(core)["agent_status"]["alpha"]["beliefs"][0]["about"] == "bravo"
        assert not graveyard_dir(world).exists() or not list(graveyard_dir(world).iterdir())


def test_dry_run_reports_belief_sweep_without_mutating():
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd)
        core = _seed_core(world, {"echo": {"last_active": "x",
                                           "beliefs": [_belief("delta")]}})
        result = retire_agent(world, core, "delta", "alpha", NOW, dry_run=True)
        assert result["dry_run"] is True
        assert result["would_remove"]["beliefs"]["core"] == {"echo": 1}
        assert _load(core)["agent_status"]["echo"]["beliefs"][0]["about"] == "delta"


# --- CLI subprocess ----------------------------------------------------------

def test_cli_retire_removes_core_residual_from_composed_read():
    """The  acceptance criterion: a retired agent disappears from
    the COMPOSED team-state read (what every partner/selector actually sees)."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd)
        _seed_core(
            world,
            {"charlie": {"last_active": "2026-07-03T09:51:06"},
             "delta": {"last_active": "2026-07-03T09:42:25"},
             "alpha": {"last_active": "2026-07-11T00:00:00"}},
            strategic_focus={"primary": "keep-me"})
        r = _run(world, "alpha", "retire-agent", "--agent", "charlie",
                 "--source", "g-115-1965")
        assert r.returncode == 0, r.stderr
        out = json.loads(r.stdout)
        assert out["removed"] is True and out["removed_core_residual"] is True
        # Composed read no longer surfaces charlie; delta + alpha remain.
        composed = compose_state(_load(world / "team-state.yaml"), world)
        assert "charlie" not in composed["agent_status"]
        assert "delta" in composed["agent_status"]
        assert "alpha" in composed["agent_status"]
        assert composed["strategic_focus"]["primary"] == "keep-me"


def test_cli_dry_run_leaves_state_intact():
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd)
        _seed_core(world, {"charlie": {"last_active": "2026-07-03T09:51:06"}})
        r = _run(world, "alpha", "retire-agent", "--agent", "charlie", "--dry-run")
        assert r.returncode == 0, r.stderr
        out = json.loads(r.stdout)
        assert out["dry_run"] is True and out["removed"] is False
        composed = compose_state(_load(world / "team-state.yaml"), world)
        assert "charlie" in composed["agent_status"]


def test_cli_retire_missing_agent_arg_errors():
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd)
        _seed_core(world, {})
        r = _run(world, "alpha", "retire-agent")
        assert r.returncode != 0  # argparse: --agent required


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("ALL OK")
