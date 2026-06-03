"""POST /v1/meta/experiment/{create,resolve} + GET .../{status,list}.

Layers:
  1. HTTP round-trip (running_daemon): create + status read-back, list,
     resolve, max_concurrent 409, not-found 404, bad-float 400.
  2. Byte-compat (direct handler vs the REAL CLI meta-experiment.py):
       - create stdout (timestamp-free) byte-identical + written
         active-experiments.yaml byte-compat (timestamp-normalised, CSafeDumper,
         history + changelog side-effects present).
       - status/list stdout byte-for-byte (ensure_ascii=False, default=str).
       - resolve stdout (delta deterministic) byte-identical + both written
         files byte-compat (timestamp-normalised).

META-scoped; CLI redirected via MIND_META; CONFIG (max_concurrent=1,
significance_threshold=0.05) is the real core/config/meta.yaml read by both.
Agent set to alpha on both sides so changelog attribution matches.
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
EXP_PY = REPO_ROOT / "core" / "scripts" / "meta-experiment.py"


def _seed_active(meta, experiments):
    exp_dir = meta / "experiments"
    exp_dir.mkdir(parents=True, exist_ok=True)
    (exp_dir / "active-experiments.yaml").write_text(
        yaml.dump({"experiments": experiments}, Dumper=yaml.CSafeDumper,
                  default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8")


def _setup(tmp_path, name="x"):
    base = tmp_path / name
    meta = base / "meta"
    agent = base / "agents" / "alpha"
    for d in (meta, agent):
        d.mkdir(parents=True, exist_ok=True)
    return meta, agent


def _run_cli(meta, agent, args, check_rc=True):
    env = dict(os.environ)
    env["MIND_META"] = str(meta)
    env["MIND_WORLD"] = str(meta.parent / "world")
    env["MIND_AGENT"] = "alpha"
    env["MIND_AGENT_DIR"] = str(agent)
    proc = subprocess.run(
        [sys.executable, str(EXP_PY), *args],
        text=True, encoding="utf-8", env=env, cwd=str(REPO_ROOT),
        capture_output=True, timeout=60,
    )
    if check_rc:
        assert proc.returncode == 0, (
            f"CLI meta-experiment.py failed (rc={proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return proc


class _FakePaths:
    def __init__(self, meta, agent):
        self.meta = meta
        self.agent = agent
        self.project_root = REPO_ROOT


class _FakeCtx:
    def __init__(self, meta, agent, query=None, body=None, headers=None):
        self.paths = _FakePaths(meta, agent)
        self.query = query or {}
        self.body = body
        self.headers = headers if headers is not None else {"x-ayoai-agent": "alpha"}


_TS_RE = re.compile(r'(created|resolved):\s*\S+')


def _norm(text):
    return _TS_RE.sub(r'\1: <TS>', text)


def _http(port, method, path, query=None, body=None):
    qs = ("?" + urllib.parse.urlencode(query)) if query else ""
    url = f"http://127.0.0.1:{port}{path}{qs}"
    data = body.encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, method=method, data=data)
    req.add_header("X-Mind-Agent", "alpha")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.read().decode("utf-8")


_RESOLVABLE = {
    "id": "exp-meta-001", "created": "2026-05-01T00:00:00",
    "strategy_file": "goal-selection-strategy.yaml", "field": "weights.novelty",
    "baseline_value": 1.0, "variant_value": 1.5, "status": "active",
    "phase": "variant", "total_goals": 20,
    "metrics": {"baseline": [0.5, 0.5], "variant": [0.9, 0.9]},  # delta=+0.4 -> adopted
}


# ---------------------------------------------------------------------------
# HTTP round-trip
# ---------------------------------------------------------------------------

def test_create_then_status(running_daemon):
    _, port = running_daemon
    body = {"strategy": "goal-selection-strategy.yaml", "field": "weights.x",
            "baseline": 1.0, "variant": 1.5}
    status, out = _http(port, "POST", "/v1/meta/experiment/create", body=json.dumps(body))
    assert status == 200
    created = json.loads(out)
    assert created["status"] == "created" and created["id"].startswith("exp-meta-")
    status, out = _http(port, "GET", "/v1/meta/experiment/status")
    assert status == 200
    assert json.loads(out)["active_experiments"] >= 1


def test_create_max_concurrent_409(running_daemon):
    # First create fills the single slot; second must 409.
    _, port = running_daemon
    body = {"strategy": "s", "field": "f", "baseline": 1.0, "variant": 2.0}
    try:
        _http(port, "POST", "/v1/meta/experiment/create", body=json.dumps(body))
    except urllib.error.HTTPError:
        pass  # a prior test may have filled the slot already
    try:
        _http(port, "POST", "/v1/meta/experiment/create", body=json.dumps(body))
    except urllib.error.HTTPError as e:
        assert e.code == 409
        assert json.loads(e.read())["error"] == "max_concurrent"
    else:
        raise AssertionError("expected 409 at max_concurrent")


def test_create_bad_float_400(running_daemon):
    _, port = running_daemon
    body = {"strategy": "s", "field": "f", "baseline": "abc", "variant": 2.0}
    try:
        _http(port, "POST", "/v1/meta/experiment/create", body=json.dumps(body))
    except urllib.error.HTTPError as e:
        assert e.code == 400
    else:
        raise AssertionError("expected 400 for bad float")


def test_status_not_found_404(running_daemon):
    _, port = running_daemon
    try:
        _http(port, "GET", "/v1/meta/experiment/status", {"id": "exp-meta-999"})
    except urllib.error.HTTPError as e:
        assert e.code == 404
    else:
        raise AssertionError("expected 404 for unknown experiment")


def test_list_route(running_daemon):
    _, port = running_daemon
    status, out = _http(port, "GET", "/v1/meta/experiment/list", {"completed": "1"})
    assert status == 200
    assert "count" in json.loads(out)


# ---------------------------------------------------------------------------
# Byte-compat
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not EXP_PY.exists(), reason="core/scripts/meta-experiment.py missing")
class TestByteCompat:
    def test_create(self, tmp_path):
        from mind_api.src.meta import meta_experiment
        cli_meta, cli_agent = _setup(tmp_path, "cli")
        dmn_meta, dmn_agent = _setup(tmp_path, "dmn")
        args = ["create", "--strategy", "goal-selection-strategy.yaml",
                "--field", "weights.novelty", "--baseline", "1.0", "--variant", "1.5"]
        cli_out = _run_cli(cli_meta, cli_agent, args).stdout
        body = {"strategy": "goal-selection-strategy.yaml", "field": "weights.novelty",
                "baseline": 1.0, "variant": 1.5}
        resp = meta_experiment.create(
            _FakeCtx(dmn_meta, dmn_agent, body=json.dumps(body).encode("utf-8")))
        assert resp.status == 200
        assert resp.body.decode("utf-8") == cli_out  # timestamp-free stdout
        cli_yaml = (cli_meta / "experiments" / "active-experiments.yaml").read_text(encoding="utf-8")
        dmn_yaml = (dmn_meta / "experiments" / "active-experiments.yaml").read_text(encoding="utf-8")
        assert _norm(dmn_yaml) == _norm(cli_yaml)
        # First create: file was new -> NO history snapshot (matches CLI
        # save_history), but a changelog entry IS appended.
        assert not (dmn_meta / ".history" / "experiments" / "active-experiments.yaml").exists()
        assert (dmn_meta / "changelog.jsonl").exists()

    def test_create_409(self, tmp_path):
        from mind_api.src.meta import meta_experiment
        meta, agent = _setup(tmp_path)
        _seed_active(meta, [dict(_RESOLVABLE)])  # 1 active == max_concurrent
        proc = _run_cli(meta, agent, [
            "create", "--strategy", "s", "--field", "f",
            "--baseline", "1", "--variant", "2"], check_rc=False)
        assert proc.returncode == 1  # CLI sys.exit(1)
        resp = meta_experiment.create(_FakeCtx(meta, agent, body=json.dumps(
            {"strategy": "s", "field": "f", "baseline": 1, "variant": 2}).encode("utf-8")))
        assert resp.status == 409

    def test_status_single(self, tmp_path):
        from mind_api.src.meta import meta_experiment
        meta, agent = _setup(tmp_path)
        _seed_active(meta, [dict(_RESOLVABLE)])
        cli = _run_cli(meta, agent, ["status", "--id", "exp-meta-001"]).stdout
        resp = meta_experiment.status(_FakeCtx(meta, agent, query={"id": "exp-meta-001"}))
        assert resp.body.decode("utf-8") == cli

    def test_status_all(self, tmp_path):
        from mind_api.src.meta import meta_experiment
        meta, agent = _setup(tmp_path)
        _seed_active(meta, [dict(_RESOLVABLE)])
        cli = _run_cli(meta, agent, ["status"]).stdout
        resp = meta_experiment.status(_FakeCtx(meta, agent))
        assert resp.body.decode("utf-8") == cli

    def test_status_missing_file_empty(self, tmp_path):
        from mind_api.src.meta import meta_experiment
        meta, agent = _setup(tmp_path)  # no experiments dir
        cli = _run_cli(meta, agent, ["status"]).stdout
        resp = meta_experiment.status(_FakeCtx(meta, agent))
        assert resp.body.decode("utf-8") == cli
        assert json.loads(resp.body.decode("utf-8"))["active_experiments"] == 0

    def test_list_active(self, tmp_path):
        from mind_api.src.meta import meta_experiment
        meta, agent = _setup(tmp_path)
        _seed_active(meta, [dict(_RESOLVABLE)])
        cli = _run_cli(meta, agent, ["list"]).stdout
        resp = meta_experiment.list_experiments(_FakeCtx(meta, agent))
        assert resp.body.decode("utf-8") == cli

    def test_resolve(self, tmp_path):
        from mind_api.src.meta import meta_experiment
        cli_meta, cli_agent = _setup(tmp_path, "cli")
        dmn_meta, dmn_agent = _setup(tmp_path, "dmn")
        _seed_active(cli_meta, [dict(_RESOLVABLE)])
        _seed_active(dmn_meta, [dict(_RESOLVABLE)])
        cli_out = _run_cli(cli_meta, cli_agent, ["resolve", "--id", "exp-meta-001"]).stdout
        resp = meta_experiment.resolve(
            _FakeCtx(dmn_meta, dmn_agent, body=json.dumps({"id": "exp-meta-001"}).encode("utf-8")))
        assert resp.status == 200
        assert resp.body.decode("utf-8") == cli_out  # outcome=adopted, delta=0.4
        out = json.loads(cli_out)
        assert out["outcome"] == "adopted" and out["delta"] == 0.4
        for fname in ("active-experiments.yaml", "completed-experiments.yaml"):
            cli_y = (cli_meta / "experiments" / fname).read_text(encoding="utf-8")
            dmn_y = (dmn_meta / "experiments" / fname).read_text(encoding="utf-8")
            assert _norm(dmn_y) == _norm(cli_y), fname
        # active-experiments.yaml pre-existed -> resolve DOES snapshot it.
        assert (dmn_meta / ".history" / "experiments" / "active-experiments.yaml").exists()
        assert (dmn_meta / "changelog.jsonl").exists()

    def test_resolve_not_found_404(self, tmp_path):
        from mind_api.src.meta import meta_experiment
        meta, agent = _setup(tmp_path)
        _seed_active(meta, [dict(_RESOLVABLE)])
        proc = _run_cli(meta, agent, ["resolve", "--id", "exp-meta-999"], check_rc=False)
        assert proc.returncode == 1
        resp = meta_experiment.resolve(_FakeCtx(meta, agent, body=json.dumps(
            {"id": "exp-meta-999"}).encode("utf-8")))
        assert resp.status == 404
