"""Batch 4 bespoke write endpoints: HTTP round-trip + byte-compat.

Covers the six write commands that needed dedicated daemon endpoints (the
generic store endpoint could not host them because each carries derived-field
recompute or a record-shape transition):

  POST /v1/aspirations/recompute-all-progress  (full progress recount)
  POST /v1/aspirations/evolution-append        (meta/evolution-log.jsonl append)
  POST /v1/pipeline/recompute-meta             (pipeline-meta.json full recount)
  POST /v1/spark-questions/increment           (counter + yield_rate recompute)
  POST /v1/spark-questions/promote             (candidate -> active question)
  POST /v1/pattern-signatures/record-outcome   (outcome counter + accuracy + date)

Two layers per endpoint:
  1. HTTP round-trip (running_daemon, conftest world/meta): the route is wired
     and the flow works end-to-end, including the input-validation 400s.
  2. Byte-compat (direct handler vs the REAL CLI engine via subprocess): the
     written file is byte-identical. These commands have no per-write volatile
     fields EXCEPT date-only stamps (last_matched / last_updated =
     date.today().isoformat()), so a same-day raw byte diff holds.

CLI engines are redirected with MIND_WORLD / MIND_META (and MIND_AGENT_DIR
where the import path needs an agent dir) so they write to temp dirs, never
the real repo.
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

REPO_ROOT = Path(__file__).resolve().parents[2]
SPARK_PY = REPO_ROOT / "core" / "scripts" / "spark-questions.py"
PATSIG_PY = REPO_ROOT / "core" / "scripts" / "pattern-signatures.py"
PIPELINE_PY = REPO_ROOT / "core" / "scripts" / "pipeline.py"
ASP_PY = REPO_ROOT / "core" / "scripts" / "aspirations.py"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _post(port, path, query=None, body=None, *, agent="alpha"):
    qs = ("?" + urllib.parse.urlencode(query)) if query else ""
    url = f"http://127.0.0.1:{port}{path}{qs}"
    data = (body or "").encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


def _read_jsonl(path: Path):
    out = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


# ---------------------------------------------------------------------------
# Byte-compat scaffolding: fake ctx + CLI subprocess runner
# ---------------------------------------------------------------------------

class _FakePaths:
    def __init__(self, *, world: Path, meta: Path, agent: Path,
                 project_root: Path, agent_name="alpha"):
        self.world = world
        self.meta = meta
        self.agent = agent
        self.project_root = project_root
        self.agent_name = agent_name


class _FakeCtx:
    def __init__(self, *, world=None, meta=None, agent=None,
                 project_root=REPO_ROOT, query=None, body=b"",
                 agent_name="alpha"):
        self.paths = _FakePaths(world=world, meta=meta, agent=agent,
                                project_root=project_root, agent_name=agent_name)
        self.query = query or {}
        self.body = body
        self.headers = {"x-ayoai-agent": agent_name}


def _run_cli(script: Path, args, *, world: Path, meta: Path,
             agent_dir: Path = None, stdin_text: str = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["MIND_WORLD"] = str(world)
    env["MIND_META"] = str(meta)
    env["MIND_AGENT"] = "alpha"
    if agent_dir is not None:
        env["MIND_AGENT_DIR"] = str(agent_dir)
        agent_dir.mkdir(parents=True, exist_ok=True)
    world.mkdir(parents=True, exist_ok=True)
    meta.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, str(script), *args],
        input=stdin_text, text=True, env=env, cwd=str(REPO_ROOT),
        capture_output=True, timeout=60,
    )
    assert proc.returncode == 0, (
        f"CLI {script.name} {args} failed (rc={proc.returncode}):\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return proc


# ===========================================================================
# spark-questions increment
# ===========================================================================

_SQ_SEED = (
    '{"id":"sq-001","type":"question","status":"active","text":"What works?",'
    '"yield_rate":0.5,"times_asked":4,"sparks_generated":2}\n'
    '{"id":"sq-c01","type":"candidate","text":"candidate question text",'
    '"proposed_session":7}\n'
)


def test_spark_increment_roundtrip(running_daemon):
    project_root, port = running_daemon
    meta = project_root / "meta"
    status, body = _post(port, "/v1/spark-questions/increment",
                         {"rec_id": "sq-001", "field": "times_asked"})
    assert status == 200, body
    rec = json.loads(body)["record"]
    assert rec["times_asked"] == 5
    assert rec["yield_rate"] == round(2 / 5, 4)
    on_disk = {r["id"]: r for r in _read_jsonl(meta / "spark-questions.jsonl")}
    assert on_disk["sq-001"]["times_asked"] == 5


def test_spark_increment_invalid_field_400(running_daemon):
    _, port = running_daemon
    try:
        _post(port, "/v1/spark-questions/increment",
              {"rec_id": "sq-001", "field": "bogus"})
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "invalid_field"
    else:
        raise AssertionError("expected 400 for non-incrementable field")


def test_spark_increment_non_question_400(running_daemon):
    _, port = running_daemon
    try:
        _post(port, "/v1/spark-questions/increment",
              {"rec_id": "sq-c01", "field": "times_asked"})
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "modify_failed"
    else:
        raise AssertionError("expected 400 incrementing a candidate")


@pytest.mark.skipif(not SPARK_PY.exists(), reason="spark-questions.py missing")
def test_byte_compat_spark_increment(tmp_path):
    from mind_api.src.meta import spark_questions_write

    cli_meta = tmp_path / "cli-meta"
    dae_meta = tmp_path / "dae-meta"
    cli_meta.mkdir()
    dae_meta.mkdir()
    (cli_meta / "spark-questions.jsonl").write_text(_SQ_SEED, encoding="utf-8")
    (dae_meta / "spark-questions.jsonl").write_text(_SQ_SEED, encoding="utf-8")

    _run_cli(SPARK_PY, ["increment", "sq-001", "sparks_generated"],
             world=tmp_path / "cli-world", meta=cli_meta,
             agent_dir=tmp_path / "cli-agent")
    spark_questions_write.increment(_FakeCtx(
        meta=dae_meta, world=tmp_path / "dae-world",
        query={"rec_id": "sq-001", "field": "sparks_generated"}))

    assert ((dae_meta / "spark-questions.jsonl").read_bytes()
            == (cli_meta / "spark-questions.jsonl").read_bytes())


# ===========================================================================
# spark-questions promote
# ===========================================================================

def test_spark_promote_roundtrip(running_daemon):
    project_root, port = running_daemon
    meta = project_root / "meta"
    status, body = _post(port, "/v1/spark-questions/promote",
                         {"candidate_id": "sq-c01", "new_id": "sq-050"})
    assert status == 200, body
    rec = json.loads(body)["record"]
    assert rec["id"] == "sq-050"
    assert rec["type"] == "question"
    assert rec["status"] == "active"
    assert "proposed_session" not in rec
    on_disk = {r["id"]: r for r in _read_jsonl(meta / "spark-questions.jsonl")}
    assert "sq-050" in on_disk and "sq-c01" not in on_disk


def test_spark_promote_bad_new_id_400(running_daemon):
    _, port = running_daemon
    try:
        _post(port, "/v1/spark-questions/promote",
              {"candidate_id": "sq-c01", "new_id": "not-an-sq-id"})
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "invalid_new_id"
    else:
        raise AssertionError("expected 400 for malformed new_id")


@pytest.mark.skipif(not SPARK_PY.exists(), reason="spark-questions.py missing")
def test_byte_compat_spark_promote(tmp_path):
    from mind_api.src.meta import spark_questions_write

    cli_meta = tmp_path / "cli-meta"
    dae_meta = tmp_path / "dae-meta"
    cli_meta.mkdir()
    dae_meta.mkdir()
    (cli_meta / "spark-questions.jsonl").write_text(_SQ_SEED, encoding="utf-8")
    (dae_meta / "spark-questions.jsonl").write_text(_SQ_SEED, encoding="utf-8")

    _run_cli(SPARK_PY, ["promote", "sq-c01", "sq-077"],
             world=tmp_path / "cli-world", meta=cli_meta,
             agent_dir=tmp_path / "cli-agent")
    spark_questions_write.promote(_FakeCtx(
        meta=dae_meta, world=tmp_path / "dae-world",
        query={"candidate_id": "sq-c01", "new_id": "sq-077"}))

    assert ((dae_meta / "spark-questions.jsonl").read_bytes()
            == (cli_meta / "spark-questions.jsonl").read_bytes())


# ===========================================================================
# pattern-signatures record-outcome
# ===========================================================================

_SIG_SEED = (
    '{"id":"sig-001","name":"alpha pattern","validation_status":"validated",'
    '"status":"active","outcome_stats":{"total":5,"confirmed":4,"accuracy":0.8}}\n'
    '{"id":"sig-002","name":"beta pattern","validation_status":"unvalidated",'
    '"status":"active","outcome_stats":{"total":0,"confirmed":0,"accuracy":0.0}}\n'
)


def test_patsig_record_outcome_roundtrip(running_daemon):
    project_root, port = running_daemon
    world = project_root / "world"
    status, body = _post(port, "/v1/pattern-signatures/record-outcome",
                         {"rec_id": "sig-001", "outcome": "CONFIRMED"})
    assert status == 200, body
    rec = json.loads(body)["record"]
    assert rec["outcome_stats"]["total"] == 6
    assert rec["outcome_stats"]["confirmed"] == 5
    assert rec["last_matched"]  # date stamped
    on_disk = {r["id"]: r for r in _read_jsonl(world / "pattern-signatures.jsonl")}
    assert on_disk["sig-001"]["outcome_stats"]["total"] == 6


def test_patsig_record_outcome_invalid_400(running_daemon):
    _, port = running_daemon
    try:
        _post(port, "/v1/pattern-signatures/record-outcome",
              {"rec_id": "sig-001", "outcome": "MAYBE"})
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "invalid_outcome"
    else:
        raise AssertionError("expected 400 for invalid outcome")


@pytest.mark.skipif(not PATSIG_PY.exists(), reason="pattern-signatures.py missing")
def test_byte_compat_patsig_record_outcome(tmp_path):
    from mind_api.src.world import pattern_signatures_write

    cli_world = tmp_path / "cli-world"
    dae_world = tmp_path / "dae-world"
    cli_world.mkdir()
    dae_world.mkdir()
    (cli_world / "pattern-signatures.jsonl").write_text(_SIG_SEED, encoding="utf-8")
    (dae_world / "pattern-signatures.jsonl").write_text(_SIG_SEED, encoding="utf-8")

    _run_cli(PATSIG_PY, ["record-outcome", "sig-001", "CONFIRMED"],
             world=cli_world, meta=tmp_path / "cli-meta",
             agent_dir=tmp_path / "cli-agent")
    pattern_signatures_write.record_outcome(_FakeCtx(
        world=dae_world, meta=tmp_path / "dae-meta",
        query={"rec_id": "sig-001", "outcome": "CONFIRMED"}))

    assert ((dae_world / "pattern-signatures.jsonl").read_bytes()
            == (cli_world / "pattern-signatures.jsonl").read_bytes())


# ===========================================================================
# pipeline recompute-meta
# ===========================================================================

_PIPE_LIVE = (
    '{"id":"2026-05-01_a","stage":"resolved","outcome":"CONFIRMED",'
    '"strategy":"empirical","horizon":"short","depth":"deep"}\n'
    '{"id":"2026-05-02_b","stage":"resolved","outcome":"CORRECTED",'
    '"strategy":"empirical","horizon":"session","depth":"shallow"}\n'
    '{"id":"2026-05-03_c","stage":"active","title":"in flight"}\n'
)
_PIPE_ARCHIVE = (
    '{"id":"2026-04-01_z","stage":"archived","outcome":"CONFIRMED",'
    '"strategy":"calibration","horizon":"long"}\n'
)
# Existing meta carrying micro_hypothesis_stats — both engines must preserve it.
_PIPE_OLD_META = '{"micro_hypothesis_stats":{"resolved":3,"pending":1}}\n'


def test_pipeline_recompute_meta_roundtrip(running_daemon):
    _, port = running_daemon
    status, body = _post(port, "/v1/pipeline/recompute-meta")
    assert status == 200, body
    meta = json.loads(body)["meta"]
    assert "stage_counts" in meta and "accuracy" in meta
    assert meta["last_updated"]  # date stamped


@pytest.mark.skipif(not PIPELINE_PY.exists(), reason="pipeline.py missing")
def test_byte_compat_pipeline_recompute_meta(tmp_path):
    from mind_api.src.world import pipeline_write

    cli_world = tmp_path / "cli-world"
    dae_world = tmp_path / "dae-world"
    for w in (cli_world, dae_world):
        w.mkdir()
        (w / "pipeline.jsonl").write_text(_PIPE_LIVE, encoding="utf-8")
        (w / "pipeline-archive.jsonl").write_text(_PIPE_ARCHIVE, encoding="utf-8")
        (w / "pipeline-meta.json").write_text(_PIPE_OLD_META, encoding="utf-8")

    _run_cli(PIPELINE_PY, ["recompute-meta"],
             world=cli_world, meta=tmp_path / "cli-meta",
             agent_dir=tmp_path / "cli-agent")
    pipeline_write.recompute_meta(_FakeCtx(
        world=dae_world, meta=tmp_path / "dae-meta", query={}))

    assert ((dae_world / "pipeline-meta.json").read_bytes()
            == (cli_world / "pipeline-meta.json").read_bytes())


# ===========================================================================
# aspirations recompute-all-progress
# ===========================================================================

# Goals with a deliberately-stale progress block so the recompute changes it.
_ASP_SEED = (
    '{"id":"asp-001","title":"Test","status":"active","priority":"LOW",'
    '"archived":false,"initial_goal_count":2,'
    '"goals":['
    '{"id":"g-001-01","status":"completed","title":"done"},'
    '{"id":"g-001-02","status":"pending","title":"todo"},'
    '{"id":"g-001-03","status":"completed","recurring":true,"title":"recur"}'
    '],'
    '"progress":{"completed_goals":0,"total_goals":0,"recurring_goals":0,"fan_out_ratio":null}}\n'
)


def test_aspirations_recompute_all_progress_roundtrip(running_daemon):
    project_root, port = running_daemon
    world = project_root / "world"
    status, body = _post(port, "/v1/aspirations/recompute-all-progress",
                         {"source": "world"})
    assert status == 200, body
    assert json.loads(body)["ok"] is True
    asp = _read_jsonl(world / "aspirations.jsonl")[0]
    # conftest asp-001 has zero goals → progress stays consistent.
    assert asp["progress"]["total_goals"] == 0


@pytest.mark.skipif(not ASP_PY.exists(), reason="aspirations.py missing")
def test_byte_compat_aspirations_recompute_all_progress(tmp_path):
    from mind_api.src.endpoints import aspirations_write

    cli_world = tmp_path / "cli-world"
    dae_world = tmp_path / "dae-world"
    cli_world.mkdir()
    dae_world.mkdir()
    (cli_world / "aspirations.jsonl").write_text(_ASP_SEED, encoding="utf-8")
    (dae_world / "aspirations.jsonl").write_text(_ASP_SEED, encoding="utf-8")

    # CLI takes an explicit path arg (temp file not under any base dir → no
    # history/changelog side-effects, only the primary file is written).
    _run_cli(ASP_PY, ["recompute-all-progress", str(cli_world / "aspirations.jsonl")],
             world=tmp_path / "cli-w2", meta=tmp_path / "cli-meta",
             agent_dir=tmp_path / "cli-agent")
    aspirations_write.recompute_all_progress(_FakeCtx(
        world=dae_world, meta=tmp_path / "dae-meta",
        agent=tmp_path / "dae-agent", query={"source": "world"}))

    assert ((dae_world / "aspirations.jsonl").read_bytes()
            == (cli_world / "aspirations.jsonl").read_bytes())


# ===========================================================================
# aspirations evolution-append
# ===========================================================================

def test_evolution_append_roundtrip(running_daemon):
    project_root, port = running_daemon
    meta = project_root / "meta"
    evt = {"date": "2026-05-29", "event": "test_event", "details": "round-trip"}
    status, body = _post(port, "/v1/aspirations/evolution-append",
                         body=json.dumps(evt))
    assert status == 200, body
    records = _read_jsonl(meta / "evolution-log.jsonl")
    assert records[-1]["event"] == "test_event"


def test_evolution_append_missing_field_400(running_daemon):
    _, port = running_daemon
    try:
        _post(port, "/v1/aspirations/evolution-append",
              body=json.dumps({"event": "no_date"}))
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "validation_failed"
    else:
        raise AssertionError("expected 400 for missing required fields")


def test_evolution_append_bad_date_400(running_daemon):
    _, port = running_daemon
    try:
        _post(port, "/v1/aspirations/evolution-append",
              body=json.dumps({"date": "05/29/2026", "event": "x", "details": "y"}))
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "validation_failed"
    else:
        raise AssertionError("expected 400 for bad date format")


@pytest.mark.skipif(not ASP_PY.exists(), reason="aspirations.py missing")
def test_byte_compat_evolution_append(tmp_path):
    from mind_api.src.endpoints import aspirations_write

    cli_meta = tmp_path / "cli-meta"
    dae_meta = tmp_path / "dae-meta"
    cli_meta.mkdir()
    dae_meta.mkdir()
    evt = {"date": "2026-05-29", "event": "stage_advance",
           "details": "promoted to stage 3", "extra": {"k": "v", "n": 1}}
    evt_json = json.dumps(evt)

    _run_cli(ASP_PY, ["evolution-append"],
             world=tmp_path / "cli-world", meta=cli_meta,
             agent_dir=tmp_path / "cli-agent", stdin_text=evt_json)
    aspirations_write.evolution_append(_FakeCtx(
        meta=dae_meta, world=tmp_path / "dae-world",
        agent=tmp_path / "dae-agent", body=evt_json.encode("utf-8")))

    assert ((dae_meta / "evolution-log.jsonl").read_bytes()
            == (cli_meta / "evolution-log.jsonl").read_bytes())
