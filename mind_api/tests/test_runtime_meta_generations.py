"""GET snapshot/status/history + POST close/open/update for meta-generations.

Layers:
  1. HTTP round-trip (running_daemon): all 6 routes wired; update bad float
     -> 400; close-with-no-generation -> 200 + {"error":...} body.
  2. Byte-compat (direct handler vs the REAL CLI meta-generations.py):
       - snapshot: pure read of the 6 strategy files; byte-identical stdout;
         GEN_PATH is NOT created (snapshot never calls ensure_state).
       - open: stdout byte-identical (timestamp-free) + GEN_PATH byte-compat
         (timestamp-normalised); 4-key metrics.
       - update auto-open: stdout + GEN_PATH byte-compat; metrics has exactly
         the 2 keys (avg/total) — the 4-key/2-key open-vs-update split.
       - close after open: stdout + GEN_PATH byte-compat.
       - close on empty state: stdout {"error":...} byte-identical (exit-0 CLI
         path -> HTTP 200).
       - status: stdout byte-identical + lazy-init GEN_PATH write side-effect.
       - history: stdout byte-compat (started/ended JSON-normalised).

WRITE mechanism: locked CSafeDumper + history + changelog. GEN_PATH lives
under META_DIR -> CLI redirected via MIND_META.
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
MG_PY = REPO_ROOT / "core" / "scripts" / "meta-generations.py"

_GOAL_SEL = (
    "weights:\n"
    "  novelty: 1.0\n"
    "  completion_pressure: 1.5\n"
    "selection_heuristics:\n"
    "  - rule: existing-heuristic\n"
)
_REFLECTION = (
    "depth_allocation:\n"
    "  micro: 0.2\n"
    "  session: 0.5\n"
)


def _seed_meta(meta: Path):
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "goal-selection-strategy.yaml").write_text(_GOAL_SEL, encoding="utf-8")
    (meta / "reflection-strategy.yaml").write_text(_REFLECTION, encoding="utf-8")


def _run_cli(meta, args, check_rc=True):
    env = dict(os.environ)
    env["MIND_META"] = str(meta)
    env["MIND_AGENT"] = "alpha"
    proc = subprocess.run(
        [sys.executable, str(MG_PY), *args],
        text=True, encoding="utf-8", env=env, cwd=str(REPO_ROOT),
        capture_output=True, timeout=60,
    )
    if check_rc:
        assert proc.returncode == 0, (
            f"CLI meta-generations.py failed (rc={proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return proc


class _FakePaths:
    def __init__(self, meta):
        self.meta = meta
        self.world = meta.parent / "world"
        self.agent = meta.parent / "agents" / "alpha"
        self.project_root = REPO_ROOT


class _FakeCtx:
    def __init__(self, meta, query=None, body=None, headers=None):
        self.paths = _FakePaths(meta)
        self.query = query or {}
        self.body = body
        self.headers = headers if headers is not None else {}


_TS_RE = re.compile(r"(started|ended)(: )'?\d{4}-\d\d-\d\dT[\d:]+'?")


def _norm(text):
    return _TS_RE.sub(r"\1\2<TS>", text)


def _norm_json_ts(stdout):
    """Null the started/ended timestamps in a history JSON list for compare."""
    data = json.loads(stdout)
    for row in data:
        row["started"] = "<TS>"
        if row.get("ended") is not None:
            row["ended"] = "<TS>"
    return data


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

def test_open_then_status_route_wired(running_daemon):
    _, port = running_daemon
    s, b = _http(port, "POST", "/v1/meta/generation/open", body="{}")
    assert s == 200 and json.loads(b)["status"] == "opened"
    s, b = _http(port, "GET", "/v1/meta/generation/status")
    assert s == 200 and json.loads(b)["current_generation"] == 1


def test_update_bad_float_400(running_daemon):
    _, port = running_daemon
    try:
        _http(port, "POST", "/v1/meta/generation/update",
              body=json.dumps({"learning_value": "not-a-number"}))
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "invalid_param"
    else:
        raise AssertionError("expected 400 for bad learning_value")


def test_close_empty_state_200_error_body(running_daemon):
    _, port = running_daemon
    s, b = _http(port, "POST", "/v1/meta/generation/close", body="{}")
    assert s == 200
    assert json.loads(b)["error"] == "No active generation to close"


# ---------------------------------------------------------------------------
# Byte-compat
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not MG_PY.exists(), reason="core/scripts/meta-generations.py missing")
class TestByteCompat:
    def test_snapshot_pure_read(self, tmp_path):
        from mind_api.src.meta import meta_generations
        meta = tmp_path / "meta"
        _seed_meta(meta)
        cli = _run_cli(meta, ["snapshot"]).stdout
        resp = meta_generations.snapshot(_FakeCtx(meta))
        assert resp.body.decode("utf-8") == cli
        # snapshot never calls ensure_state -> no GEN_PATH write.
        assert not (meta / "strategy-generations.yaml").exists()
        # Snapshot reflects the flattened nested weights.
        snap = json.loads(cli)
        assert snap["goal_selection_strategy.weights.novelty"] == 1.0

    def test_open(self, tmp_path):
        from mind_api.src.meta import meta_generations
        cli_meta = tmp_path / "cli"
        dmn_meta = tmp_path / "dmn"
        _seed_meta(cli_meta)
        _seed_meta(dmn_meta)
        cli = _run_cli(cli_meta, ["open"]).stdout
        resp = meta_generations.open_generation(_FakeCtx(dmn_meta, body=b"{}"))
        assert resp.status == 200
        assert resp.body.decode("utf-8") == cli  # timestamp-free stdout
        out = json.loads(cli)
        assert out["status"] == "opened" and out["generation"] == 1
        # GEN_PATH byte-compat (timestamps normalised).
        cli_y = (cli_meta / "strategy-generations.yaml").read_text("utf-8")
        dmn_y = (dmn_meta / "strategy-generations.yaml").read_text("utf-8")
        assert _norm(dmn_y) == _norm(cli_y)
        gen = yaml.safe_load(dmn_y)["generations"][0]
        # open seeds 4-key metrics.
        assert set(gen["metrics"].keys()) == {
            "avg_learning_value", "total_learning_value",
            "avg_goal_completion_rate", "pipeline_accuracy"}

    def test_update_autoopen_2key_metrics(self, tmp_path):
        from mind_api.src.meta import meta_generations
        cli_meta = tmp_path / "cli"
        dmn_meta = tmp_path / "dmn"
        _seed_meta(cli_meta)
        _seed_meta(dmn_meta)
        cli = _run_cli(cli_meta, ["update", "--learning-value", "0.8"]).stdout
        resp = meta_generations.update(_FakeCtx(
            dmn_meta, body=json.dumps({"learning_value": 0.8}).encode("utf-8")))
        assert resp.status == 200
        assert resp.body.decode("utf-8") == cli
        out = json.loads(cli)
        assert out["goals_completed"] == 1 and out["avg_learning_value"] == 0.8
        cli_y = (cli_meta / "strategy-generations.yaml").read_text("utf-8")
        dmn_y = (dmn_meta / "strategy-generations.yaml").read_text("utf-8")
        assert _norm(dmn_y) == _norm(cli_y)
        gen = yaml.safe_load(dmn_y)["generations"][0]
        # update's auto-open seeds ONLY 2 metric keys (avg + total).
        assert set(gen["metrics"].keys()) == {
            "avg_learning_value", "total_learning_value"}
        assert gen["metrics"]["total_learning_value"] == 0.8

    def test_close_after_open(self, tmp_path):
        from mind_api.src.meta import meta_generations
        cli_meta = tmp_path / "cli"
        dmn_meta = tmp_path / "dmn"
        _seed_meta(cli_meta)
        _seed_meta(dmn_meta)
        _run_cli(cli_meta, ["open"])
        cli = _run_cli(cli_meta, ["close"]).stdout
        meta_generations.open_generation(_FakeCtx(dmn_meta, body=b"{}"))
        resp = meta_generations.close(_FakeCtx(dmn_meta, body=b"{}"))
        assert resp.status == 200
        assert resp.body.decode("utf-8") == cli
        out = json.loads(cli)
        assert out["status"] == "closed" and out["generation"] == 1
        cli_y = (cli_meta / "strategy-generations.yaml").read_text("utf-8")
        dmn_y = (dmn_meta / "strategy-generations.yaml").read_text("utf-8")
        assert _norm(dmn_y) == _norm(cli_y)
        # The generation is now ended.
        assert yaml.safe_load(dmn_y)["generations"][0]["ended"] is not None

    def test_close_empty_state(self, tmp_path):
        from mind_api.src.meta import meta_generations
        meta = tmp_path / "meta"
        _seed_meta(meta)
        cli = _run_cli(meta, ["close"]).stdout
        resp = meta_generations.close(_FakeCtx(meta, body=b"{}"))
        assert resp.status == 200  # CLI exit-0 informational -> HTTP 200
        assert resp.body.decode("utf-8") == cli
        assert json.loads(cli)["error"] == "No active generation to close"

    def test_status_fresh_lazy_init(self, tmp_path):
        from mind_api.src.meta import meta_generations
        cli_meta = tmp_path / "cli"
        dmn_meta = tmp_path / "dmn"
        _seed_meta(cli_meta)
        _seed_meta(dmn_meta)
        cli = _run_cli(cli_meta, ["status"]).stdout
        resp = meta_generations.status(_FakeCtx(dmn_meta))
        assert resp.body.decode("utf-8") == cli  # timestamp-free
        out = json.loads(cli)
        assert out["current_generation"] == 0 and out["total_generations"] == 0
        # ensure_state lazily wrote the init file on a "read".
        assert (dmn_meta / "strategy-generations.yaml").exists()
        assert (cli_meta / "strategy-generations.yaml").exists()

    def test_history_after_open(self, tmp_path):
        from mind_api.src.meta import meta_generations
        cli_meta = tmp_path / "cli"
        dmn_meta = tmp_path / "dmn"
        _seed_meta(cli_meta)
        _seed_meta(dmn_meta)
        _run_cli(cli_meta, ["open"])
        cli = _run_cli(cli_meta, ["history"]).stdout
        meta_generations.open_generation(_FakeCtx(dmn_meta, body=b"{}"))
        resp = meta_generations.history_cmd(_FakeCtx(dmn_meta))
        # started/ended differ by wall-clock -> normalise the JSON timestamps.
        assert _norm_json_ts(resp.body.decode("utf-8")) == _norm_json_ts(cli)
        rows = json.loads(resp.body.decode("utf-8"))
        assert len(rows) == 1 and rows[0]["generation"] == 1
