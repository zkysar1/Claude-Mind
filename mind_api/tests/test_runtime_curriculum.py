"""GET/POST /v1/curriculum/{status,evaluate,promote,contract-check,audit}.

Two layers:
  1. HTTP round-trip (running_daemon): endpoints wired, agent-header gate,
     unconfigured/configured status, evaluate/promote writes land on disk,
     contract-check permitted/denied/unknown, audit consistency.
  2. Byte-compat (direct handler vs the REAL CLI curriculum.py):
     - status / contract-check / audit: STDOUT compared byte-for-byte (no now()
       stamps → deterministic).
     - evaluate / promote: the now() last_checked/date/entered/exited stamps
       differ between any two runs by construction, so STDOUT and the written
       curriculum.yaml / curriculum-promotions.jsonl are compared after
       normalising ISO timestamps to <TS> (proves DUMPER/key-order/indent/
       quoting byte-identity) PLUS a structural parse comparison excluding the
       volatile keys.

The CLI subprocess uses MIND_AGENT_DIR (the documented unit-test override) so
it writes to a temp agent dir, never agents/alpha/. Both sides read the SAME
framework curriculum.yaml (CLI via script-derived CONFIG_DIR == REPO_ROOT/core/
config; daemon via project_root=REPO_ROOT) so unlock_defaults match.
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

REPO_ROOT = Path(__file__).resolve().parents[2]
CURRICULUM_PY = REPO_ROOT / "core" / "scripts" / "curriculum.py"

_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# A two-stage curriculum. cur-01 has all THREE file-reading gate types
# (count_check, log_scan, metric_threshold) to exercise the corrected
# AGENT_DIR-resolution trap. Seeded agent files make all three pass so promote
# advances to cur-02.
_CURRICULUM_YAML = (
    "current_stage: cur-01\n"
    "stages:\n"
    "  - id: cur-01\n"
    "    name: Foundation\n"
    "    description: First developmental stage\n"
    "    unlocks:\n"
    "      allow_self_edits: false\n"
    "    graduation_gates:\n"
    "      - id: g-count\n"
    "        type: count_check\n"
    "        file: aspirations.jsonl\n"
    "        field: status\n"
    "        value: completed\n"
    "        operator: '>='\n"
    "        threshold: 1\n"
    "        description: at least one completed\n"
    "      - id: g-log\n"
    "        type: log_scan\n"
    "        log_file: execution-log.jsonl\n"
    "        match_field: event\n"
    "        match_value: did-thing\n"
    "        min_count: 1\n"
    "        description: one logged event\n"
    "      - id: g-metric\n"
    "        type: metric_threshold\n"
    "        metric: developmental-stage.current_assessment.average_competence\n"
    "        operator: '>='\n"
    "        threshold: 0.5\n"
    "        description: competence threshold\n"
    "  - id: cur-02\n"
    "    name: Growth\n"
    "    description: Second developmental stage\n"
    "    unlocks:\n"
    "      allow_self_edits: true\n"
    "      allow_forge_skill: true\n"
    "    graduation_gates: []\n"
    "stage_history:\n"
    "  - stage_id: cur-01\n"
    "    entered: '2026-01-01T00:00:00'\n"
    "    exited: null\n"
)


def _seed_agent(agent_dir: Path, *, completed: int = 1):
    """Seed curriculum.yaml + the three gate-input files. completed=0 makes the
    count_check gate FAIL (for the not-all-passed promote path).

    Also seeds the WORLD competence inputs (g-115-2026 adjudication,
    g-115-2480): evaluate now RECOMPUTES average_competence from world
    evidence via refresh_competence_for_gates, overwriting the stored 0.7 —
    an empty world derives 0.0 and the g-metric gate fails. The stored value
    below remains only the pre-refresh baseline (and what promote — which
    does not refresh — reads). Seeded inputs: pipeline 5/5 resolved +
    rb 20/20 active + 25/25 completed world goals → recomputed 0.75 ≥ 0.5."""
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "curriculum.yaml").write_text(_CURRICULUM_YAML, encoding="utf-8")
    asp_lines = "".join(
        json.dumps({"id": f"asp-{i}", "status": "completed"}) + "\n"
        for i in range(completed)
    )
    (agent_dir / "aspirations.jsonl").write_text(asp_lines, encoding="utf-8")
    (agent_dir / "execution-log.jsonl").write_text(
        json.dumps({"event": "did-thing"}) + "\n", encoding="utf-8")
    (agent_dir / "developmental-stage.yaml").write_text(
        "current_assessment:\n  average_competence: 0.7\n", encoding="utf-8")
    # World layout differs per harness: HTTP tests use <root>/agents/alpha with
    # world at <root>/world; the CLI byte-compat tmp layout puts world beside
    # the agent dir (_run_cli's MIND_WORLD = agent_dir.parent / "world").
    world = (agent_dir.parent.parent if agent_dir.parent.name == "agents"
             else agent_dir.parent) / "world"
    world.mkdir(parents=True, exist_ok=True)
    (world / "pipeline.jsonl").write_text("".join(
        json.dumps({"id": f"hyp-{i}", "stage": "resolved"}) + "\n"
        for i in range(5)), encoding="utf-8")
    (world / "reasoning-bank.jsonl").write_text("".join(
        json.dumps({"id": f"rb-{i}", "status": "active"}) + "\n"
        for i in range(20)), encoding="utf-8")
    (world / "aspirations.jsonl").write_text(json.dumps(
        {"id": "asp-w", "goals": [
            {"id": f"g-w-{i}", "status": "completed"} for i in range(25)]}
    ) + "\n", encoding="utf-8")


def _norm(s: str) -> str:
    return _TS_RE.sub("<TS>", s)


def _strip_ts(obj):
    """Recursively replace ISO-timestamp string values with <TS> for structural
    comparison (the only volatile content in evaluate/promote output)."""
    if isinstance(obj, dict):
        return {k: _strip_ts(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_ts(v) for v in obj]
    if isinstance(obj, str) and _TS_RE.fullmatch(obj):
        return "<TS>"
    return obj


def _run_cli(agent_dir: Path, args, allowed_rcs=(0,)) -> str:
    env = dict(os.environ)
    env["MIND_AGENT"] = "alpha"
    env["MIND_AGENT_DIR"] = str(agent_dir)
    env["MIND_WORLD"] = str(agent_dir.parent / "world")
    env["MIND_META"] = str(agent_dir.parent / "meta")
    (agent_dir.parent / "world").mkdir(parents=True, exist_ok=True)
    (agent_dir.parent / "meta").mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, str(CURRICULUM_PY), *args],
        text=True, encoding="utf-8", env=env, cwd=str(REPO_ROOT),
        capture_output=True, timeout=60,
    )
    assert proc.returncode in allowed_rcs, (
        f"CLI curriculum.py {args} rc={proc.returncode} not in {allowed_rcs}:\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return proc.stdout


class _FakePaths:
    def __init__(self, agent: Path, project_root: Path, agent_name="alpha"):
        self.agent = agent
        self.project_root = project_root
        # Mirrors _run_cli's MIND_WORLD convention (agent's sibling world/) —
        # the endpoint now reads ctx.paths.world + ctx.paths.agent_name for the
        # 6 competence refresh and _evaluate_gate (curriculum.py:404).
        self.world = agent.parent / "world"
        self.agent_name = agent_name


class _FakeCtx:
    def __init__(self, agent: Path, project_root: Path, query: dict, *,
                 agent_name="alpha"):
        self.paths = _FakePaths(agent, project_root, agent_name=agent_name or "alpha")
        self.query = query
        self.body = b""
        self.headers = {"x-mind-agent": agent_name} if agent_name else {}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _http(method, port, path, query=None, *, agent="alpha"):
    qs = ("?" + urllib.parse.urlencode(query)) if query else ""
    url = f"http://127.0.0.1:{port}{path}{qs}"
    req = urllib.request.Request(url, data=b"" if method == "POST" else None, method=method)
    if agent:
        req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


# ---------------------------------------------------------------------------
# HTTP round-trip
# ---------------------------------------------------------------------------

def test_status_unconfigured(running_daemon):
    # conftest agent has no curriculum.yaml.
    _, port = running_daemon
    status, body = _http("GET", port, "/v1/curriculum/status")
    assert status == 200
    assert json.loads(body)["configured"] is False


def test_status_requires_header(running_daemon):
    _, port = running_daemon
    try:
        _http("GET", port, "/v1/curriculum/status", agent=None)
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "missing_agent_header"
    else:
        raise AssertionError("expected 400 without X-Mind-Agent")


def test_status_configured(running_daemon):
    project_root, port = running_daemon
    _seed_agent(project_root / "agents" / "alpha")
    status, body = _http("GET", port, "/v1/curriculum/status")
    assert status == 200
    data = json.loads(body)
    assert data["configured"] is True
    assert data["current_stage"] == "cur-01"
    assert data["next_stage"] == "cur-02"
    assert data["gates_total"] == 3


def test_evaluate_roundtrip(running_daemon):
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    _seed_agent(agent_dir)
    status, body = _http("POST", port, "/v1/curriculum/evaluate")
    assert status == 200, body
    data = json.loads(body)
    assert data["all_passed"] is True
    assert data["gates_passed_count"] == 3
    # gate_status persisted to disk.
    import yaml
    state = yaml.safe_load((agent_dir / "curriculum.yaml").read_text(encoding="utf-8"))
    assert state["stages"][0]["gate_status"][0]["passed"] is True


def test_promote_roundtrip(running_daemon):
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    _seed_agent(agent_dir)
    status, body = _http("POST", port, "/v1/curriculum/promote")
    assert status == 200, body
    data = json.loads(body)
    assert data["promoted"] is True
    assert data["to_stage"] == "cur-02"
    # promotion log appended.
    promos = [json.loads(ln) for ln in
              (agent_dir / "curriculum-promotions.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert promos[-1]["to_stage"] == "cur-02"
    assert promos[-1]["actor"] == "curriculum.py"


def test_promote_not_all_passed(running_daemon):
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    _seed_agent(agent_dir, completed=0)  # count_check fails
    status, body = _http("POST", port, "/v1/curriculum/promote")
    assert status == 200, body
    data = json.loads(body)
    assert data["promoted"] is False
    assert data["reason"] == "not all gates passed"


def test_contract_check_unknown_action_permitted(running_daemon):
    project_root, port = running_daemon
    _seed_agent(project_root / "agents" / "alpha")
    status, body = _http("GET", port, "/v1/curriculum/contract-check",
                         {"action": "allow_unknown_xyz"})
    assert status == 200
    assert json.loads(body)["permitted"] is True  # unknown -> default True


def test_contract_check_denied(running_daemon):
    project_root, port = running_daemon
    _seed_agent(project_root / "agents" / "alpha")
    # cur-01 has allow_self_edits: false; later stage unlocks it -> permitted False.
    status, body = _http("GET", port, "/v1/curriculum/contract-check",
                         {"action": "allow_self_edits"})
    assert status == 200  # denial is 200 with permitted=false, NOT 4xx
    data = json.loads(body)
    assert data["permitted"] is False
    assert data.get("unlocks_at") == "Growth"


def test_contract_check_missing_action(running_daemon):
    project_root, port = running_daemon
    _seed_agent(project_root / "agents" / "alpha")
    try:
        _http("GET", port, "/v1/curriculum/contract-check")
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "missing_param"
    else:
        raise AssertionError("expected 400 without action")


def test_audit_roundtrip(running_daemon):
    project_root, port = running_daemon
    _seed_agent(project_root / "agents" / "alpha")
    status, body = _http("GET", port, "/v1/curriculum/audit")
    assert status == 200
    data = json.loads(body)
    assert data["status"] == "ok"
    assert data["issues"] == []


# ---------------------------------------------------------------------------
# Byte-compat: daemon handler body == real CLI stdout (+ written files)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not CURRICULUM_PY.exists(), reason="core/scripts/curriculum.py missing")
class TestByteCompat:
    def _agents(self, tmp_path, **kw):
        cli = tmp_path / "cli-agent"
        dae = tmp_path / "dae-agent"
        _seed_agent(cli, **kw)
        _seed_agent(dae, **kw)
        return cli, dae

    def _daemon(self, handler_name, agent_dir, query=None):
        from mind_api.src.endpoints import curriculum
        handler = getattr(curriculum, handler_name)
        resp = handler(_FakeCtx(agent_dir, REPO_ROOT, query or {}))
        return resp.body.decode("utf-8")

    # --- deterministic (no now()) -> direct stdout byte-compare -------------

    def test_status_byte_compat(self, tmp_path):
        cli, dae = self._agents(tmp_path)
        assert self._daemon("status", dae) == _run_cli(cli, ["status"])

    def test_status_unconfigured_byte_compat(self, tmp_path):
        cli = tmp_path / "cli-agent"
        dae = tmp_path / "dae-agent"
        cli.mkdir(); dae.mkdir()
        assert self._daemon("status", dae) == _run_cli(cli, ["status"])

    def test_audit_byte_compat(self, tmp_path):
        cli, dae = self._agents(tmp_path)
        assert self._daemon("audit", dae) == _run_cli(cli, ["audit"])

    def test_audit_with_issues_byte_compat(self, tmp_path):
        # Break consistency: current_stage points at a missing stage.
        cli = tmp_path / "cli-agent"
        dae = tmp_path / "dae-agent"
        for a in (cli, dae):
            a.mkdir()
            (a / "curriculum.yaml").write_text(
                "current_stage: cur-99\n"
                "stages:\n"
                "  - id: cur-01\n    name: F\n    description: d\n",
                encoding="utf-8")
        assert self._daemon("audit", dae) == _run_cli(cli, ["audit"])

    def test_contract_check_permitted_byte_compat(self, tmp_path):
        cli, dae = self._agents(tmp_path)
        d = self._daemon("contract_check", dae, {"action": "allow_unknown_xyz"})
        c = _run_cli(cli, ["contract-check", "--action", "allow_unknown_xyz"], allowed_rcs=(0,))
        assert d == c

    def test_contract_check_denied_byte_compat(self, tmp_path):
        cli, dae = self._agents(tmp_path)
        d = self._daemon("contract_check", dae, {"action": "allow_self_edits"})
        # CLI exits 1 on not-permitted; that is a shell signal, body still matches.
        c = _run_cli(cli, ["contract-check", "--action", "allow_self_edits"], allowed_rcs=(0, 1))
        assert d == c

    # --- now()-stamped -> timestamp-normalized compare ----------------------

    def test_evaluate_byte_compat(self, tmp_path):
        import yaml
        cli, dae = self._agents(tmp_path)
        cli_out = _run_cli(cli, ["evaluate"])
        dae_out = self._daemon("evaluate", dae)
        # stdout: identical modulo last_checked timestamps.
        assert _norm(dae_out) == _norm(cli_out)
        assert _strip_ts(json.loads(dae_out)) == _strip_ts(json.loads(cli_out))
        # written curriculum.yaml: byte-identical modulo timestamps.
        cli_y = (cli / "curriculum.yaml").read_text(encoding="utf-8")
        dae_y = (dae / "curriculum.yaml").read_text(encoding="utf-8")
        assert _norm(dae_y) == _norm(cli_y)
        # structural (full parse) equality excluding timestamps.
        assert _strip_ts(yaml.safe_load(dae_y)) == _strip_ts(yaml.safe_load(cli_y))

    def test_promote_byte_compat(self, tmp_path):
        import yaml
        cli, dae = self._agents(tmp_path)
        cli_out = _run_cli(cli, ["promote"])
        dae_out = self._daemon("promote", dae)
        assert _norm(dae_out) == _norm(cli_out)
        assert _strip_ts(json.loads(dae_out)) == _strip_ts(json.loads(cli_out))
        # curriculum.yaml after promotion.
        cli_y = (cli / "curriculum.yaml").read_text(encoding="utf-8")
        dae_y = (dae / "curriculum.yaml").read_text(encoding="utf-8")
        assert _norm(dae_y) == _norm(cli_y)
        assert _strip_ts(yaml.safe_load(dae_y)) == _strip_ts(yaml.safe_load(cli_y))
        # curriculum-promotions.jsonl: the appended line, byte-identical mod TS.
        cli_p = (cli / "curriculum-promotions.jsonl").read_text(encoding="utf-8")
        dae_p = (dae / "curriculum-promotions.jsonl").read_text(encoding="utf-8")
        assert _norm(dae_p) == _norm(cli_p)
        # Confirms ensure_ascii=True + actor literal + key order.
        assert json.loads(dae_p.splitlines()[-1])["actor"] == "curriculum.py"

    def test_promote_not_all_passed_byte_compat(self, tmp_path):
        # No now() in this branch -> direct byte-compare.
        cli, dae = self._agents(tmp_path, completed=0)
        dae_out = self._daemon("promote", dae)
        cli_out = _run_cli(cli, ["promote"])
        assert dae_out == cli_out
        # No file write on the failure path.
        assert not (dae / "curriculum-promotions.jsonl").exists()
