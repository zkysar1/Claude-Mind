"""Roster-from-team-state for backfill-completed-by._discover_agents
(g-303-21, zeta allowlist audit site 1a).

backfill-completed-by.py:45 formerly hardcoded AGENTS = a 6-agent roster; the
audit flagged it rot-risk HIGH -- a 7th agent silently drops from the backfill
recon, and the agents/omni/ system-identity dir (a local-paths.conf with no
journal/self.md, absent from team-state) was a latent mismatch the hardcoded
list papered over. The path-(i) fix derives the roster dynamically from
world/team-state.yaml agent_status (the SSOT the audit named).

These are the per-site synthetic divergence tests: the roster IS the team-state
keys, a NEW agent is auto-included (the rot the fix removes), a non-team-state
identity is excluded, and a missing/empty team-state fail-safes to an empty
roster (so the backfill no-ops with a visible 0-count rather than stamping
against a stale list).

Hyphenated module name -> importlib load (no daemon, no WORLD_DIR -- hermetic).
"""
import importlib.util
from pathlib import Path

import yaml

_MOD = Path(__file__).resolve().parent.parent / "backfill-completed-by.py"
_spec = importlib.util.spec_from_file_location("backfill_completed_by", _MOD)
bc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bc)


def _write_team_state(world_dir: Path, agent_names) -> None:
    ts = {"agent_status": {n: {"last_active": "2026-06-25T00:00:00"} for n in agent_names}}
    (world_dir / "team-state.yaml").write_text(yaml.safe_dump(ts), encoding="utf-8")


def test_roster_is_sorted_team_state_keys(tmp_path):
    _write_team_state(tmp_path, ["zeta", "alpha", "bravo"])
    assert bc._discover_agents(str(tmp_path)) == ["alpha", "bravo", "zeta"]


def test_new_agent_auto_included(tmp_path):
    """The rot the fix removes: a 7th agent appears WITHOUT a code edit."""
    _write_team_state(
        tmp_path, ["alpha", "bravo", "charlie", "delta", "echo", "zeta", "theta"]
    )
    assert "theta" in bc._discover_agents(str(tmp_path))


def test_non_team_state_identity_excluded(tmp_path):
    """agents/omni/ carries a local-paths.conf but is NOT a real agent and is
    absent from team-state -> excluded (the live-deployment mismatch this fix
    resolves, distinct from a directory scan that would re-admit it)."""
    _write_team_state(tmp_path, ["alpha", "bravo"])
    assert "omni" not in bc._discover_agents(str(tmp_path))


def test_missing_team_state_failsafes_to_empty(tmp_path):
    """No team-state.yaml -> empty roster: the backfill stamps nothing and
    reports a visible 0-count rather than acting on a stale hardcoded list."""
    assert bc._discover_agents(str(tmp_path)) == []


def test_empty_agent_status_is_empty(tmp_path):
    (tmp_path / "team-state.yaml").write_text("agent_status: {}\n", encoding="utf-8")
    assert bc._discover_agents(str(tmp_path)) == []


def test_absent_agent_status_key_is_empty(tmp_path):
    """team-state.yaml present but no agent_status key -> empty, not a crash."""
    (tmp_path / "team-state.yaml").write_text("other: 1\n", encoding="utf-8")
    assert bc._discover_agents(str(tmp_path)) == []
