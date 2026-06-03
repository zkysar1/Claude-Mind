"""7 meta-backpressure routes: monitor/check/graduate/status/cooldown-check +
evolution-monitor/evolution-check.

Layers:
  1. HTTP round-trip (running_daemon): routes wired; graduate not-found -> 200
     {error}; check bad float -> 400; evolution-monitor bad kind -> 400.
  2. Byte-compat (direct handler vs the REAL CLI meta-backpressure.py):
       - monitor: stdout + backpressure.yaml (timestamp-normalised).
       - check (single below-threshold sample, no rollback): stdout empties +
         count, byte-identical; backpressure.yaml normalised.
       - graduate success: stdout + file; graduate NOT-FOUND: stdout {error}
         byte-identical AND no write (changelog.jsonl absent — BLOCKING quirk #1).
       - status: stdout normalised after a monitor create.
       - cooldown-check on empty history: stdout empties byte-identical.
       - evolution-monitor: stdout (signal_count) + file (META-scoped, quirk #2).
       - evolution-check: empty path (evolution-snapshot-metrics.py rc=64 ->
         every monitor skipped) — stdout empties + count, byte-identical.

config (regression/graduation windows) is read from the REAL core/config/
meta.yaml via project_root=REPO_ROOT for both CLI and daemon -> identical.
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
BP_PY = REPO_ROOT / "core" / "scripts" / "meta-backpressure.py"


def _setup(tmp_path, name="x"):
    base = tmp_path / name
    meta = base / "meta"
    world = base / "world"
    agent = base / "agents" / "alpha"
    for d in (meta, world, agent):
        d.mkdir(parents=True, exist_ok=True)
    return meta, world, agent


def _run_cli(meta, world, agent, args, check_rc=True):
    env = dict(os.environ)
    env["MIND_META"] = str(meta)
    env["MIND_WORLD"] = str(world)
    env["MIND_AGENT"] = "alpha"
    env["MIND_AGENT_DIR"] = str(agent)
    proc = subprocess.run(
        [sys.executable, str(BP_PY), *args],
        text=True, encoding="utf-8", env=env, cwd=str(REPO_ROOT),
        capture_output=True, timeout=60,
    )
    if check_rc:
        assert proc.returncode == 0, (
            f"CLI meta-backpressure.py failed (rc={proc.returncode}):\n"
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


# Blanket ISO second-precision timestamp normaliser — handles both YAML
# (created: '2026-...') and JSON ("created": "2026-...") forms. The only
# values of this shape in backpressure output are created/rolled_back_at/
# skipped_at/ts; CLI vs daemon stamps can differ by a wall-clock second.
_TS_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")


def _norm(text):
    return _TS_RE.sub("<TS>", text)


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

def test_monitor_then_status_wired(running_daemon):
    _, port = running_daemon
    s, b = _http(port, "POST", "/v1/meta/backpressure/monitor", body=json.dumps({
        "change_id": "mc-rt-1", "file": "goal-selection-strategy.yaml",
        "field": "weights.novelty", "old": "1.0", "new": "1.5", "baseline": 0.5}))
    assert s == 200 and json.loads(b)["status"] == "created"
    s, b = _http(port, "GET", "/v1/meta/backpressure/status")
    assert s == 200 and json.loads(b)["active_count"] == 1


def test_graduate_not_found_200(running_daemon):
    _, port = running_daemon
    s, b = _http(port, "POST", "/v1/meta/backpressure/graduate",
                 body=json.dumps({"change_id": "mc-ghost-zzz"}))
    assert s == 200
    assert json.loads(b)["error"] == "Monitor mc-ghost-zzz not found"


def test_check_bad_float_400(running_daemon):
    _, port = running_daemon
    try:
        _http(port, "POST", "/v1/meta/backpressure/check",
              body=json.dumps({"learning_value": "abc"}))
    except urllib.error.HTTPError as e:
        assert e.code == 400 and json.loads(e.read())["error"] == "invalid_param"
    else:
        raise AssertionError("expected 400 for bad learning_value")


def test_evolution_monitor_bad_kind_400(running_daemon):
    _, port = running_daemon
    try:
        _http(port, "POST", "/v1/meta/backpressure/evolution-monitor",
              body=json.dumps({"monitor_kind": "bogus", "revision_id": "r1",
                               "file_path": "x", "history_snapshot": "s",
                               "baseline_vector": {}}))
    except urllib.error.HTTPError as e:
        assert e.code == 400
    else:
        raise AssertionError("expected 400 for bad monitor_kind")


def test_evolution_check_wired(running_daemon):
    _, port = running_daemon
    s, b = _http(port, "POST", "/v1/meta/backpressure/evolution-check", body="{}")
    assert s == 200
    out = json.loads(b)
    assert out["rollback_actions"] == [] and out["active_monitors_count"] == 0


# ---------------------------------------------------------------------------
# Byte-compat
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not BP_PY.exists(), reason="core/scripts/meta-backpressure.py missing")
class TestByteCompat:
    def test_monitor(self, tmp_path):
        from mind_api.src.meta import meta_backpressure
        cm, cw, ca = _setup(tmp_path, "cli")
        dm, dw, da = _setup(tmp_path, "dmn")
        args = ["monitor", "--change-id", "mc-001", "--file",
                "goal-selection-strategy.yaml", "--field", "weights.novelty",
                "--old", "1.0", "--new", "1.5", "--baseline", "0.5"]
        cli = _run_cli(cm, cw, ca, args).stdout
        resp = meta_backpressure.monitor(_FakeCtx(dm, dw, da, body=json.dumps({
            "change_id": "mc-001", "file": "goal-selection-strategy.yaml",
            "field": "weights.novelty", "old": "1.0", "new": "1.5",
            "baseline": 0.5}).encode("utf-8")))
        assert resp.status == 200
        assert resp.body.decode("utf-8") == cli
        cy = (cm / "backpressure.yaml").read_text("utf-8")
        dy = (dm / "backpressure.yaml").read_text("utf-8")
        assert _norm(dy) == _norm(cy)
        mon = yaml.safe_load(dy)["active_monitors"][0]
        assert mon["old_value"] == 1.0 and mon["new_value"] == 1.5

    def test_check_no_rollback(self, tmp_path):
        from mind_api.src.meta import meta_backpressure
        cm, cw, ca = _setup(tmp_path, "cli")
        dm, dw, da = _setup(tmp_path, "dmn")
        mon_args = ["monitor", "--change-id", "mc-002", "--file", "f.yaml",
                    "--field", "g", "--old", "1", "--new", "2", "--baseline", "0.5"]
        _run_cli(cm, cw, ca, mon_args)
        meta_backpressure.monitor(_FakeCtx(dm, dw, da, body=json.dumps({
            "change_id": "mc-002", "file": "f.yaml", "field": "g",
            "old": "1", "new": "2", "baseline": 0.5}).encode("utf-8")))
        cli = _run_cli(cm, cw, ca, ["check", "--learning-value", "0.9"]).stdout
        resp = meta_backpressure.check(_FakeCtx(dm, dw, da, body=json.dumps(
            {"learning_value": 0.9}).encode("utf-8")))
        assert resp.body.decode("utf-8") == cli  # empties + count, timestamp-free
        out = json.loads(cli)
        assert out["rollback_actions"] == [] and out["active_monitors_count"] == 1
        assert _norm((dm / "backpressure.yaml").read_text("utf-8")) == \
            _norm((cm / "backpressure.yaml").read_text("utf-8"))

    def test_graduate_success(self, tmp_path):
        from mind_api.src.meta import meta_backpressure
        cm, cw, ca = _setup(tmp_path, "cli")
        dm, dw, da = _setup(tmp_path, "dmn")
        seed = {"version": 1, "rollback_history": [], "active_monitors": [
            {"meta_change_id": "mc-g", "strategy_file": "f", "field": "x",
             "status": "monitoring"}]}
        for m in (cm, dm):
            (m / "backpressure.yaml").write_text(yaml.safe_dump(seed, sort_keys=False),
                                                 encoding="utf-8")
        cli = _run_cli(cm, cw, ca, ["graduate", "--change-id", "mc-g"]).stdout
        resp = meta_backpressure.graduate(_FakeCtx(dm, dw, da, body=json.dumps(
            {"change_id": "mc-g"}).encode("utf-8")))
        assert resp.body.decode("utf-8") == cli
        assert json.loads(cli)["status"] == "graduated"
        assert _norm((dm / "backpressure.yaml").read_text("utf-8")) == \
            _norm((cm / "backpressure.yaml").read_text("utf-8"))
        # graduated monitor removed from active list.
        assert yaml.safe_load((dm / "backpressure.yaml").read_text("utf-8"))[
            "active_monitors"] == []

    def test_graduate_not_found_no_write(self, tmp_path):
        from mind_api.src.meta import meta_backpressure
        cm, cw, ca = _setup(tmp_path, "cli")
        dm, dw, da = _setup(tmp_path, "dmn")
        # Seed WITH version so ensure_state does not lazily write -> any write
        # must come from the graduate persist, which must NOT happen on not-found.
        seed = {"version": 1, "rollback_history": [], "active_monitors": [
            {"meta_change_id": "mc-real", "strategy_file": "f", "field": "x",
             "status": "monitoring"}]}
        for m in (cm, dm):
            (m / "backpressure.yaml").write_text(yaml.safe_dump(seed, sort_keys=False),
                                                 encoding="utf-8")
        cli = _run_cli(cm, cw, ca, ["graduate", "--change-id", "mc-ghost"]).stdout
        resp = meta_backpressure.graduate(_FakeCtx(dm, dw, da, body=json.dumps(
            {"change_id": "mc-ghost"}).encode("utf-8")))
        assert resp.status == 200
        assert resp.body.decode("utf-8") == cli
        assert json.loads(cli)["error"] == "Monitor mc-ghost not found"
        # BLOCKING quirk #1: no persist -> no changelog, no history, file unchanged.
        assert not (dm / "changelog.jsonl").exists()
        assert not (cm / "changelog.jsonl").exists()
        assert (dm / "backpressure.yaml").read_text("utf-8") == \
            yaml.safe_dump(seed, sort_keys=False)

    def test_status_after_monitor(self, tmp_path):
        from mind_api.src.meta import meta_backpressure
        cm, cw, ca = _setup(tmp_path, "cli")
        dm, dw, da = _setup(tmp_path, "dmn")
        margs = ["monitor", "--change-id", "mc-s", "--file", "f", "--field", "x",
                 "--old", "1", "--new", "2", "--baseline", "0.5"]
        _run_cli(cm, cw, ca, margs)
        meta_backpressure.monitor(_FakeCtx(dm, dw, da, body=json.dumps({
            "change_id": "mc-s", "file": "f", "field": "x", "old": "1",
            "new": "2", "baseline": 0.5}).encode("utf-8")))
        cli = _run_cli(cm, cw, ca, ["status"]).stdout
        resp = meta_backpressure.status(_FakeCtx(dm, dw, da))
        assert _norm(resp.body.decode("utf-8")) == _norm(cli)
        assert json.loads(cli)["active_count"] == 1

    def test_cooldown_check_empty(self, tmp_path):
        from mind_api.src.meta import meta_backpressure
        cm, cw, ca = _setup(tmp_path, "cli")
        dm, dw, da = _setup(tmp_path, "dmn")
        cli = _run_cli(cm, cw, ca, ["cooldown-check"]).stdout
        resp = meta_backpressure.cooldown_check(_FakeCtx(dm, dw, da))
        assert resp.body.decode("utf-8") == cli
        out = json.loads(cli)
        assert out["in_cooldown"] == [] and out["cooldown_window"] == 20

    def test_evolution_monitor(self, tmp_path):
        from mind_api.src.meta import meta_backpressure
        cm, cw, ca = _setup(tmp_path, "cli")
        dm, dw, da = _setup(tmp_path, "dmn")
        bv = json.dumps({"sig_a": 0.8, "sig_b": 0.6})
        args = ["evolution-monitor", "--monitor-kind", "skill_evolution",
                "--revision-id", "rev-001", "--file-path", "skills/x/SKILL.md",
                "--history-snapshot", "/tmp/snap.md", "--baseline-vector", bv]
        cli = _run_cli(cm, cw, ca, args).stdout
        resp = meta_backpressure.evolution_monitor(_FakeCtx(dm, dw, da, body=json.dumps({
            "monitor_kind": "skill_evolution", "revision_id": "rev-001",
            "file_path": "skills/x/SKILL.md", "history_snapshot": "/tmp/snap.md",
            "baseline_vector": {"sig_a": 0.8, "sig_b": 0.6}, "agent": "alpha"}).encode("utf-8")))
        assert resp.status == 200
        assert resp.body.decode("utf-8") == cli
        assert json.loads(cli)["signal_count"] == 2
        assert _norm((dm / "backpressure.yaml").read_text("utf-8")) == \
            _norm((cm / "backpressure.yaml").read_text("utf-8"))

    def test_evolution_check_empty_path(self, tmp_path):
        from mind_api.src.meta import meta_backpressure
        cm, cw, ca = _setup(tmp_path, "cli")
        dm, dw, da = _setup(tmp_path, "dmn")
        bv = json.dumps({"sig_a": 0.8})
        emon = ["evolution-monitor", "--monitor-kind", "rule_evolution",
                "--revision-id", "rev-ec", "--file-path", "rules/x.md",
                "--history-snapshot", "/tmp/s.md", "--baseline-vector", bv]
        _run_cli(cm, cw, ca, emon)
        meta_backpressure.evolution_monitor(_FakeCtx(dm, dw, da, body=json.dumps({
            "monitor_kind": "rule_evolution", "revision_id": "rev-ec",
            "file_path": "rules/x.md", "history_snapshot": "/tmp/s.md",
            "baseline_vector": {"sig_a": 0.8}, "agent": "alpha"}).encode("utf-8")))
        # evolution-snapshot-metrics.py returns rc=64 -> monitor skipped -> empties.
        cli = _run_cli(cm, cw, ca, ["evolution-check"]).stdout
        resp = meta_backpressure.evolution_check(_FakeCtx(dm, dw, da, body=b"{}"))
        assert resp.body.decode("utf-8") == cli
        out = json.loads(cli)
        assert out["rollback_actions"] == [] and out["graduated"] == []
        assert out["active_monitors_count"] == 1  # the still-monitoring evolution monitor
        assert _norm((dm / "backpressure.yaml").read_text("utf-8")) == \
            _norm((cm / "backpressure.yaml").read_text("utf-8"))
