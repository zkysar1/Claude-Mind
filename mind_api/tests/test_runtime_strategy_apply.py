"""POST /v1/meta/strategy-apply/match + /migrate.

Layers:
  1. HTTP round-trip (running_daemon): match + migrate wired; bad phase -> 400.
  2. Byte-compat (direct handler vs the REAL CLI strategy-apply.py):
       - match w/ increment (matches a heuristic): stdout (json indent=2) +
         goal-selection-strategy.yaml (times_applied bumped, CSafeDumper) +
         gate-firings.jsonl "pass" record (ts-normalised).
       - match no-match: stdout (count 0) + gate-firings "noop" record; NO
         strategy file write (no changelog).
       - phase filter (generation): only the generation file is touched.
       - migrate: backfills times_applied:0 on all heuristics; stdout {migrated}.

agent/session_id on the gate-firing record match because the CLI subprocess
inherits the test env and the daemon passes agent_name explicitly.
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
SA_PY = REPO_ROOT / "core" / "scripts" / "strategy-apply.py"

_GOAL_SEL = (
    "selection_heuristics:\n"
    "- id: sh-1\n"
    "  description: prefer goals about caching and retrieval performance\n"
    "- id: sh-2\n"
    "  description: deprioritize speculative exploration paths\n"
)
_ASP_GEN = (
    "generation_heuristics:\n"
    "- id: gh-1\n"
    "  description: generate aspirations around observability and metrics\n"
)


def _seed(meta: Path):
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "goal-selection-strategy.yaml").write_text(_GOAL_SEL, encoding="utf-8")
    (meta / "aspiration-generation-strategy.yaml").write_text(_ASP_GEN, encoding="utf-8")


def _run_cli(meta, args, check_rc=True):
    env = dict(os.environ)
    env["MIND_META"] = str(meta)
    env["MIND_AGENT"] = "alpha"
    env["MIND_AGENT_DIR"] = str(meta.parent / "agents" / "alpha")
    proc = subprocess.run(
        [sys.executable, str(SA_PY), *args],
        text=True, encoding="utf-8", env=env, cwd=str(REPO_ROOT),
        capture_output=True, timeout=60,
    )
    if check_rc:
        assert proc.returncode == 0, (
            f"CLI strategy-apply.py failed (rc={proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return proc


class _FakePaths:
    def __init__(self, meta):
        self.meta = meta
        self.world = meta.parent / "world"
        self.agent = meta.parent / "agents" / "alpha"
        self.project_root = REPO_ROOT


class _FakeCtx:
    def __init__(self, meta, body=None, headers=None):
        self.paths = _FakePaths(meta)
        self.query = {}
        self.body = body
        self.headers = headers if headers is not None else {"x-ayoai-agent": "alpha"}


_TS_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")


def _norm(text):
    return _TS_RE.sub("<TS>", text)


def _http(port, method, path, body=None):
    url = f"http://127.0.0.1:{port}{path}"
    data = body.encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, method=method, data=data)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.read().decode("utf-8")


# ---------------------------------------------------------------------------
# HTTP round-trip
# ---------------------------------------------------------------------------

def test_match_and_migrate_wired(running_daemon):
    _, port = running_daemon
    s, b = _http(port, "POST", "/v1/meta/strategy-apply/match",
                 body=json.dumps({"goal_keywords": "anything", "phase": "any"}))
    assert s == 200 and "matched" in json.loads(b)
    s, b = _http(port, "POST", "/v1/meta/strategy-apply/migrate", body="{}")
    assert s == 200 and "migrated" in json.loads(b)


def test_match_bad_phase_400(running_daemon):
    _, port = running_daemon
    try:
        _http(port, "POST", "/v1/meta/strategy-apply/match",
              body=json.dumps({"goal_keywords": "x", "phase": "bogus"}))
    except urllib.error.HTTPError as e:
        assert e.code == 400 and json.loads(e.read())["error"] == "invalid_param"
    else:
        raise AssertionError("expected 400 for bad phase")


# ---------------------------------------------------------------------------
# Byte-compat
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not SA_PY.exists(), reason="core/scripts/strategy-apply.py missing")
class TestByteCompat:
    def _meta(self, tmp_path, name):
        m = tmp_path / name / "meta"
        _seed(m)
        (m.parent / "agents" / "alpha").mkdir(parents=True, exist_ok=True)
        return m

    def test_match_increment(self, tmp_path):
        from mind_api.src.meta import strategy_apply
        cm = self._meta(tmp_path, "cmi")
        dm = self._meta(tmp_path, "dmi")
        cli = _run_cli(cm, ["--goal-keywords", "caching", "--increment"]).stdout
        resp = strategy_apply.match(_FakeCtx(dm, body=json.dumps({
            "goal_keywords": "caching", "increment": True}).encode("utf-8")))
        assert resp.status == 200
        assert resp.body.decode("utf-8") == cli  # indent=2 json, timestamp-free
        out = json.loads(cli)
        assert out["count"] == 1 and out["matched"][0]["id"] == "sh-1"
        assert out["matched"][0]["times_applied"] == 1
        # strategy file byte-compat (CSafeDumper, no timestamps).
        assert (dm / "goal-selection-strategy.yaml").read_text("utf-8") == \
            (cm / "goal-selection-strategy.yaml").read_text("utf-8")
        # gate-firings.jsonl "pass" record byte-compat (ts-normalised).
        assert _norm((dm / "gate-firings.jsonl").read_text("utf-8")) == \
            _norm((cm / "gate-firings.jsonl").read_text("utf-8"))
        rec = json.loads((dm / "gate-firings.jsonl").read_text("utf-8").strip())
        assert rec["gate_id"] == "strategy-apply" and rec["decision"] == "pass"
        assert rec["trigger_matched"] == "sh-1" and rec["agent"] == "alpha"

    def test_match_no_match_noop(self, tmp_path):
        from mind_api.src.meta import strategy_apply
        cm = self._meta(tmp_path, "cnm")
        dm = self._meta(tmp_path, "dnm")
        cli = _run_cli(cm, ["--goal-keywords", "zzznomatchzzz"]).stdout
        resp = strategy_apply.match(_FakeCtx(dm, body=json.dumps({
            "goal_keywords": "zzznomatchzzz"}).encode("utf-8")))
        assert resp.body.decode("utf-8") == cli
        out = json.loads(cli)
        assert out["count"] == 0 and out["keyword_tokens"] == ["zzznomatchzzz"]
        assert _norm((dm / "gate-firings.jsonl").read_text("utf-8")) == \
            _norm((cm / "gate-firings.jsonl").read_text("utf-8"))
        assert json.loads((dm / "gate-firings.jsonl").read_text("utf-8").strip())[
            "decision"] == "noop"
        # No match -> no strategy write -> no changelog (gate-firings is a
        # separate append, not a locked_write_yaml persist).
        assert not (dm / "changelog.jsonl").exists()
        # Seeded files untouched.
        assert (dm / "goal-selection-strategy.yaml").read_text("utf-8") == _GOAL_SEL

    def test_phase_filter_generation(self, tmp_path):
        from mind_api.src.meta import strategy_apply
        cm = self._meta(tmp_path, "cpf")
        dm = self._meta(tmp_path, "dpf")
        cli = _run_cli(cm, ["--goal-keywords", "observability",
                            "--phase", "generation", "--increment"]).stdout
        resp = strategy_apply.match(_FakeCtx(dm, body=json.dumps({
            "goal_keywords": "observability", "phase": "generation",
            "increment": True}).encode("utf-8")))
        assert resp.body.decode("utf-8") == cli
        out = json.loads(cli)
        assert out["count"] == 1 and out["matched"][0]["id"] == "gh-1"
        assert out["matched"][0]["phase"] == "generation"
        # Only the generation file touched; selection file untouched (matches seed).
        assert (dm / "aspiration-generation-strategy.yaml").read_text("utf-8") == \
            (cm / "aspiration-generation-strategy.yaml").read_text("utf-8")
        assert (dm / "goal-selection-strategy.yaml").read_text("utf-8") == _GOAL_SEL

    def test_migrate(self, tmp_path):
        from mind_api.src.meta import strategy_apply
        cm = self._meta(tmp_path, "cmg")
        dm = self._meta(tmp_path, "dmg")
        cli = _run_cli(cm, ["--migrate"]).stdout
        resp = strategy_apply.migrate(_FakeCtx(dm, body=b"{}"))
        assert resp.body.decode("utf-8") == cli
        out = json.loads(cli)
        assert out["migrated"] == 3  # sh-1, sh-2, gh-1
        assert (dm / "goal-selection-strategy.yaml").read_text("utf-8") == \
            (cm / "goal-selection-strategy.yaml").read_text("utf-8")
        loaded = yaml.safe_load((dm / "goal-selection-strategy.yaml").read_text("utf-8"))
        assert loaded["selection_heuristics"][0]["times_applied"] == 0
