"""POST /v1/experience/{add,update-field,archive-goal}.

Two layers:
  1. HTTP round-trip (running_daemon, conftest world): endpoints wired,
     add / update-field / archive-goal work end-to-end, incl. the agent-header
     gate, content_path-on-disk validation, dup-check, and the
     experience-meta.json sidecar recompute.
  2. Byte-compat (direct handler vs the REAL CLI experience.py): the
     experience.jsonl line matches structurally (volatile `created` excluded)
     AND experience-meta.json is byte-identical (deterministic same-day).

The CLI subprocess is redirected with MIND_AGENT_DIR (the documented unit-test
override in core/scripts/_paths.py:222-228) so it writes to a temp agent dir,
never the real agents/alpha/. content_path is supplied ABSOLUTE so neither side
resolves it against PROJECT_ROOT (the CLI's PROJECT_ROOT is script-derived and
not overridable — an absolute path keeps both sides off the real repo).
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
EXPERIENCE_PY = REPO_ROOT / "core" / "scripts" / "experience.py"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _post_json(port, path, query, body, *, agent="alpha"):
    qs = ("?" + urllib.parse.urlencode(query)) if query else ""
    url = f"http://127.0.0.1:{port}{path}{qs}"
    data = json.dumps(body).encode("utf-8") if body is not None else b""
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


def _live_records(agent_dir: Path):
    p = agent_dir / "experience.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _make_trace(project_root: Path, rel: str, text="trace body content that is reasonably long\n") -> str:
    """Create a trace .md under project_root and return the relative posix path."""
    p = project_root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return rel.replace("\\", "/")


# ---------------------------------------------------------------------------
# HTTP round-trip tests (conftest world)
# ---------------------------------------------------------------------------

def test_add_roundtrip(running_daemon):
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    cp = _make_trace(project_root, "agents/alpha/experience/rt-add.md")
    rec = {"id": "exp-rt-add", "type": "research", "category": "mycat",
           "summary": "a sufficiently long summary line", "content_path": cp}
    status, body = _post_json(port, "/v1/experience/add", {}, rec)
    assert status == 200, body
    data = json.loads(body)
    assert data["ok"] and data["record"]["id"] == "exp-rt-add"
    assert "created" in data["record"]
    # Appended on disk.
    ids = [r["id"] for r in _live_records(agent_dir)]
    assert "exp-rt-add" in ids
    # Meta recomputed.
    meta = json.loads((agent_dir / "experience-meta.json").read_text(encoding="utf-8"))
    assert "total_live" in meta and meta["total_live"] == len(_live_records(agent_dir))
    assert meta["by_type"].get("research", 0) >= 1


def test_add_requires_agent_header(running_daemon):
    project_root, port = running_daemon
    cp = _make_trace(project_root, "agents/alpha/experience/noh.md")
    rec = {"id": "exp-noh", "type": "research", "category": "c",
           "summary": "summary long enough here", "content_path": cp}
    try:
        _post_json(port, "/v1/experience/add", {}, rec, agent=None)
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "missing_agent_header"
    else:
        raise AssertionError("expected 400 without X-Mind-Agent")


def test_add_missing_content_path_400(running_daemon):
    _, port = running_daemon
    rec = {"id": "exp-nofile", "type": "research", "category": "c",
           "summary": "summary long enough here",
           "content_path": "agents/alpha/experience/does-not-exist.md"}
    try:
        _post_json(port, "/v1/experience/add", {}, rec)
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "validation_failed"
    else:
        raise AssertionError("expected 400 for missing content_path file")


def test_add_rejects_temp_content_path(running_daemon):
    """ / guard-1373 (Layer-B): a content_path under a temp/ segment
    is rejected at write time even when the file EXISTS (temp/ is drained, so
    the body would orphan). The record must NOT land on disk."""
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    # Create the temp body so the reject fires on the temp segment, not on the
    # existence check (proves the guard is independent of file existence).
    cp = _make_trace(project_root, "agents/alpha/temp/orphan-prone.md")
    rec = {"id": "exp-temp-reject", "type": "research", "category": "c",
           "summary": "summary long enough here", "content_path": cp}
    try:
        _post_json(port, "/v1/experience/add", {}, rec)
    except urllib.error.HTTPError as e:
        assert e.code == 400
        payload = json.loads(e.read())
        assert payload["error"] == "validation_failed"
        assert "temp/" in payload["detail"]
    else:
        raise AssertionError("expected 400 rejecting a temp/ content_path")
    # Never written.
    assert "exp-temp-reject" not in [r["id"] for r in _live_records(agent_dir)]


def test_add_allows_template_segment_not_substring(running_daemon):
    """The guard matches a temp/ path SEGMENT (PurePosixPath.parts), not the
    substring 'temp' — so a legitimate 'template/' directory is unaffected."""
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    cp = _make_trace(project_root, "agents/alpha/experience/template/keep.md")
    rec = {"id": "exp-template-ok", "type": "research", "category": "c",
           "summary": "summary long enough here", "content_path": cp}
    status, body = _post_json(port, "/v1/experience/add", {}, rec)
    assert status == 200, body
    assert "exp-template-ok" in [r["id"] for r in _live_records(agent_dir)]


def test_add_duplicate_409(running_daemon):
    project_root, port = running_daemon
    cp = _make_trace(project_root, "agents/alpha/experience/dup.md")
    rec = {"id": "exp-dup", "type": "research", "category": "c",
           "summary": "summary long enough here", "content_path": cp}
    status, _ = _post_json(port, "/v1/experience/add", {}, rec)
    assert status == 200
    try:
        _post_json(port, "/v1/experience/add", {}, rec)
    except urllib.error.HTTPError as e:
        assert e.code == 409
        assert json.loads(e.read())["error"] == "duplicate_id"
    else:
        raise AssertionError("expected 409 for duplicate id")


def test_update_field_roundtrip(running_daemon):
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    cp = _make_trace(project_root, "agents/alpha/experience/upd.md")
    rec = {"id": "exp-upd", "type": "research", "category": "c",
           "summary": "original summary text here", "content_path": cp}
    _post_json(port, "/v1/experience/add", {}, rec)
    status, body = _post_json(port, "/v1/experience/update-field",
                              {"id": "exp-upd", "field": "summary",
                               "value": "revised summary text"}, None)
    assert status == 200, body
    assert json.loads(body)["record"]["summary"] == "revised summary text"
    updated = next(r for r in _live_records(agent_dir) if r["id"] == "exp-upd")
    assert updated["summary"] == "revised summary text"


def test_update_field_whole_object_recomputes_utility_ratio(running_daemon):
    """ -- the LIVE path, end to end over HTTP.

    experience-update-field.sh is daemon-only, so THIS handler is what runs.
    The recompute used to be guarded on `field.startswith("retrieval_stats.")`
    while the dotted rejection at the top of the handler returned 400 before the
    cycle was entered -- so it never fired, and utility_ratio read 0.0 on 4,174
    of 4,175 fleet records. The whole-object write is the only shape that
    reaches it, and the shape every caller uses.
    """
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    cp = _make_trace(project_root, "agents/alpha/experience/ur.md")
    rec = {"id": "exp-ur-daemon", "type": "research", "category": "c",
           "summary": "summary text long enough here", "content_path": cp}
    _post_json(port, "/v1/experience/add", {}, rec)

    blob = json.dumps({"retrieval_count": 4, "times_useful": 2, "times_noise": 0,
                       "utility_ratio": 0.0, "last_retrieved": None})
    status, body = _post_json(port, "/v1/experience/update-field",
                              {"id": "exp-ur-daemon", "field": "retrieval_stats",
                               "value": blob}, None)
    assert status == 200, body
    assert json.loads(body)["record"]["retrieval_stats"]["utility_ratio"] == 0.5, body
    updated = next(r for r in _live_records(agent_dir) if r["id"] == "exp-ur-daemon")
    assert updated["retrieval_stats"]["utility_ratio"] == 0.5, updated

    # A payload omitting a strict-lookup sub-key must backfill, not 500: the
    # whole-object write replaces the dict _normalize_record already filled.
    status, body = _post_json(port, "/v1/experience/update-field",
                              {"id": "exp-ur-daemon", "field": "retrieval_stats",
                               "value": json.dumps({"retrieval_count": 3})}, None)
    assert status == 200, body
    stats = json.loads(body)["record"]["retrieval_stats"]
    assert stats["times_useful"] == 0 and stats["utility_ratio"] == 0.0, stats


def test_update_field_rejects_created(running_daemon):
    project_root, port = running_daemon
    cp = _make_trace(project_root, "agents/alpha/experience/crt.md")
    rec = {"id": "exp-crt", "type": "research", "category": "c",
           "summary": "summary text long enough", "content_path": cp}
    _post_json(port, "/v1/experience/add", {}, rec)
    try:
        _post_json(port, "/v1/experience/update-field",
                   {"id": "exp-crt", "field": "created", "value": "2020-01-01T00:00:00"}, None)
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "immutable_field"
    else:
        raise AssertionError("expected 400 rejecting created update")


def test_archive_goal_roundtrip(running_daemon):
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    # Pre-write a trace file at a NON-canonical path; archive-goal moves it.
    src = project_root / "agents" / "alpha" / "session" / "trace-src.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("a real reasoning trace with enough body to pass the gate\n" * 5,
                   encoding="utf-8")
    body = {"goal": "g-9-9", "skill_slug": "aspirations-execute",
            "category": "framework", "summary": "archived the goal execution trace",
            "trace_file": "agents/alpha/session/trace-src.md"}
    status, resp = _post_json(port, "/v1/experience/archive-goal", {}, body)
    assert status == 200, resp
    rec = json.loads(resp)["record"]
    assert rec["id"] == "exp-g-9-9-aspirations-execute"
    assert rec["content_path"].endswith("agents/alpha/experience/exp-g-9-9-aspirations-execute.md")
    # Trace moved to canonical.
    canonical = agent_dir / "experience" / "exp-g-9-9-aspirations-execute.md"
    assert canonical.exists()
    assert not src.exists()
    ids = [r["id"] for r in _live_records(agent_dir)]
    assert "exp-g-9-9-aspirations-execute" in ids


def test_meta_update_roundtrip(running_daemon):
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    status, body = _post_json(port, "/v1/experience/meta-update",
                              {"field": "custom_note", "value": "hello"}, None)
    assert status == 200, body
    meta = json.loads((agent_dir / "experience-meta.json").read_text(encoding="utf-8"))
    assert meta["custom_note"] == "hello"
    assert meta.get("last_updated")


def test_meta_update_rejects_dotted(running_daemon):
    _, port = running_daemon
    try:
        _post_json(port, "/v1/experience/meta-update",
                   {"field": "a.b", "value": "x"}, None)
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "dotted_field_rejected"
    else:
        raise AssertionError("expected 400 for a dotted meta field")


def test_meta_update_requires_agent_header(running_daemon):
    _, port = running_daemon
    try:
        _post_json(port, "/v1/experience/meta-update",
                   {"field": "f", "value": "v"}, None, agent=None)
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "missing_agent_header"
    else:
        raise AssertionError("expected 400 without X-Mind-Agent")


def test_archive_sweep_roundtrip(running_daemon):
    """Conftest experience records are recent (created within ~3 weeks) so none
    are stale — the sweep is a no-op, confirming wiring + the agent gate.
    Actual archiving is covered by the byte-compat test (seeded stale data)."""
    _, port = running_daemon
    status, body = _post_json(port, "/v1/experience/archive-sweep", {}, None)
    assert status == 200, body
    assert json.loads(body)["archived"] == 0


def test_archive_sweep_requires_agent_header(running_daemon):
    _, port = running_daemon
    try:
        _post_json(port, "/v1/experience/archive-sweep", {}, None, agent=None)
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "missing_agent_header"
    else:
        raise AssertionError("expected 400 without X-Mind-Agent")


def test_recompute_index_roundtrip(running_daemon):
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    status, body = _post_json(port, "/v1/experience/recompute-index", {}, None)
    assert status == 200, body
    data = json.loads(body)
    assert data["ok"] and "index" in data
    # The index file landed on disk.
    assert (agent_dir / "experiential-index.yaml").exists()


# ---------------------------------------------------------------------------
# Byte-compat: daemon handler output == real CLI output
# ---------------------------------------------------------------------------

class _FakePaths:
    def __init__(self, agent: Path, project_root: Path, world: Path = None):
        self.agent = agent
        self.project_root = project_root
        # world drives recompute-index (reads the collective pipeline). Other
        # ops never touch it, so it stays optional.
        self.world = world
        self.agent_name = "alpha"


class _FakeCtx:
    def __init__(self, agent: Path, project_root: Path, query: dict, body: bytes,
                 *, agent_name="alpha", world: Path = None):
        self.paths = _FakePaths(agent, project_root, world)
        self.query = query
        self.body = body
        self.headers = {"x-mind-agent": agent_name}


def _run_exp_cli(world, meta, agent_dir, args, stdin_text):
    env = dict(os.environ)
    env["MIND_WORLD"] = str(world)
    env["MIND_META"] = str(meta)
    env["MIND_AGENT"] = "alpha"
    env["MIND_AGENT_DIR"] = str(agent_dir)
    world.mkdir(parents=True, exist_ok=True)
    meta.mkdir(parents=True, exist_ok=True)
    agent_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, str(EXPERIENCE_PY), *args],
        input=stdin_text, text=True, env=env, cwd=str(REPO_ROOT),
        capture_output=True, timeout=60,
    )
    assert proc.returncode == 0, (
        f"CLI experience.py failed (rc={proc.returncode}):\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return proc


def _assert_line_compat(daemon_line, cli_line, volatile):
    d = json.loads(daemon_line)
    c = json.loads(cli_line)
    assert list(d.keys()) == list(c.keys()), \
        f"key order: {list(d.keys())} vs {list(c.keys())}"
    assert daemon_line == json.dumps(d, ensure_ascii=True), \
        "daemon serialization params differ from _fileops (ensure_ascii=True)"
    assert cli_line == json.dumps(c, ensure_ascii=True)
    for k in d:
        if k in volatile:
            assert type(d[k]) is type(c[k])
        else:
            assert d[k] == c[k], f"field {k}: {d[k]!r} vs {c[k]!r}"


@pytest.mark.skipif(not EXPERIENCE_PY.exists(), reason="core/scripts/experience.py missing")
def test_byte_compat_add(tmp_path):
    from mind_api.src.endpoints import experience_write

    # Absolute content_path to a real tmp md (keeps both sides off the real repo).
    trace = tmp_path / "trace.md"
    trace.write_text("trace body\n", encoding="utf-8")
    cp = trace.as_posix()

    rec = {"id": "exp-bc-add", "type": "research", "category": "mycat",
           "summary": "byte-compat add summary line", "content_path": cp}

    cli_agent = tmp_path / "cli-agent"
    dae_agent = tmp_path / "dae-agent"

    _run_exp_cli(tmp_path / "cli-world", tmp_path / "cli-meta", cli_agent,
                 ["add"], json.dumps(rec))
    experience_write.add(_FakeCtx(dae_agent, tmp_path, {},
                                  json.dumps(rec).encode("utf-8")))

    cli_lines = [ln for ln in (cli_agent / "experience.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
    dae_lines = [ln for ln in (dae_agent / "experience.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(cli_lines) == len(dae_lines) == 1
    _assert_line_compat(dae_lines[0], cli_lines[0], volatile={"created"})

    # experience-meta.json is fully deterministic same-day -> raw byte diff.
    cli_meta = (cli_agent / "experience-meta.json").read_bytes()
    dae_meta = (dae_agent / "experience-meta.json").read_bytes()
    assert dae_meta == cli_meta


def _seed_jsonl(path: Path, records: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


@pytest.mark.skipif(not EXPERIENCE_PY.exists(), reason="core/scripts/experience.py missing")
def test_byte_compat_meta_update(tmp_path):
    """meta-update on a fresh (absent) experience-meta.json: both sides start
    from the empty_meta skeleton, set the flat field, stamp last_updated=today
    -> byte-identical same-day."""
    from mind_api.src.endpoints import experience_write

    cli_agent = tmp_path / "cli-agent"
    dae_agent = tmp_path / "dae-agent"

    _run_exp_cli(tmp_path / "cli-world", tmp_path / "cli-meta", cli_agent,
                 ["meta-update", "custom_field", "42"], None)
    experience_write.meta_update(_FakeCtx(
        dae_agent, tmp_path, {"field": "custom_field", "value": "42"}, b""))

    cli = (cli_agent / "experience-meta.json").read_bytes()
    dae = (dae_agent / "experience-meta.json").read_bytes()
    assert dae == cli


@pytest.mark.skipif(not EXPERIENCE_PY.exists(), reason="core/scripts/experience.py missing")
def test_byte_compat_archive_sweep(tmp_path):
    """A stale record (2020, retrieval 0) is archived; a protected record
    (retrieval 10, utility 0.9) stays. experience.jsonl, experience-archive.jsonl,
    and experience-meta.json all byte-match the CLI. Records carry archived/
    archived_date keys so the in-place archived-flag set doesn't reorder."""
    from mind_api.src.endpoints import experience_write

    stats0 = {"retrieval_count": 0, "times_useful": 0, "times_noise": 0,
              "utility_ratio": 0.0, "last_retrieved": None}
    stats_hi = {"retrieval_count": 10, "times_useful": 9, "times_noise": 0,
                "utility_ratio": 0.9, "last_retrieved": None}
    stale = {"id": "exp-stale-1", "type": "research", "category": "c",
             "summary": "old record", "content_path": "x.md",
             "created": "2020-01-01T00:00:00", "retrieval_stats": stats0,
             "archived": False, "archived_date": None}
    protected = {"id": "exp-protected", "type": "research", "category": "c",
                 "summary": "keep me", "content_path": "y.md",
                 "created": "2020-01-01T00:00:00", "retrieval_stats": stats_hi,
                 "archived": False, "archived_date": None}

    cli_agent = tmp_path / "cli-agent"
    dae_agent = tmp_path / "dae-agent"
    _seed_jsonl(cli_agent / "experience.jsonl", [stale, protected])
    _seed_jsonl(dae_agent / "experience.jsonl", [stale, protected])

    _run_exp_cli(tmp_path / "cli-world", tmp_path / "cli-meta", cli_agent,
                 ["archive-sweep"], None)
    experience_write.archive_sweep(_FakeCtx(dae_agent, tmp_path, {}, b""))

    for fname in ("experience.jsonl", "experience-archive.jsonl",
                  "experience-meta.json"):
        cli = (cli_agent / fname).read_bytes()
        dae = (dae_agent / fname).read_bytes()
        assert dae == cli, f"{fname} bytes differ"


