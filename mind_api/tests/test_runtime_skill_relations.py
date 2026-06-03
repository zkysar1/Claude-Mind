"""GET/POST /v1/skill-relations/{read,add,co-invoke,discover}.

Two layers:
  1. HTTP round-trip (running_daemon): routes wired, read merge, POST add +
     read-back, co-invoke, discover, validation -> 400.
  2. Byte-compat (direct handler vs the REAL CLI skill-relations.py): response
     body == CLI stdout; written world/skill-relations.yaml (DEFAULT yaml.Dumper,
     raw tmp+os.replace, no history/changelog) matches the CLI's, with co-invoke's
     now()-stamped date normalised.

Both CLI and daemon read the REAL core/config/skill-relations.yaml (base
relations + config) — CLI via CONFIG_DIR (cwd=REPO_ROOT), daemon via
ctx.paths.project_root=REPO_ROOT — and a temp WORLD via MIND_WORLD /
ctx.paths.world. Test skill names are synthetic (test-skill-*) so they cannot
collide with real base compose_with relations in the discover-exclusion set.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_REL_PY = REPO_ROOT / "core" / "scripts" / "skill-relations.py"


def _seed_world(world: Path, data=None) -> Path:
    """Write world/skill-relations.yaml (default Dumper, matching write_yaml)."""
    world.mkdir(parents=True, exist_ok=True)
    p = world / "skill-relations.yaml"
    if data is not None:
        p.write_text(
            yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
    return p


def _run_cli(world: Path, args, stdin_input=None, agent="alpha", check_rc=True):
    env = dict(os.environ)
    env["MIND_WORLD"] = str(world)
    env["MIND_META"] = str(world.parent / "meta")
    env["MIND_AGENT"] = agent
    env["MIND_AGENT_DIR"] = str(world.parent / "agents" / agent)
    proc = subprocess.run(
        [sys.executable, str(SKILL_REL_PY), *args],
        text=True, encoding="utf-8", env=env, cwd=str(REPO_ROOT),
        input=stdin_input, capture_output=True, timeout=60,
    )
    if check_rc:
        assert proc.returncode == 0, (
            f"CLI skill-relations.py failed (rc={proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return proc


class _FakePaths:
    def __init__(self, world: Path):
        self.world = world
        self.project_root = REPO_ROOT


class _FakeCtx:
    def __init__(self, world: Path, query=None, headers=None, body=None):
        self.paths = _FakePaths(world)
        self.query = query or {}
        self.headers = headers if headers is not None else {}
        self.body = body


_DATE_LINE = re.compile(r"^(\s*date:\s*).*$", re.MULTILINE)


def _norm_world_text(world: Path) -> str:
    return _DATE_LINE.sub(r"\1<TS>", (world / "skill-relations.yaml").read_text(encoding="utf-8"))


def _http(port, method, path, query=None, body=None):
    qs = ("?" + urllib.parse.urlencode(query)) if query else ""
    url = f"http://127.0.0.1:{port}{path}{qs}"
    data = body.encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, method=method, data=data)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


# ---------------------------------------------------------------------------
# HTTP round-trip
# ---------------------------------------------------------------------------

def test_read_merges_base(running_daemon):
    # conftest doesn't seed world/skill-relations.yaml -> reads only real base.
    _, port = running_daemon
    status, body = _http(port, "GET", "/v1/skill-relations/read")
    assert status == 200
    assert isinstance(json.loads(body), list)


def test_add_then_read(running_daemon):
    project_root, port = running_daemon
    rel = {"source": "test-skill-a", "target": "test-skill-b", "type": "compose_with",
           "confidence": 0.9}
    status, body = _http(port, "POST", "/v1/skill-relations/add", body=json.dumps(rel))
    assert status == 200
    assert body == "Added relation: test-skill-a --compose_with--> test-skill-b\n"
    status, body = _http(port, "GET", "/v1/skill-relations/read",
                         {"skill": "test-skill-a"})
    assert status == 200
    recs = json.loads(body)
    assert any(r["source"] == "test-skill-a" for r in recs)


def test_add_duplicate_400(running_daemon):
    _, port = running_daemon
    rel = {"source": "dup-x", "target": "dup-y", "type": "similar_to"}
    _http(port, "POST", "/v1/skill-relations/add", body=json.dumps(rel))
    try:
        _http(port, "POST", "/v1/skill-relations/add", body=json.dumps(rel))
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "duplicate_relation"
    else:
        raise AssertionError("expected 400 for duplicate relation")


def test_add_invalid_type_400(running_daemon):
    _, port = running_daemon
    rel = {"source": "a", "target": "b", "type": "bogus"}
    try:
        _http(port, "POST", "/v1/skill-relations/add", body=json.dumps(rel))
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "invalid_type"
    else:
        raise AssertionError("expected 400 for invalid type")


def test_co_invoke_then_discover(running_daemon):
    project_root, port = running_daemon
    for _ in range(5):
        _http(port, "POST", "/v1/skill-relations/co-invoke",
              {"goal": "g-1", "skills": "test-skill-p,test-skill-q"})
    status, body = _http(port, "GET", "/v1/skill-relations/discover")
    assert status == 200
    assert isinstance(json.loads(body), list)


def test_co_invoke_too_few_400(running_daemon):
    _, port = running_daemon
    try:
        _http(port, "POST", "/v1/skill-relations/co-invoke",
              {"goal": "g-1", "skills": "only-one"})
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "too_few_skills"
    else:
        raise AssertionError("expected 400 for <2 skills")


# ---------------------------------------------------------------------------
# Byte-compat
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not SKILL_REL_PY.exists(), reason="core/scripts/skill-relations.py missing")
class TestByteCompat:
    def _world(self, tmp_path, name, data=None):
        world = tmp_path / name
        _seed_world(world, data)
        return world

    def _read_check(self, world, cli_args, query, handler_name):
        from mind_api.src.world import skill_relations
        cli_out = _run_cli(world, cli_args).stdout
        resp = getattr(skill_relations, handler_name)(_FakeCtx(world, query))
        assert resp.body.decode("utf-8") == cli_out

    def test_read_all(self, tmp_path):
        data = {"forged_relations": [
            {"source": "fa", "target": "fb", "type": "compose_with", "confidence": 0.7}]}
        self._read_check(self._world(tmp_path, "w", data), ["read"], {}, "read")

    def test_read_filter_skill(self, tmp_path):
        data = {"forged_relations": [
            {"source": "fa", "target": "fb", "type": "compose_with"},
            {"source": "fc", "target": "fa", "type": "similar_to"}]}
        self._read_check(self._world(tmp_path, "w", data),
                         ["read", "--skill", "fa"], {"skill": "fa"}, "read")

    def test_read_filter_type(self, tmp_path):
        data = {"forged_relations": [
            {"source": "fa", "target": "fb", "type": "compose_with"},
            {"source": "fc", "target": "fd", "type": "similar_to"}]}
        self._read_check(self._world(tmp_path, "w", data),
                         ["read", "--type", "similar_to"], {"type": "similar_to"}, "read")

    def test_read_empty_world(self, tmp_path):
        # No world file -> only real base relations; CLI and daemon both read it.
        self._read_check(self._world(tmp_path, "w"), ["read"], {}, "read")

    def test_add_appends(self, tmp_path):
        from mind_api.src.world import skill_relations
        seed = {"forged_relations": [
            {"source": "existing", "target": "thing", "type": "depend_on"}],
            "co_invocation_log": [{"goal_id": "g-0", "skills": ["x", "y"],
                                   "date": "2026-05-20T10:00:00"}]}
        cli_w = self._world(tmp_path, "cli", dict(seed))
        dmn_w = self._world(tmp_path, "dmn", dict(seed))
        rel = {"source": "test-skill-a", "target": "test-skill-b",
               "type": "compose_with", "confidence": 0.8, "evidence": "exp"}
        cli_out = _run_cli(cli_w, ["add"], stdin_input=json.dumps(rel)).stdout
        resp = skill_relations.add(_FakeCtx(dmn_w, body=json.dumps(rel).encode("utf-8")))
        assert resp.body.decode("utf-8") == cli_out
        assert cli_out == "Added relation: test-skill-a --compose_with--> test-skill-b\n"
        # No timestamp in add -> world file byte-identical (default Dumper both).
        assert (dmn_w / "skill-relations.yaml").read_bytes() == \
               (cli_w / "skill-relations.yaml").read_bytes()
        # No history/changelog side-effects on either.
        assert not (dmn_w / ".history").exists()
        assert not (dmn_w / "changelog.jsonl").exists()

    def test_co_invoke(self, tmp_path):
        from mind_api.src.world import skill_relations
        seed = {"forged_relations": []}
        cli_w = self._world(tmp_path, "cli", dict(seed))
        dmn_w = self._world(tmp_path, "dmn", dict(seed))
        cli_out = _run_cli(cli_w, [
            "co-invoke", "--goal", "g-7", "--skills", "sk-a, sk-b ,sk-c"]).stdout
        resp = skill_relations.co_invoke(_FakeCtx(dmn_w, {
            "goal": "g-7", "skills": "sk-a, sk-b ,sk-c"}))
        assert resp.body.decode("utf-8") == cli_out
        assert cli_out == "Logged co-invocation: 3 skills for goal g-7\n"
        # world file matches modulo the now()-stamped date.
        assert _norm_world_text(dmn_w) == _norm_world_text(cli_w)
        dmn_data = yaml.safe_load((dmn_w / "skill-relations.yaml").read_text(encoding="utf-8"))
        assert dmn_data["co_invocation_log"][-1]["skills"] == ["sk-a", "sk-b", "sk-c"]

    def test_discover(self, tmp_path):
        # 5 co-invocations of the same synthetic pair -> exceeds default min_co=3.
        log = [{"goal_id": f"g-{i}", "skills": ["test-skill-p", "test-skill-q"],
                "date": f"2026-05-20T10:{i:02d}:00"} for i in range(5)]
        data = {"co_invocation_log": log}
        self._read_check(self._world(tmp_path, "w", data), ["discover"], {}, "discover")

    def test_discover_empty(self, tmp_path):
        self._read_check(self._world(tmp_path, "w", {"co_invocation_log": []}),
                         ["discover"], {}, "discover")
