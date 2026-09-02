"""GET/POST /v1/skill-evaluate/{read,report,underperforming,score}.

Layers:
  1. HTTP round-trip (running_daemon): routes wired, read full/skill/summary,
     report, underperforming, score POST + read-back, invalid grade -> 400.
  2. Byte-compat (direct handler vs the REAL CLI skill-evaluate.py):
       - read/report/underperforming: stdout byte-for-byte (ensure_ascii=False,
         indent=2). skill-not-found returns the error JSON at HTTP 200.
       - score: the "Scored ..." stdout line is timestamp-free -> byte-identical
         without normalisation. The WRITTEN skill-quality.yaml carries two
         datetime.now() stamps (entry.date, last_updated) -> normalised before
         comparison. Verifies DEFAULT yaml.Dumper (not CSafeDumper) and NO
         .history / changelog side-effects.

META-scoped, no header. CLI redirected via MIND_META; weights come from the
temp meta/skill-quality-strategy.yaml (or DEFAULT_WEIGHTS when absent).
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
EVAL_PY = REPO_ROOT / "core" / "scripts" / "skill-evaluate.py"

# Pre-populated skill-quality.yaml exercising read/report/underperforming.
_SEED = {
    "last_updated": "2026-05-01T00:00:00",
    "skills": {
        "skill-hi": {
            "evaluations": [
                {"goal_id": "g-1", "date": "2026-05-01T10:00:00",
                 "safety": 1.0, "completeness": 1.0, "executability": 1.0,
                 "maintainability": 1.0, "cost_awareness": 1.0, "overall": 1.0}],
            "aggregate": {"safety": 1.0, "completeness": 1.0, "executability": 1.0,
                          "maintainability": 1.0, "cost_awareness": 1.0, "overall": 1.0},
            "total_evaluations": 1,
        },
        "skill-lo": {
            "evaluations": [
                {"goal_id": "g-2", "date": "2026-05-01T10:00:00",
                 "safety": 0.0, "completeness": 0.5, "executability": 0.0,
                 "maintainability": 0.5, "cost_awareness": 0.0, "overall": 0.2}],
            "aggregate": {"safety": 0.0, "completeness": 0.5, "executability": 0.0,
                          "maintainability": 0.5, "cost_awareness": 0.0, "overall": 0.2},
            "total_evaluations": 1,
        },
    },
}


def _seed_meta(tmp_path, name="meta", data=None):
    meta = tmp_path / name
    meta.mkdir(parents=True, exist_ok=True)
    if data is not None:
        (meta / "skill-quality.yaml").write_text(
            yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
            encoding="utf-8")
    return meta


def _run_cli(meta, args, agent="alpha", check_rc=True):
    env = dict(os.environ)
    env["MIND_META"] = str(meta)
    env["MIND_WORLD"] = str(meta.parent / "world")
    env["MIND_AGENT"] = agent
    env["MIND_AGENT_DIR"] = str(meta.parent / "agents" / agent)
    proc = subprocess.run(
        [sys.executable, str(EVAL_PY), *args],
        text=True, encoding="utf-8", env=env, cwd=str(REPO_ROOT),
        capture_output=True, timeout=60,
    )
    if check_rc:
        assert proc.returncode == 0, (
            f"CLI skill-evaluate.py failed (rc={proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return proc


def _cli_judge_from_env():
    """What the CLI subprocess will resolve for judge provenance ().

    _run_cli hands the subprocess a copy of THIS process's environment, so the
    CLI's own resolver run here yields exactly what it will resolve there. Used
    by the byte-compat harness to feed the daemon equivalent input, since the
    daemon takes these in the body rather than from an environment it must not
    read.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("skill_evaluate_cli_judge",
                                                  EVAL_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._judge_from_env()


class _FakePaths:
    def __init__(self, meta):
        self.meta = meta
        self.project_root = REPO_ROOT


class _FakeCtx:
    def __init__(self, meta, query=None, body=None, headers=None):
        self.paths = _FakePaths(meta)
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

def test_read_empty(running_daemon):
    _, port = running_daemon
    status, body = _http(port, "GET", "/v1/skill-evaluate/read")
    assert status == 200 and isinstance(json.loads(body), dict)


def test_report_empty(running_daemon):
    _, port = running_daemon
    status, body = _http(port, "GET", "/v1/skill-evaluate/report")
    out = json.loads(body)
    assert out["summary"]["total_skills_evaluated"] == 0 and out["alerts"] == []


def test_score_then_read(running_daemon):
    _, port = running_daemon
    rel = {"skill": "rt-skill", "goal": "g-rt-1", "safety": "good",
           "completeness": "average", "executability": "poor",
           "maintainability": "good", "cost_awareness": "average"}
    status, body = _http(port, "POST", "/v1/skill-evaluate/score", body=json.dumps(rel))
    assert status == 200
    assert body.startswith("Scored rt-skill: overall ")
    status, body = _http(port, "GET", "/v1/skill-evaluate/read", {"skill": "rt-skill"})
    assert status == 200
    rec = json.loads(body)
    assert rec["total_evaluations"] == 1
    assert rec["evaluations"][0]["safety"] == 1.0


def test_score_invalid_grade_400(running_daemon):
    _, port = running_daemon
    rel = {"skill": "x", "goal": "g", "safety": "excellent", "completeness": "good",
           "executability": "good", "maintainability": "good", "cost_awareness": "good"}
    try:
        _http(port, "POST", "/v1/skill-evaluate/score", body=json.dumps(rel))
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "invalid_grade"
    else:
        raise AssertionError("expected 400 for invalid grade")


def test_read_skill_not_found_is_200(running_daemon):
    _, port = running_daemon
    status, body = _http(port, "GET", "/v1/skill-evaluate/read", {"skill": "nope-zzz"})
    assert status == 200  # CLI prints error JSON and returns normally, not 404
    assert "not found" in json.loads(body)["error"]


# ---------------------------------------------------------------------------
# Byte-compat: reads
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not EVAL_PY.exists(), reason="core/scripts/skill-evaluate.py missing")
class TestReadByteCompat:
    def _check(self, tmp_path, cli_args, handler, query=None):
        from mind_api.src.meta import skill_evaluate
        meta = _seed_meta(tmp_path, data=_SEED)
        cli = _run_cli(meta, cli_args).stdout
        resp = handler(_FakeCtx(meta, query=query))
        assert resp.body.decode("utf-8") == cli

    def test_read_full(self, tmp_path):
        from mind_api.src.meta import skill_evaluate
        self._check(tmp_path, ["read"], skill_evaluate.read)

    def test_read_skill(self, tmp_path):
        from mind_api.src.meta import skill_evaluate
        self._check(tmp_path, ["read", "--skill", "skill-hi"], skill_evaluate.read,
                    {"skill": "skill-hi"})

    def test_read_skill_missing(self, tmp_path):
        from mind_api.src.meta import skill_evaluate
        self._check(tmp_path, ["read", "--skill", "ghost"], skill_evaluate.read,
                    {"skill": "ghost"})

    def test_read_all_summary(self, tmp_path):
        from mind_api.src.meta import skill_evaluate
        self._check(tmp_path, ["read", "--all", "--summary"], skill_evaluate.read,
                    {"all": "1", "summary": "1"})

    def test_report(self, tmp_path):
        from mind_api.src.meta import skill_evaluate
        self._check(tmp_path, ["report"], skill_evaluate.report)

    def test_underperforming_default(self, tmp_path):
        from mind_api.src.meta import skill_evaluate
        self._check(tmp_path, ["underperforming"], skill_evaluate.underperforming)

    def test_underperforming_threshold(self, tmp_path):
        from mind_api.src.meta import skill_evaluate
        self._check(tmp_path, ["underperforming", "--threshold", "0.3"],
                    skill_evaluate.underperforming, {"threshold": "0.3"})