@pytest.mark.skipif(not EXPERIENCE_PY.exists(), reason="core/scripts/experience.py missing")
def test_byte_compat_recompute_index(tmp_path):
    """recompute-index reads the collective pipeline (world) and writes the
    hand-formatted experiential-index.yaml (agent). Both sides read identical
    pipeline data and produce a byte-identical YAML (sorted categories,
    last_updated=today)."""
    from mind_api.src.endpoints import experience_write

    pipeline = [
        {"id": "2026-01-01_a", "outcome": "CONFIRMED", "category": "alpha"},
        {"id": "2026-01-01_b", "outcome": "CORRECTED", "category": "alpha"},
        {"id": "2026-01-01_c", "outcome": "CONFIRMED", "category": "beta"},
        {"id": "2026-01-01_d", "outcome": "discovered", "category": "gamma"},
    ]
    cli_world = tmp_path / "cli-world"
    dae_world = tmp_path / "dae-world"
    _seed_jsonl(cli_world / "pipeline.jsonl", pipeline)
    _seed_jsonl(dae_world / "pipeline.jsonl", pipeline)

    cli_agent = tmp_path / "cli-agent"
    dae_agent = tmp_path / "dae-agent"

    _run_exp_cli(cli_world, tmp_path / "cli-meta", cli_agent,
                 ["recompute-index"], None)
    experience_write.recompute_index(_FakeCtx(
        dae_agent, tmp_path, {}, b"", world=dae_world))

    cli = (cli_agent / "experiential-index.yaml").read_bytes()
    dae = (dae_agent / "experiential-index.yaml").read_bytes()
    assert dae == cli


