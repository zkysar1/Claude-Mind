"""GET /v1/skill-analytics/{reuse-report,co-invocation,coverage,recommendations,trend}.

Two layers:
  1. HTTP round-trip (running_daemon): routes wired, empty zero-states,
     coverage missing-header -> 400, trend invalid window -> 400.
  2. Byte-compat (direct handler vs the REAL CLI skill-analytics.py): response
     body == CLI stdout for all 5 pure-read reports (deterministic seeds, no
     timestamps in output -> exact byte-compat).

CLI redirected with MIND_META / MIND_WORLD / MIND_AGENT_DIR; base relations
come from the REAL core/config/skill-relations.yaml (CLI via CONFIG_DIR cwd=
REPO_ROOT, daemon via ctx.paths.project_root=REPO_ROOT). Synthetic skill names
(skill-a/b/c) cannot collide with real base relations in the quality map.
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
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
ANALYTICS_PY = REPO_ROOT / "core" / "scripts" / "skill-analytics.py"

_QUALITY = {
    "skills": {
        "skill-a": {"evaluations": [
            {"overall": 0.8, "date": "2026-05-20T10:00:00"},
            {"overall": 0.9, "date": "2026-05-21T10:00:00"}]},
        "skill-b": {"evaluations": [
            {"overall": 0.2, "date": "2026-05-20T10:00:00"}]},
        "skill-c": {"evaluations": [
            {"overall": 0.4, "date": "2026-05-20T10:00:00"}]},
    }
}
_GAPS = {"gaps": [
    {"name": "gap-x", "times_encountered": 5, "description": "needs forging"},
    {"name": "gap-y", "times_encountered": 1, "description": "rare"}]}
_WORLD = {
    "co_invocation_log": [
        {"goal_id": "g-1", "skills": ["skill-a", "skill-b"], "date": "2026-05-20T10:00:00"},
        {"goal_id": "g-2", "skills": ["skill-a", "skill-b"], "date": "2026-05-20T10:01:00"},
        {"goal_id": "g-3", "skills": ["skill-a", "skill-c"], "date": "2026-05-20T10:02:00"}],
    "forged_relations": [
        {"source": "skill-b", "target": "skill-a", "type": "similar_to"}],
}
_EXPERIENCE = [
    {"category": "cat-1", "skill": "skill-a", "outcome": "success"},
    {"category": "cat-1", "skill": "skill-a", "outcome": "failed"},
    {"category": "cat-2", "skill": "skill-b", "outcome": {"success": True}},
]


def _setup(tmp_path):
    meta = tmp_path / "meta"
    world = tmp_path / "world"
    agent = tmp_path / "agents" / "alpha"
    for d in (meta, world, agent):
        d.mkdir(parents=True, exist_ok=True)
    (meta / "skill-quality.yaml").write_text(
        yaml.safe_dump(_QUALITY, default_flow_style=False, sort_keys=False), encoding="utf-8")
    (meta / "skill-gaps.yaml").write_text(
        yaml.safe_dump(_GAPS, default_flow_style=False, sort_keys=False), encoding="utf-8")
    (world / "skill-relations.yaml").write_text(
        yaml.safe_dump(_WORLD, default_flow_style=False, sort_keys=False), encoding="utf-8")
    (agent / "experience.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in _EXPERIENCE), encoding="utf-8")
    return meta, world, agent


def _run_cli(meta, world, agent, args, check_rc=True):
    env = dict(os.environ)
    env["MIND_META"] = str(meta)
    env["MIND_WORLD"] = str(world)
    env["MIND_AGENT"] = "alpha"
    env["MIND_AGENT_DIR"] = str(agent)
    proc = subprocess.run(
        [sys.executable, str(ANALYTICS_PY), *args],
        text=True, encoding="utf-8", env=env, cwd=str(REPO_ROOT),
        capture_output=True, timeout=60,
    )
    if check_rc:
        assert proc.returncode == 0, (
            f"CLI skill-analytics.py failed (rc={proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return proc


class _FakePaths:
    def __init__(self, meta, world, agent):
        self.meta = meta
        self.world = world
        self.agent = agent
        self.project_root = REPO_ROOT


class _FakeCtx:
    def __init__(self, meta, world, agent, query=None, headers=None):
        self.paths = _FakePaths(meta, world, agent)
        self.query = query or {}
        self.headers = headers if headers is not None else {"x-ayoai-agent": "alpha"}
        self.body = None


def _http(port, path, query=None, agent="alpha"):
    qs = ("?" + urllib.parse.urlencode(query)) if query else ""
    url = f"http://127.0.0.1:{port}{path}{qs}"
    req = urllib.request.Request(url, method="GET")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


# ---------------------------------------------------------------------------
# HTTP round-trip
# ---------------------------------------------------------------------------

def test_reuse_report_empty(running_daemon):
    _, port = running_daemon
    status, body = _http(port, "/v1/skill-analytics/reuse-report")
    assert status == 200
    out = json.loads(body)
    assert out["summary"] == {"total_evaluated": 0, "avg_quality": 0.0}


def test_co_invocation_empty(running_daemon):
    _, port = running_daemon
    status, body = _http(port, "/v1/skill-analytics/co-invocation")
    assert status == 200
    assert body == "[]\n"


def test_coverage_missing_header_400(running_daemon):
    _, port = running_daemon
    try:
        _http(port, "/v1/skill-analytics/coverage", agent=None)
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "missing_agent_header"
    else:
        raise AssertionError("expected 400 for missing agent header")


def test_trend_invalid_window_400(running_daemon):
    _, port = running_daemon
    try:
        _http(port, "/v1/skill-analytics/trend", {"window": "abc"})
    except urllib.error.HTTPError as e:
        assert e.code == 400
    else:
        raise AssertionError("expected 400 for non-int window")


# ---------------------------------------------------------------------------
# Byte-compat
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not ANALYTICS_PY.exists(), reason="core/scripts/skill-analytics.py missing")
class TestByteCompat:
    def _check(self, tmp_path, cli_args, handler_name, query=None):
        from mind_api.src.endpoints import skill_analytics
        meta, world, agent = _setup(tmp_path)
        cli_out = _run_cli(meta, world, agent, cli_args).stdout
        resp = getattr(skill_analytics, handler_name)(_FakeCtx(meta, world, agent, query))
        assert resp.body.decode("utf-8") == cli_out

    def test_reuse_report(self, tmp_path):
        self._check(tmp_path, ["reuse-report"], "reuse_report")

    def test_co_invocation(self, tmp_path):
        self._check(tmp_path, ["co-invocation"], "co_invocation")

    def test_coverage(self, tmp_path):
        self._check(tmp_path, ["coverage"], "coverage")

    def test_recommendations(self, tmp_path):
        self._check(tmp_path, ["recommendations"], "recommendations")

    def test_trend_default(self, tmp_path):
        self._check(tmp_path, ["trend"], "trend")

    def test_trend_window(self, tmp_path):
        self._check(tmp_path, ["trend", "--window", "1"], "trend", {"window": "1"})

    def test_empty_meta(self, tmp_path):
        # All-missing files -> zero-state byte-compat.
        from mind_api.src.endpoints import skill_analytics
        meta = tmp_path / "meta"; world = tmp_path / "world"
        agent = tmp_path / "agents" / "alpha"
        for d in (meta, world, agent):
            d.mkdir(parents=True, exist_ok=True)
        cli_out = _run_cli(meta, world, agent, ["reuse-report"]).stdout
        resp = skill_analytics.reuse_report(_FakeCtx(meta, world, agent))
        assert resp.body.decode("utf-8") == cli_out