# ---------------------------------------------------------------------------
# Byte-compat: score (write)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not EVAL_PY.exists(), reason="core/scripts/skill-evaluate.py missing")
class TestScoreByteCompat:
    def test_score_stdout_and_file(self, tmp_path):
        from mind_api.src.meta import skill_evaluate
        cli_meta = _seed_meta(tmp_path, "cli_meta", data=dict(_SEED))
        dmn_meta = _seed_meta(tmp_path, "dmn_meta", data=dict(_SEED))
        args = ["score", "--skill", "new-skill", "--goal", "g-9-1",
                "--safety", "good", "--completeness", "average",
                "--executability", "poor", "--maintainability", "good",
                "--cost-awareness", "average"]
        cli_out = _run_cli(cli_meta, args).stdout
        # Judge provenance travels by a DIFFERENT transport on each side
        # (): the CLI runs in the judge's own process and resolves it
        # from that environment, while the daemon is a long-lived process and
        # must be told, in the body. Byte-compat is "same INPUTS -> same
        # bytes", so the harness has to supply the daemon the values the CLI
        # just resolved for itself -- otherwise it compares a populated judge
        # against an honestly-absent one and reports a parity break that is
        # really a transport difference (guard-1189).
        judge_model, harness = skill_evaluate._judge_provenance(
            *_cli_judge_from_env())
        body = {"skill": "new-skill", "goal": "g-9-1", "safety": "good",
                "completeness": "average", "executability": "poor",
                "maintainability": "good", "cost_awareness": "average",
                "judge_model": judge_model, "harness": harness}
        resp = skill_evaluate.score(_FakeCtx(dmn_meta, body=json.dumps(body).encode("utf-8")))
        # stdout line is timestamp-free -> byte-identical.
        assert resp.body.decode("utf-8") == cli_out
        # Written YAML matches modulo the two datetime.now() stamps.
        cli_yaml = (cli_meta / "skill-quality.yaml").read_text(encoding="utf-8")
        dmn_yaml = (dmn_meta / "skill-quality.yaml").read_text(encoding="utf-8")
        assert _norm(dmn_yaml) == _norm(cli_yaml)
        # Confirm DEFAULT yaml.Dumper round-trips and the score landed.
        loaded = yaml.safe_load(dmn_yaml)
        assert loaded["skills"]["new-skill"]["total_evaluations"] == 1
        assert loaded["skills"]["new-skill"]["evaluations"][0]["safety"] == 1.0
        # No history/changelog artefacts on the daemon side.
        assert not (dmn_meta / ".history").exists()
        assert not (dmn_meta / "changelog.jsonl").exists()

    def test_cost_awareness_hyphen_form(self, tmp_path):
        from mind_api.src.meta import skill_evaluate
        meta = _seed_meta(tmp_path, data=dict(_SEED))
        body = {"skill": "hy-skill", "goal": "g-h-1", "safety": "good",
                "completeness": "good", "executability": "good",
                "maintainability": "good", "cost-awareness": "poor"}
        resp = skill_evaluate.score(_FakeCtx(meta, body=json.dumps(body).encode("utf-8")))
        assert resp.status == 200
        loaded = yaml.safe_load((meta / "skill-quality.yaml").read_text(encoding="utf-8"))
        assert loaded["skills"]["hy-skill"]["evaluations"][0]["cost_awareness"] == 0.0

    def test_rolling_window_cap(self, tmp_path):
        # 21 scores -> only the last 20 evaluations retained.
        from mind_api.src.meta import skill_evaluate
        meta = _seed_meta(tmp_path, data={})
        for i in range(21):
            body = {"skill": "cap-skill", "goal": f"g-{i}", "safety": "good",
                    "completeness": "good", "executability": "good",
                    "maintainability": "good", "cost_awareness": "good"}
            skill_evaluate.score(_FakeCtx(meta, body=json.dumps(body).encode("utf-8")))
        loaded = yaml.safe_load((meta / "skill-quality.yaml").read_text(encoding="utf-8"))
        sk = loaded["skills"]["cap-skill"]
        assert len(sk["evaluations"]) == 20
        assert sk["total_evaluations"] == 21