# ---------------------------------------------------------------------------
#  /  regression tests (experience-pipeline triple defect)
# ---------------------------------------------------------------------------

def test_archive_goal_recurring_rerun_uniquifies(running_daemon):
    """: re-archiving the same goal+skill_slug must PERSIST a fresh
    record with an auto-suffixed id (-YYYYMMDD, then -2), never 409 and lose
    the trace (the pre-fix behavior orphaned the .md on every recurring
    re-run)."""
    import re as _re
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    base = "exp-g-8-8-review-hypotheses"

    ids = []
    for i in range(3):
        src = project_root / "agents" / "alpha" / "session" / f"rerun-{i}.md"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("recurring re-run trace body with enough content\n" * 5,
                       encoding="utf-8")
        body = {"goal": "g-8-8", "skill_slug": "review-hypotheses",
                "category": "framework",
                "summary": "recurring re-run trace archived cleanly",
                "trace_file": f"agents/alpha/session/rerun-{i}.md"}
        status, resp = _post_json(port, "/v1/experience/archive-goal", {}, body)
        assert status == 200, f"call {i} refused: {resp}"
        rec = json.loads(resp)["record"]
        ids.append(rec["id"])
        canonical = agent_dir / "experience" / f"{rec['id']}.md"
        assert canonical.exists(), f"call {i}: canonical .md missing"

    assert len(set(ids)) == 3, f"ids not unique: {ids}"
    assert ids[0] == base
    assert _re.match(rf"^{_re.escape(base)}-\d{{8}}$", ids[1]), ids[1]
    assert _re.match(rf"^{_re.escape(base)}-\d{{8}}-2$", ids[2]), ids[2]
    live_ids = [r["id"] for r in _live_records(agent_dir)]
    for rid in ids:
        assert rid in live_ids, f"{rid} not persisted to experience.jsonl"


