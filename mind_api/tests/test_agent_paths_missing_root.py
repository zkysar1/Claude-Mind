""" outcome 1: a daemon whose resolved world or meta root does not
exist refuses loudly instead of serving empty results.

Measured on a Windows box: a daemon recycled from a shell that exported
MSYS_NO_PATHCONV=1 resolved MIND_WORLD=/c/<path> to a phantom, empty
C:/c/<path> and served it for 7 minutes with no error. Under own-cloud the
local tree is a read-through cache, so its absence proves nothing
(guard-980); the refusal is for local storage only.
"""
import json
import urllib.error
import urllib.request

import pytest

from mind_api.src.agent_paths import AgentPathResolver
from mind_api.src.server import RequestContext


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("MIND_WORLD", raising=False)
    monkeypatch.delenv("MIND_META", raising=False)
    monkeypatch.setenv("STORAGE_BACKEND", "local")


def _agent(root, name, world, meta):
    agent = root / "agents" / name
    agent.mkdir(parents=True)
    (agent / "local-paths.conf").write_text(
        f"WORLD_PATH={world}\nMETA_PATH={meta}\n", encoding="utf-8")


def _resolver(tmp_path, world, meta):
    _agent(tmp_path, "tester", world, meta)
    return AgentPathResolver(tmp_path)


def _roots(tmp_path, prefix=""):
    world, meta = tmp_path / f"{prefix}w", tmp_path / f"{prefix}m"
    world.mkdir()
    meta.mkdir()
    return world, meta


def test_existing_roots_resolve(tmp_path):
    world, meta = _roots(tmp_path)
    paths = _resolver(tmp_path, world, meta).resolve("tester")
    assert paths.world == world and paths.meta == meta


@pytest.mark.parametrize("missing", ["world", "meta"])
def test_a_missing_root_is_refused_by_name(tmp_path, missing):
    world, meta = tmp_path / "w", tmp_path / "m"
    (meta if missing == "world" else world).mkdir()
    with pytest.raises(RuntimeError) as exc:
        _resolver(tmp_path, world, meta).resolve("tester")
    msg = str(exc.value)
    assert f"{missing} root" in msg and "does not exist" in msg
    assert str(world if missing == "world" else meta) in msg
    assert "g-115-11554" in msg


def test_a_refusal_is_not_cached(tmp_path):
    world, meta = tmp_path / "w", tmp_path / "m"
    meta.mkdir()
    resolver = _resolver(tmp_path, world, meta)
    with pytest.raises(RuntimeError):
        resolver.resolve("tester")
    world.mkdir()
    assert resolver.resolve("tester").world == world


def test_own_cloud_does_not_refuse_a_missing_local_root(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    world, meta = tmp_path / "w", tmp_path / "m"
    paths = _resolver(tmp_path, world, meta).resolve("tester")
    assert paths.world == world and paths.meta == meta


def test_a_phantom_env_root_is_refused(tmp_path, monkeypatch):
    """The incident shape: MIND_WORLD outranks the conf and names nothing."""
    world, meta = _roots(tmp_path)
    resolver = _resolver(tmp_path, world, meta)
    phantom = tmp_path / "c" / "phantom" / "world"
    monkeypatch.setenv("MIND_WORLD", str(phantom))
    with pytest.raises(RuntimeError, match="world root .* does not exist"):
        resolver.resolve("tester")


def test_agent_less_pick_passes_over_a_stale_conf(tmp_path):
    """A throwaway conf sorting first must not fail every agent-less request."""
    _agent(tmp_path, "_stale", tmp_path / "gone-w", tmp_path / "gone-m")
    world, meta = _roots(tmp_path)
    _agent(tmp_path, "real", world, meta)
    paths = AgentPathResolver(tmp_path).resolve(None)
    assert paths.agent_name == "real" and paths.world == world


def test_agent_less_pick_still_refuses_when_no_conf_is_served(tmp_path):
    """Positive control for the pick: with no servable candidate it keeps the
    first conf, so the refusal names a real agent instead of going quiet."""
    _agent(tmp_path, "_stale", tmp_path / "gone-w", tmp_path / "gone-m")
    _agent(tmp_path, "real", tmp_path / "also-gone-w", tmp_path / "also-gone-m")
    with pytest.raises(RuntimeError, match=r"agent='_stale'"):
        AgentPathResolver(tmp_path).resolve(None)


def test_health_serves_while_paths_refuse(tmp_path):
    """The refusal surfaces where paths are used; /v1/admin/health never reads
    them, so a missing root cannot turn into a daemon-down kill-and-respawn."""
    from mind_api.src.endpoints.health import health

    resolver = _resolver(tmp_path, tmp_path / "w", tmp_path / "m")
    ctx = RequestContext(
        method="GET", path="/v1/admin/health", query={}, body=b"", paths=None,
        pid=42, port=4242, headers={}, tenant="default",
        paths_factory=lambda: resolver.resolve("tester"))
    resp = health(ctx)
    assert resp.status == 200 and json.loads(resp.body.decode("utf-8"))["ok"] is True
    with pytest.raises(RuntimeError, match="g-115-11554"):
        _ = ctx.paths


def _board_post(port, text):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/board/post?channel=general",
        data=text.encode("utf-8"), method="POST")
    req.add_header("Content-Type", "text/plain")
    req.add_header("X-Mind-Agent", "alpha")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status


def test_a_daemon_on_a_phantom_world_writes_nothing(running_daemon, monkeypatch):
    """Outcome 3, end to end through a real daemon: the incident's write landed
    in the phantom tree. With MIND_WORLD naming a root that does not exist, a
    store write is refused and nothing appears there; restoring the env on the
    SAME daemon lands the write in the real world (the refusal was not cached)."""
    project_root, port = running_daemon
    phantom = project_root / "c" / "phantom" / "world"
    monkeypatch.setenv("MIND_WORLD", str(phantom))
    with pytest.raises(urllib.error.HTTPError) as exc:
        _board_post(port, "must not land")
    assert exc.value.code == 500
    assert not (project_root / "c").exists(), "the refused write created the phantom root"

    monkeypatch.delenv("MIND_WORLD")
    assert _board_post(port, "lands in the real world") == 200
    lines = (project_root / "world" / "board" / "general.jsonl").read_text(
        encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["text"] == "lands in the real world"
