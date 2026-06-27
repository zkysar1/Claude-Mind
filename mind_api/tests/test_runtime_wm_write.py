"""POST /v1/wm/{set,append,clear,prune,init,reset,clear-identity}, GET /v1/wm/ages.

Two layers:
  1. HTTP round-trip (running_daemon, conftest world): endpoints wired, the
     set/append/clear/prune/init/ages flows work end-to-end incl. the
     agent-header gate, structured-dict refusal, and knowledge_debt validation.
  2. Byte-compat (direct handler vs the REAL CLI wm.py): working-memory.yaml is
     byte-identical for a TOP-LEVEL key set (top-level writes skip slot_meta
     timestamping, so the file is fully deterministic). Both sides read the
     real core/config/memory-pipeline.yaml so _default_wm_data matches.

The CLI is redirected with MIND_AGENT_DIR (unit-test override) so it writes to
a temp agent dir, never the real one.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

REPO_ROOT = Path(__file__).resolve().parents[2]
WM_PY = REPO_ROOT / "core" / "scripts" / "wm.py"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _post(port, path, query, body=None, *, agent="alpha"):
    qs = ("?" + urllib.parse.urlencode(query)) if query else ""
    url = f"http://127.0.0.1:{port}{path}{qs}"
    data = (body or "").encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


def _get(port, path, query=None, *, agent="alpha"):
    qs = ("?" + urllib.parse.urlencode(query)) if query else ""
    url = f"http://127.0.0.1:{port}{path}{qs}"
    req = urllib.request.Request(url, method="GET")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


def _read_wm(agent_dir: Path) -> dict:
    p = agent_dir / "session" / "working-memory.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# HTTP round-trip tests (conftest world)
# ---------------------------------------------------------------------------

def test_set_slot_roundtrip(running_daemon):
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    status, body = _post(port, "/v1/wm/set", {"slot": "active_strategy"},
                         '"breadth-first"')
    assert status == 200, body
    wm = _read_wm(agent_dir)
    assert wm["slots"]["active_strategy"] == "breadth-first"
    assert wm["slot_meta"]["active_strategy"]["updated_at"]


def test_set_top_level_roundtrip(running_daemon):
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    status, body = _post(port, "/v1/wm/set", {"slot": "last_goal_category"},
                         "framework")
    assert status == 200, body
    assert _read_wm(agent_dir)["last_goal_category"] == "framework"


def test_set_requires_agent_header(running_daemon):
    _, port = running_daemon
    try:
        _post(port, "/v1/wm/set", {"slot": "active_strategy"}, '"x"', agent=None)
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "missing_agent_header"
    else:
        raise AssertionError("expected 400 without X-Mind-Agent")


def test_set_structured_dict_rejected(running_daemon):
    _, port = running_daemon
    try:
        _post(port, "/v1/wm/set", {"slot": "loop_state"}, '"a bare string"')
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "structured_dict_required"
    else:
        raise AssertionError("expected 400 for non-dict loop_state write")


def test_set_loop_state_dict_ok(running_daemon):
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    status, body = _post(port, "/v1/wm/set", {"slot": "loop_state"},
                         json.dumps({"signals": {"quiescence": False}}))
    assert status == 200, body
    assert _read_wm(agent_dir)["slots"]["loop_state"]["signals"]["quiescence"] is False


def test_append_roundtrip(running_daemon):
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    status, body = _post(port, "/v1/wm/append", {"slot": "known_blockers"},
                         json.dumps({"id": "blk-1", "reason": "waiting"}))
    assert status == 200, body
    arr = _read_wm(agent_dir)["slots"]["known_blockers"]
    assert arr[-1]["id"] == "blk-1"
    assert "_item_ts" in arr[-1]


def test_append_knowledge_debt_invalid_400(running_daemon):
    _, port = running_daemon
    try:
        _post(port, "/v1/wm/append", {"slot": "knowledge_debt"},
              json.dumps({"node_key": "no-such-node"}))
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "validation_failed"
    else:
        raise AssertionError("expected 400 for unresolvable knowledge_debt node_key")


def test_append_not_initialized_400(running_daemon):
    project_root, port = running_daemon
    # Use bravo, whose conftest dir has no working-memory.yaml.
    try:
        _post(port, "/v1/wm/append", {"slot": "known_blockers"},
              json.dumps({"id": "x"}), agent="bravo")
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "not_initialized"
    else:
        raise AssertionError("expected 400 appending to uninitialized WM")


def test_clear_roundtrip(running_daemon):
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    status, body = _post(port, "/v1/wm/clear", {"slot": "active_strategy"})
    assert status == 200, body
    assert _read_wm(agent_dir)["slots"]["active_strategy"] is None


def test_init_roundtrip(running_daemon):
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "bravo"
    status, body = _post(port, "/v1/wm/init", {}, agent="bravo")
    assert status == 200, body
    assert json.loads(body)["slots"] >= 1
    assert (agent_dir / "session" / "working-memory.yaml").exists()


def test_ages_roundtrip(running_daemon):
    _, port = running_daemon
    status, body = _get(port, "/v1/wm/ages")
    assert status == 200, body
    data = json.loads(body)
    assert "active_context" in data


def test_prune_dry_run(running_daemon):
    _, port = running_daemon
    status, body = _post(port, "/v1/wm/prune", {"dry_run": "1"})
    assert status == 200, body
    data = json.loads(body)
    assert data["dry_run"] is True
    assert "report" in data


# ---------------------------------------------------------------------------
# Byte-compat: daemon handler output == real CLI output
# ---------------------------------------------------------------------------

class _FakePaths:
    def __init__(self, agent: Path, project_root: Path, world: Path):
        self.agent = agent
        self.project_root = project_root
        self.world = world
        self.agent_name = "alpha"


class _FakeCtx:
    def __init__(self, agent: Path, project_root: Path, world: Path,
                 query: dict, body: bytes, *, agent_name="alpha"):
        self.paths = _FakePaths(agent, project_root, world)
        self.query = query
        self.body = body
        self.headers = {"x-mind-agent": agent_name}


def _run_wm_cli(world, meta, agent_dir, args, stdin_text):
    env = dict(os.environ)
    env["MIND_WORLD"] = str(world)
    env["MIND_META"] = str(meta)
    env["MIND_AGENT"] = "alpha"
    env["MIND_AGENT_DIR"] = str(agent_dir)
    world.mkdir(parents=True, exist_ok=True)
    meta.mkdir(parents=True, exist_ok=True)
    agent_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, str(WM_PY), *args],
        input=stdin_text, text=True, env=env, cwd=str(REPO_ROOT),
        capture_output=True, timeout=60,
    )
    assert proc.returncode == 0, (
        f"CLI wm.py failed (rc={proc.returncode}):\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return proc


@pytest.mark.skipif(yaml is None, reason="PyYAML required")
@pytest.mark.skipif(not WM_PY.exists(), reason="core/scripts/wm.py missing")
def test_byte_compat_set_top_level(tmp_path):
    """Top-level set self-heals to _default_wm_data (deterministic, no
    slot_meta timestamps) then sets the key — fully byte-comparable. Both
    sides read the REAL memory-pipeline.yaml so slot_types match."""
    from mind_api.src.endpoints import wm_write

    cli_agent = tmp_path / "cli-agent"
    dae_agent = tmp_path / "dae-agent"

    _run_wm_cli(tmp_path / "world", tmp_path / "meta", cli_agent,
                ["set", "last_goal_category"], "framework-loop")
    wm_write.set_slot(_FakeCtx(dae_agent, REPO_ROOT, tmp_path / "world",
                               {"slot": "last_goal_category"},
                               b"framework-loop"))

    cli_wm = (cli_agent / "session" / "working-memory.yaml").read_bytes()
    dae_wm = (dae_agent / "session" / "working-memory.yaml").read_bytes()
    assert dae_wm == cli_wm