def test_add_derives_goal_id_from_id(running_daemon):
    """: add without goal_id derives it from the canonical
    exp-{goal-id}-{suffix} id shape so --goal queries can find the record;
    non-goal-shaped ids stay null (conservative)."""
    project_root, port = running_daemon

    cp1 = _make_trace(project_root, "agents/alpha/experience/derive-1.md")
    rec1 = {"id": "exp-g-7-7-s99", "type": "research", "category": "c",
            "summary": "derive goal id from record id", "content_path": cp1}
    status, resp = _post_json(port, "/v1/experience/add", {}, rec1)
    assert status == 200, resp
    assert json.loads(resp)["record"]["goal_id"] == "g-7-7"

    cp2 = _make_trace(project_root, "agents/alpha/experience/derive-2.md")
    rec2 = {"id": "exp-nogoal-shape", "type": "research", "category": "c",
            "summary": "non goal shaped id stays null", "content_path": cp2}
    status, resp = _post_json(port, "/v1/experience/add", {}, rec2)
    assert status == 200, resp
    assert json.loads(resp)["record"]["goal_id"] is None

    cp3 = _make_trace(project_root, "agents/alpha/experience/derive-3.md")
    rec3 = {"id": "exp-g-6-6-explicit", "type": "research", "category": "c",
            "summary": "explicit goal_id is never overwritten",
            "content_path": cp3, "goal_id": "g-999-999"}
    status, resp = _post_json(port, "/v1/experience/add", {}, rec3)
    assert status == 200, resp
    assert json.loads(resp)["record"]["goal_id"] == "g-999-999"
