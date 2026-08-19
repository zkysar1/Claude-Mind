"""xw_origin.resolve — one canonical `<agent>@<env-id>` peer address out of the
four provenance shapes live records carry (g-361-03, goal-completion audit).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import xw_origin  # noqa: E402

ENVS = {"ayoai-mind": ["alpha", "bravo", "echo", "foxtrot", "zeta"],
        "zds-mind": ["omni", "zeta"], "claude-mind": [], "local": []}
ME = "ayoai-mind"


def _r(goal, asp=None, roster=("alpha", "bravo", "echo", "foxtrot", "zeta")):
    return xw_origin.resolve(goal, asp, envs=ENVS, me=ME, local_roster=list(roster))


def test_canonical_on_goal():
    hit = _r({"id": "g-115-2888", "cross_world_origin": "omni@zds-mind"})
    assert hit == {"agent": "omni", "env": "zds-mind", "address": "omni@zds-mind",
                   "shape": "canonical", "field": "goal.cross_world_origin"}


def test_legacy_slash_shape_folds_to_canonical():
    hit = _r({"id": "g-1", "injected_by": "zds-mind/omni"})
    assert hit["address"] == "omni@zds-mind" and hit["shape"] == "slash"


def test_env_only_shape_yields_env_but_no_address():
    hit = _r({"id": "g-1", "cross_world_origin": "zds-mind"})
    assert hit["env"] == "zds-mind" and hit["agent"] is None and hit["address"] is None
    assert hit["shape"] == "env-only"


def test_env_only_origin_defers_to_a_sibling_field_that_names_the_agent():
    #  shape: cross_world_origin=zds-mind (env-only) beside injected_by=omni@zds-mind
    hit = _r({"id": "g-115-4228", "cross_world_origin": "zds-mind",
              "injected_by": "omni@zds-mind", "filed_by_agent": "omni"})
    assert hit["address"] == "omni@zds-mind" and hit["field"] == "goal.injected_by"
    # and through filed_by when no sibling field carries the agent
    hit = _r({"id": "g-1", "cross_world_origin": "zds-mind", "filed_by_agent": "omni"})
    assert hit["address"] == "omni@zds-mind" and hit["shape"] == "filed_by"


def test_asp_xw_shape_reads_the_aspiration():
    goal = {"id": "g-xw-20260805T172222-01", "injected_by": "omni@zds-mind"}
    asp = {"id": "asp-xw-20260805T172222", "cross_world_origin": "omni@zds-mind"}
    hit = _r({"id": "g-xw-20260805T172222-01"}, asp)
    assert hit["address"] == "omni@zds-mind" and hit["field"] == "aspiration.cross_world_origin"
    assert _r(goal, asp)["field"] == "goal.injected_by"


def test_peer_filed_goal_resolves_through_the_registry():
    #  shape: filed BY omni, no cross_world_origin at all
    hit = _r({"id": "g-335-916", "filed_by_agent": "omni"})
    assert hit == {"agent": "omni", "env": "zds-mind", "address": "omni@zds-mind",
                   "shape": "filed_by", "field": "goal.filed_by_agent"}


def test_collision_or_local_filer_never_guesses_a_peer():
    # zeta is on BOTH rosters -> ambiguous -> no peer origin
    assert _r({"id": "g-1", "filed_by_agent": "zeta"}) is None
    # alpha is local -> not a peer
    assert _r({"id": "g-1", "filed_by_agent": "alpha"}) is None
    # a name in no registry -> nothing
    assert _r({"id": "g-1", "filed_by_agent": "nobody"}) is None


def test_this_worlds_own_env_and_unregistered_envs_are_not_peers():
    assert _r({"id": "g-1", "cross_world_origin": "alpha@ayoai-mind"}) is None
    assert _r({"id": "g-1", "cross_world_origin": "omni@mars-mind"}) is None
    assert _r({"id": "g-1"}) is None


def test_registry_reads_known_agents_from_the_environments_dir(tmp_path):
    (tmp_path / "zds-mind.yaml").write_text(
        "environment_id: zds-mind\nbackend: local\n# c\nknown_agents:\n  - omni\n  - zeta\n",
        encoding="utf-8")
    (tmp_path / "claude-mind.yaml").write_text("environment_id: claude-mind\n", encoding="utf-8")
    (tmp_path / "flat.yaml").write_text("environment_id: flat-mind\nknown_agents: [a, b]\n", encoding="utf-8")
    reg = xw_origin.registry(tmp_path)
    assert reg == {"zds-mind": ["omni", "zeta"], "claude-mind": [], "flat-mind": ["a", "b"]}


def test_live_registry_knows_the_peer_deployments():
    reg = xw_origin.registry()
    assert "zds-mind" in reg and "omni" in reg["zds-mind"]


def test_cli_prints_json_for_a_peer_goal_and_nothing_otherwise(tmp_path, monkeypatch):
    store = tmp_path / "aspirations.jsonl"
    store.write_text(json.dumps({
        "id": "asp-xw-20260805T172222", "cross_world_origin": "omni@zds-mind",
        "goals": [{"id": "g-xw-20260805T172222-01", "status": "pending"}],
    }) + "\n" + json.dumps({
        "id": "asp-115", "goals": [{"id": "g-115-01", "status": "pending"}],
    }) + "\n", encoding="utf-8")
    env = dict(**{"ENVIRONMENT_ID": "ayoai-mind"})
    import os
    env = {**os.environ, "ENVIRONMENT_ID": "ayoai-mind"}
    out = subprocess.run([sys.executable, str(SCRIPTS / "xw_origin.py"), "--goal",
                          "g-xw-20260805T172222-01", "--file", str(store)],
                         capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["address"] == "omni@zds-mind"
    out = subprocess.run([sys.executable, str(SCRIPTS / "xw_origin.py"), "--goal",
                          "g-115-01", "--file", str(store)],
                         capture_output=True, text=True, env=env)
    assert out.returncode == 0 and out.stdout.strip() == ""
    out = subprocess.run([sys.executable, str(SCRIPTS / "xw_origin.py"), "--goal",
                          "g-none", "--file", str(store)],
                         capture_output=True, text=True, env=env)
    assert out.returncode == 0 and out.stdout.strip() == ""
