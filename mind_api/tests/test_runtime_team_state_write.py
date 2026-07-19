"""POST /v1/team-state/{update,in-flight,clear-in-flight,init}.

Two layers:
  1. HTTP round-trip (running_daemon, conftest world): endpoints wired,
     update/in-flight/clear-in-flight/init work end-to-end incl. the
     agent-header gate and malformed-field rejection.
  2. Byte-compat (direct handler vs the REAL CLI team-state.py): team-state.yaml
     matches modulo the volatile `last_updated` line. Byte-compat is guaranteed
     by construction (both sides call _fileops.locked_modify_yaml — one
     serializer), so the test confirms the modifier logic + wiring.
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
TEAM_STATE_PY = REPO_ROOT / "core" / "scripts" / "team-state.py"


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


def _read_ts(world: Path) -> dict:
    """Compose shared monolith + per-agent shard rows, row-first — mirrors
    _team_state.load_rows (g-328-27: agent_status rows moved to
    world/team-state/agents/<name>.yaml; the monolith keeps shared fields
    only, so a monolith-only read KeyErrors on per-agent writes)."""
    doc = yaml.safe_load((world / "team-state.yaml").read_text(encoding="utf-8")) or {}
    rows = world / "team-state" / "agents"
    if rows.is_dir():
        status = doc.setdefault("agent_status", {})
        for p in sorted(rows.glob("*.yaml")):
            row = yaml.safe_load(p.read_text(encoding="utf-8"))
            if isinstance(row, dict):
                status[p.stem] = {**(status.get(p.stem) or {}), **row}
    return doc


# ---------------------------------------------------------------------------
# HTTP round-trip tests (conftest world)
# ---------------------------------------------------------------------------

def test_update_set_roundtrip(running_daemon):
    project_root, port = running_daemon
    world = project_root / "world"
    status, body = _post(port, "/v1/team-state/update",
                         {"field": "strategic_focus.primary",
                          "value": "building the daemon cutover"})
    assert status == 200, body
    assert _read_ts(world)["strategic_focus"]["primary"] == "building the daemon cutover"
    assert _read_ts(world)["last_updated_by"] == "alpha"


def test_update_append_roundtrip(running_daemon):
    project_root, port = running_daemon
    world = project_root / "world"
    status, body = _post(port, "/v1/team-state/update",
                         {"field": "strategic_focus.acknowledged_by",
                          "value": "delta", "operation": "append"})
    assert status == 200, body
    assert "delta" in _read_ts(world)["strategic_focus"]["acknowledged_by"]


def test_update_requires_agent_header(running_daemon):
    _, port = running_daemon
    try:
        _post(port, "/v1/team-state/update",
              {"field": "strategic_focus.primary", "value": "x"}, agent=None)
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "missing_agent_header"
    else:
        raise AssertionError("expected 400 without X-Mind-Agent")


def test_update_malformed_field_400(running_daemon):
    _, port = running_daemon
    try:
        _post(port, "/v1/team-state/update",
              {"field": "agent_status..in_flight", "value": "x"})
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "invalid_field"
    else:
        raise AssertionError("expected 400 for malformed field path")


def test_in_flight_roundtrip(running_daemon):
    project_root, port = running_daemon
    world = project_root / "world"
    status, body = _post(port, "/v1/team-state/in-flight",
                         {"agent": "alpha", "goal_id": "g-9-9",
                          "title": "test goal", "phase": "4"})
    assert status == 200, body
    inflight = _read_ts(world)["agent_status"]["alpha"]["in_flight"]
    assert inflight["goal_id"] == "g-9-9" and inflight["phase"] == "4"
    assert inflight["claimed_at"]
    assert _read_ts(world)["agent_status"]["alpha"]["last_active"]


def test_clear_in_flight_roundtrip(running_daemon):
    project_root, port = running_daemon
    world = project_root / "world"
    _post(port, "/v1/team-state/in-flight",
          {"agent": "alpha", "goal_id": "g-9-9", "title": "t", "phase": "4"})
    status, body = _post(port, "/v1/team-state/clear-in-flight", {"agent": "alpha"})
    assert status == 200, body
    assert json.loads(body)["cleared"] is True
    assert "in_flight" not in _read_ts(world)["agent_status"]["alpha"]
    # Second clear is a no-op.
    _, body2 = _post(port, "/v1/team-state/clear-in-flight", {"agent": "alpha"})
    assert json.loads(body2)["cleared"] is False


def test_init_idempotent(running_daemon):
    _, port = running_daemon
    # conftest already seeds team-state.yaml → init reports not-created.
    status, body = _post(port, "/v1/team-state/init", {})
    assert status == 200, body
    assert json.loads(body)["created"] is False


# ---------------------------------------------------------------------------
# Byte-compat: daemon handler output == real CLI output
# ---------------------------------------------------------------------------

class _FakePaths:
    def __init__(self, world: Path):
        self.world = world
        self.agent_name = "alpha"


class _FakeCtx:
    def __init__(self, world: Path, query: dict, body: bytes = b"", *, agent="alpha"):
        self.paths = _FakePaths(world)
        self.query = query
        self.body = body
        self.headers = {"x-mind-agent": agent}


def _run_ts_cli(world: Path, args):
    env = dict(os.environ)
    env["MIND_WORLD"] = str(world)
    env["MIND_META"] = str(world.parent / "meta")
    env["MIND_AGENT"] = "alpha"
    (world.parent / "meta").mkdir(parents=True, exist_ok=True)
    world.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, str(TEAM_STATE_PY), *args],
        text=True, env=env, cwd=str(REPO_ROOT), capture_output=True, timeout=60,
    )
    assert proc.returncode == 0, (
        f"CLI team-state.py failed (rc={proc.returncode}):\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return proc


def _normalize(text: str) -> str:
    """Blank the volatile last_updated line (datetime.now, differs per write)."""
    out = []
    for ln in text.splitlines():
        out.append("last_updated: <NORM>" if ln.startswith("last_updated:") else ln)
    return "\n".join(out)


@pytest.mark.skipif(yaml is None, reason="PyYAML required")
@pytest.mark.skipif(not TEAM_STATE_PY.exists(), reason="core/scripts/team-state.py missing")
def test_byte_compat_update(tmp_path):
    from mind_api.src.world import team_state_write

    cli_world = tmp_path / "cli"
    dae_world = tmp_path / "dae"
    cli_world.mkdir()
    dae_world.mkdir()

    _run_ts_cli(cli_world,
                ["update", "--field", "strategic_focus.primary",
                 "--value", "shared coordination focus", "--author", "alpha"])
    team_state_write.update(_FakeCtx(dae_world, {
        "field": "strategic_focus.primary",
        "value": "shared coordination focus", "operation": "set"}))

    cli_text = (cli_world / "team-state.yaml").read_text(encoding="utf-8")
    dae_text = (dae_world / "team-state.yaml").read_text(encoding="utf-8")
    assert _normalize(dae_text) == _normalize(cli_text), \
        f"\n--- daemon ---\n{dae_text}\n--- cli ---\n{cli_text}"
