"""GET /v1/skill-quality/derive + POST /v1/skill-quality/score.

Layers:
  1. HTTP round-trip (running_daemon): derive route wired, score write +
     read-back via skill-evaluate, dry-run no-write, invalid skill -> 400.
  2. Byte-compat (direct handler vs the REAL CLI skill-quality-score.py):
       - derive: stdout byte-for-byte (indent=2, ensure_ascii=True default).
       - score success: two-line stdout (timestamp-free "Scored ..." then the
         {derived,llm_supplied} trailer, ensure_ascii default, no indent) +
         written skill-quality.yaml byte-compat (timestamp-normalised, default
         yaml.Dumper, no history/changelog).
       - dry-run: stdout byte-for-byte, no write.

Canonicalization reads .claude/skills (real, via project_root) + the forged
registry (temp world). Tests use a SEEDED forged skill "tq-skill" so the
canonical name + maintainability derivation are fully controlled and never
depend on real skill-name churn. CLI redirected via MIND_META + MIND_WORLD;
project_root pinned to REPO_ROOT for both so .claude/skills matches.
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
SQS_PY = REPO_ROOT / "core" / "scripts" / "skill-quality-score.py"

_FORGED = {"skills": {
    # forged skill with explicit maintainability grade -> derive picks it up
    "tq-skill": {"forged_date": "2026-01-01T00:00:00", "quality_at_forge": "average"},
    # forged skill with no quality key -> maintainability defaults to "good"
    "tq-base": {"forged_date": "2026-01-01T00:00:00"},
}}


def _setup(tmp_path, name="x"):
    base = tmp_path / name
    meta = base / "meta"
    world = base / "world"
    agent = base / "agents" / "alpha"
    for d in (meta, world, agent):
        d.mkdir(parents=True, exist_ok=True)
    (world / "forged-skills.yaml").write_text(
        yaml.safe_dump(_FORGED, sort_keys=False), encoding="utf-8")
    return meta, world, agent


def _cli_judge_provenance():
    """What the CLI chain will stamp for judge provenance ().

    skill-quality-score.py subprocesses skill-evaluate.py, which resolves from
    its own environment; _run_cli passes a copy of THIS process's environment,
    so running the same resolver here yields the same pair.
    """
    import importlib.util
    path = REPO_ROOT / "core" / "scripts" / "skill-evaluate.py"
    spec = importlib.util.spec_from_file_location("skill_evaluate_cli_judge",
                                                  path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._judge_provenance(*mod._judge_from_env())


def _run_cli(meta, world, agent, args, check_rc=True):
    env = dict(os.environ)
    env["MIND_META"] = str(meta)
    env["MIND_WORLD"] = str(world)
    env["MIND_AGENT"] = "alpha"
    env["MIND_AGENT_DIR"] = str(agent)
    proc = subprocess.run(
        [sys.executable, str(SQS_PY), *args],
        text=True, encoding="utf-8", env=env, cwd=str(REPO_ROOT),
        capture_output=True, timeout=60,
    )
    if check_rc:
        assert proc.returncode == 0, (
            f"CLI skill-quality-score.py failed (rc={proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return proc


class _FakePaths:
    def __init__(self, meta, world, agent):
        self.meta = meta
        self.world = world
        self.agent = agent
        self.project_root = REPO_ROOT


class _FakeCtx:
    def __init__(self, meta, world, agent, query=None, body=None, headers=None):
        self.paths = _FakePaths(meta, world, agent)
        self.query = query or {}
        self.body = body
        self.headers = headers if headers is not None else {}


_DATE_RE = re.compile(r'(date|last_updated):\s*\S+')


def _norm(text):
    return _DATE_RE.sub(r'\1: <TS>', text)


def _http(port, method, path, query=None, body=None):
    qs = ("?" + urllib.parse.urlencode(query)) if query else ""
    url = f"http://127.0.0.1:{port}{path}{qs}"
    data = body.encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, method=method, data=data)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.read().decode("utf-8")


# ---------------------------------------------------------------------------
# HTTP round-trip
# ---------------------------------------------------------------------------

def test_derive_route_wired(running_daemon):
    # conftest world has no forged registry; a real base skill name works.
    _, port = running_daemon
    try:
        status, body = _http(port, "GET", "/v1/skill-quality/derive",
                             {"skill": "replay", "goal": "g-1", "outcomes_met": "3",
                              "outcomes_total": "3"})
        assert status == 200
        out = json.loads(body)
        assert out["canonical_skill_name"] == "replay"
        assert out["completeness"] == "good"
    except urllib.error.HTTPError as e:
        # If 'replay' isn't a base skill in this checkout, accept the 400 path.
        assert e.code == 400


def test_score_invalid_skill_400(running_daemon):
    _, port = running_daemon
    body = {"skill": "totally-nonexistent-zzz", "goal": "g-1", "cost_awareness": "good"}
    try:
        _http(port, "POST", "/v1/skill-quality/score", body=json.dumps(body))
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "invalid_skill"
    else:
        raise AssertionError("expected 400 for unknown skill")


def test_score_invalid_cost_awareness_400(running_daemon):
    _, port = running_daemon
    body = {"skill": "replay", "goal": "g-1", "cost_awareness": "excellent"}
    try:
        _http(port, "POST", "/v1/skill-quality/score", body=json.dumps(body))
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "invalid_grade"
    else:
        raise AssertionError("expected 400 for invalid cost_awareness")


# ---------------------------------------------------------------------------
# Byte-compat
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not SQS_PY.exists(), reason="core/scripts/skill-quality-score.py missing")
class TestByteCompat:
    def test_derive(self, tmp_path):
        from mind_api.src.meta import skill_quality_score
        meta, world, agent = _setup(tmp_path)
        cli = _run_cli(meta, world, agent, [
            "derive", "--skill", "tq-skill", "--goal", "g-1",
            "--outcomes-met", "2", "--outcomes-total", "3",
            "--episode-chain-count", "1", "--guardrail-violations", "2"]).stdout
        resp = skill_quality_score.derive(_FakeCtx(meta, world, agent, query={
            "skill": "tq-skill", "goal": "g-1", "outcomes_met": "2",
            "outcomes_total": "3", "episode_chain_count": "1",
            "guardrail_violations": "2"}))
        assert resp.body.decode("utf-8") == cli

    def test_derive_base_maintainability_good(self, tmp_path):
        from mind_api.src.meta import skill_quality_score
        meta, world, agent = _setup(tmp_path)
        cli = _run_cli(meta, world, agent, [
            "derive", "--skill", "tq-base", "--goal", "g-1",
            "--outcomes-met", "0", "--outcomes-total", "0",
            "--episode-chain-count", "0", "--guardrail-violations", "0"]).stdout
        resp = skill_quality_score.derive(_FakeCtx(meta, world, agent, query={
            "skill": "tq-base", "goal": "g-1"}))
        assert resp.body.decode("utf-8") == cli
        # all-good defaults
        out = json.loads(resp.body.decode("utf-8"))
        assert out["maintainability"] == "good"

    def test_score_success(self, tmp_path):
        from mind_api.src.meta import skill_quality_score
        cli_meta, cli_world, cli_agent = _setup(tmp_path, "cli")
        dmn_meta, dmn_world, dmn_agent = _setup(tmp_path, "dmn")
        args = ["score", "--skill", "tq-skill", "--goal", "g-9-1",
                "--outcomes-met", "3", "--outcomes-total", "3",
                "--episode-chain-count", "0", "--guardrail-violations", "0",
                "--cost-awareness", "good"]
        cli_out = _run_cli(cli_meta, cli_world, cli_agent, args).stdout
        # Judge provenance reaches each side by a different transport
        # (): the CLI chain subprocesses skill-evaluate.py, which runs
        # in the judge's own process and resolves from its environment; the
        # daemon is long-lived and must be told, in the body. Feed it the same
        # input so this compares bytes rather than transports (guard-1189).
        judge_model, harness = _cli_judge_provenance()
        body = {"skill": "tq-skill", "goal": "g-9-1", "outcomes_met": 3,
                "outcomes_total": 3, "episode_chain_count": 0,
                "guardrail_violations": 0, "cost_awareness": "good",
                "judge_model": judge_model, "harness": harness}
        resp = skill_quality_score.score(
            _FakeCtx(dmn_meta, dmn_world, dmn_agent, body=json.dumps(body).encode("utf-8")))
        assert resp.status == 200
        # Two-line stdout is timestamp-free -> byte-identical.
        assert resp.body.decode("utf-8") == cli_out
        # Sanity on the trailer + scored line shape.
        lines = cli_out.splitlines()
        assert lines[0].startswith("Scored tq-skill: overall ")
        trailer = json.loads(lines[1])
        assert trailer["derived"]["maintainability"] == "average"  # from registry
        assert trailer["llm_supplied"]["cost_awareness"] == "good"
        # Written YAML matches modulo timestamps; default Dumper; no history/changelog.
        cli_yaml = (cli_meta / "skill-quality.yaml").read_text(encoding="utf-8")
        dmn_yaml = (dmn_meta / "skill-quality.yaml").read_text(encoding="utf-8")
        assert _norm(dmn_yaml) == _norm(cli_yaml)
        loaded = yaml.safe_load(dmn_yaml)
        assert loaded["skills"]["tq-skill"]["evaluations"][0]["maintainability"] == 0.5
        assert not (dmn_meta / ".history").exists()
        assert not (dmn_meta / "changelog.jsonl").exists()

    def test_score_dry_run_no_write(self, tmp_path):
        from mind_api.src.meta import skill_quality_score
        meta, world, agent = _setup(tmp_path)
        cli = _run_cli(meta, world, agent, [
            "score", "--skill", "tq-skill", "--goal", "g-1",
            "--outcomes-met", "1", "--outcomes-total", "3",
            "--episode-chain-count", "2", "--guardrail-violations", "3",
            "--cost-awareness", "poor", "--dry-run"]).stdout
        # CLI dry-run wrote nothing.
        assert not (meta / "skill-quality.yaml").exists()
        body = {"skill": "tq-skill", "goal": "g-1", "outcomes_met": 1,
                "outcomes_total": 3, "episode_chain_count": 2,
                "guardrail_violations": 3, "cost_awareness": "poor", "dry_run": True}
        resp = skill_quality_score.score(
            _FakeCtx(meta, world, agent, body=json.dumps(body).encode("utf-8")))
        assert resp.body.decode("utf-8") == cli
        # Daemon dry-run also wrote nothing.
        assert not (meta / "skill-quality.yaml").exists()

    def test_score_bad_skill_cli_exit1_daemon_400(self, tmp_path):
        from mind_api.src.meta import skill_quality_score
        meta, world, agent = _setup(tmp_path)
        proc = _run_cli(meta, world, agent, [
            "score", "--skill", "ghost-skill-zzz", "--goal", "g-1",
            "--cost-awareness", "good"], check_rc=False)
        assert proc.returncode == 1
        resp = skill_quality_score.score(_FakeCtx(meta, world, agent, body=json.dumps(
            {"skill": "ghost-skill-zzz", "goal": "g-1", "cost_awareness": "good"}
        ).encode("utf-8")))
        assert resp.status == 400
