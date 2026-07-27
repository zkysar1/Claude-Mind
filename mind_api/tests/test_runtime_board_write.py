"""POST /v1/board/post, /v1/board/mark-read, GET /v1/board/channels.

Two layers:
  1. HTTP round-trip (running_daemon, conftest world): endpoints wired,
     post / mark-read / channels work end-to-end through the real server,
     including the agent-header gate and the msg-id format.
  2. Byte-compat (direct handler vs the REAL CLI board.py): the on-disk
     channel / reads-sidecar JSONL lines match the CLI's. Board records
     carry datetime.now() in `id` + `timestamp` (+ `read_at` for reads), so
     the comparison is STRUCTURAL: identical key order, identical non-volatile
     fields, and a re-dump check proving the daemon used
     json.dumps(ensure_ascii=True) default separators (== _fileops).

CLI subprocess uses MIND_WORLD/MIND_META env (board is world-level, so the
override fully sandboxes it) + sys.executable (bypasses the Windows python3
stub) + cwd=REPO_ROOT.
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
BOARD_PY = REPO_ROOT / "core" / "scripts" / "board.py"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _post_raw(port, path, query, body_text, *, agent="alpha", sid=None):
    qs = ("?" + urllib.parse.urlencode(query)) if query else ""
    url = f"http://127.0.0.1:{port}{path}{qs}"
    data = (body_text or "").encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "text/plain")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    if sid:
        req.add_header("X-Mind-Sid", sid)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


def _get(port, path, query=None, *, agent="alpha"):
    qs = ("?" + urllib.parse.urlencode(query)) if query else ""
    url = f"http://127.0.0.1:{port}{path}{qs}"
    req = urllib.request.Request(url, method="GET")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


def _channel_lines(world: Path, channel: str):
    p = world / "board" / f"{channel}.jsonl"
    if not p.exists():
        return []
    return [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ---------------------------------------------------------------------------
# HTTP round-trip tests (conftest world)
# ---------------------------------------------------------------------------

def test_post_roundtrip(running_daemon):
    project_root, port = running_daemon
    world = project_root / "world"
    status, body = _post_raw(port, "/v1/board/post",
                             {"channel": "general"}, "hello from the test")
    assert status == 200, body
    data = json.loads(body)
    assert data["ok"]
    assert data["id"].startswith("msg-") and data["id"].endswith("-alpha-003")  # 2 seeded
    lines = _channel_lines(world, "general")
    assert len(lines) == 3
    last = json.loads(lines[-1])
    assert last["text"] == "hello from the test"
    assert last["author"] == "alpha"
    assert last["channel"] == "general"
    assert last["type"] == "status"
    assert list(last.keys()) == ["id", "author", "session_id", "timestamp",
                                 "channel", "type", "text", "reply_to", "tags"]


def test_post_requires_agent_header(running_daemon):
    _, port = running_daemon
    try:
        _post_raw(port, "/v1/board/post", {"channel": "general"}, "x", agent=None)
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "missing_agent_header"
    else:
        raise AssertionError("expected 400 without X-Mind-Agent")


def test_post_empty_text_400(running_daemon):
    _, port = running_daemon
    try:
        _post_raw(port, "/v1/board/post", {"channel": "general"}, "   ")
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "empty_text"
    else:
        raise AssertionError("expected 400 for empty text")


def test_post_missing_channel_400(running_daemon):
    _, port = running_daemon
    try:
        _post_raw(port, "/v1/board/post", {}, "hi")
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "missing_param"
    else:
        raise AssertionError("expected 400 without channel")


def test_post_tags_and_reply(running_daemon):
    project_root, port = running_daemon
    world = project_root / "world"
    status, body = _post_raw(port, "/v1/board/post",
                             {"channel": "general", "type": "claim",
                              "tags": "g-1-1, urgent", "reply_to": "msg-1"},
                             "claiming the goal", sid="sid-xyz")
    assert status == 200, body
    last = json.loads(_channel_lines(world, "general")[-1])
    assert last["tags"] == ["g-1-1", "urgent"]
    assert last["reply_to"] == "msg-1"
    assert last["type"] == "claim"
    assert last["session_id"] == "sid-xyz"


def test_post_coordination_succeeds(running_daemon):
    # The coordination wake-signal side-effect is fail-open; assert the post
    # itself lands (peers may or may not exist).
    project_root, port = running_daemon
    world = project_root / "world"
    status, body = _post_raw(port, "/v1/board/post",
                             {"channel": "coordination"}, "claim g-1-1")
    assert status == 200, body
    assert len(_channel_lines(world, "coordination")) == 1


def test_mark_read_roundtrip(running_daemon):
    project_root, port = running_daemon
    world = project_root / "world"
    status, body = _post_raw(port, "/v1/board/mark-read",
                             {"channel": "general", "ids": "msg-1,msg-2"}, "")
    assert status == 200, body
    data = json.loads(body)
    assert data["marked"] == 2 and data["already_read"] == 0
    # Idempotent re-mark by the same reader -> already_read.
    _, body2 = _post_raw(port, "/v1/board/mark-read",
                         {"channel": "general", "ids": "msg-1,msg-2"}, "")
    assert json.loads(body2)["already_read"] == 2
    sidecar = world / "board" / "general-reads.jsonl"
    rows = [json.loads(ln) for ln in sidecar.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert {r["msg_id"] for r in rows} == {"msg-1", "msg-2"}
    assert all(r["reader_agent"] == "alpha" for r in rows)


def test_mark_read_ids_via_body(running_daemon):
    project_root, port = running_daemon
    world = project_root / "world"
    status, body = _post_raw(port, "/v1/board/mark-read",
                             {"channel": "general"}, "msg-1\nmsg-2\n")
    assert status == 200, body
    assert json.loads(body)["marked"] == 2


def test_channels_lists(running_daemon):
    _, port = running_daemon
    status, body = _get(port, "/v1/board/channels")
    assert status == 200, body
    data = json.loads(body)
    names = {c["name"]: c for c in data["channels"]}
    assert "general" in names
    assert names["general"]["count"] == 2  # conftest seeds 2 messages
    assert names["general"]["last_timestamp"] == "2026-05-12T10:01:00"


def test_post_findings_citation_increments(running_daemon):
    """ attribution end-to-end through the daemon, including the
    g-115-2351 regex regression: a 4-digit ID (rb-3742) must attribute —
    the pre-fix _CITE_RE \\d{3} silently excluded every ID past 999.
    Non-findings channels must not attribute (channel gate)."""
    project_root, port = running_daemon
    world = project_root / "world"

    # Seed one 3-digit guard and one 4-digit rb through the canonical
    # store append (validated, cache-coherent).
    guard_rec = {"id": "guard-901", "rule": "test citation target",
                 "category": "test-cite", "trigger_condition": "never",
                 "source": "citation-test", "when_to_use": "never",
                 "tags": ["cite-test"]}
    rb_rec = {"id": "rb-3742", "title": "test citation target",
              "type": "success", "category": "test-cite",
              "content": "citation regression fixture",
              "applies_to": "framework", "tags": ["cite-test"]}
    status, _ = _post_raw(port, "/v1/store/append", {"store": "guardrails"},
                          json.dumps(guard_rec))
    assert status == 200
    status, _ = _post_raw(port, "/v1/store/append", {"store": "reasoning-bank"},
                          json.dumps(rb_rec))
    assert status == 200

    def _tih(fname, rec_id):
        recs = [json.loads(ln) for ln in
                (world / fname).read_text(encoding="utf-8").splitlines()
                if ln.strip()]
        rec = next(r for r in recs if r.get("id") == rec_id)
        return rec["utilization"]["times_inferred_helpful"], \
            rec["utilization"]["utilization_score"]

    # Findings post citing both — each gets exactly one increment.
    status, _ = _post_raw(
        port, "/v1/board/post",
        {"channel": "findings",
         "tags": "fresh-eyes-code,guard-901,rb-3742,severity:constrains"},
        "citation attribution end-to-end")
    assert status == 200
    g_tih, g_score = _tih("guardrails.jsonl", "guard-901")
    r_tih, r_score = _tih("reasoning-bank.jsonl", "rb-3742")
    assert g_tih == 1, f"guard-901 tih expected 1, got {g_tih}"
    assert r_tih == 1, f"rb-3742 (4-digit) tih expected 1, got {r_tih} " \
                       "— \\d{3}-only regex regression"
    # Smoothed  score: (th + 0.5*tih)/(max(rc, th+tih)+1)
    # = (0 + 0.5)/(max(0, 1)+1) = 0.25 on a fresh record.
    assert g_score == 0.25 and r_score == 0.25, (g_score, r_score)

    # Non-findings channel with the same tags: counters unchanged.
    status, _ = _post_raw(port, "/v1/board/post",
                          {"channel": "general", "tags": "guard-901,rb-3742"},
                          "general post must not attribute")
    assert status == 200
    assert _tih("guardrails.jsonl", "guard-901")[0] == 1
    assert _tih("reasoning-bank.jsonl", "rb-3742")[0] == 1


# ---------------------------------------------------------------------------
# Byte-compat: daemon handler output == real CLI output
# ---------------------------------------------------------------------------

class _FakePaths:
    def __init__(self, world: Path):
        self.world = world
        self.agent_name = "alpha"


class _FakeCtx:
    def __init__(self, world: Path, query: dict, body: bytes,
                 *, agent="alpha", sid=""):
        self.paths = _FakePaths(world)
        self.query = query
        self.body = body
        headers = {"x-mind-agent": agent}
        if sid:
            headers["x-mind-sid"] = sid
        self.headers = headers


def _seed_board_world(base: Path, name: str) -> Path:
    world = base / name
    (world / "board").mkdir(parents=True)
    return world


def _run_board_cli(world: Path, meta: Path, args, stdin_text):
    env = dict(os.environ)
    env["MIND_WORLD"] = str(world)
    env["MIND_META"] = str(meta)
    env["MIND_AGENT"] = "alpha"
    env.pop("MIND_SID", None)
    meta.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, str(BOARD_PY), *args],
        input=stdin_text, text=True, env=env, cwd=str(REPO_ROOT),
        capture_output=True, timeout=60,
    )
    assert proc.returncode == 0, (
        f"CLI board.py failed (rc={proc.returncode}):\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return proc


def _assert_line_compat(daemon_line: str, cli_line: str, volatile):
    d = json.loads(daemon_line)
    c = json.loads(cli_line)
    # 1. Key order identical — the byte-compat property for json.dumps.
    assert list(d.keys()) == list(c.keys()), \
        f"key order: {list(d.keys())} vs {list(c.keys())}"
    # 2. Daemon used json.dumps(ensure_ascii=True) default separators.
    assert daemon_line == json.dumps(d, ensure_ascii=True), \
        "daemon serialization params differ from _fileops (ensure_ascii=True)"
    assert cli_line == json.dumps(c, ensure_ascii=True)
    # 3. Non-volatile fields equal; volatile fields same type.
    for k in d:
        if k in volatile:
            assert type(d[k]) is type(c[k]), f"{k}: type {type(d[k])} vs {type(c[k])}"
        else:
            assert d[k] == c[k], f"field {k}: {d[k]!r} vs {c[k]!r}"


@pytest.mark.skipif(not BOARD_PY.exists(), reason="core/scripts/board.py missing")
def test_byte_compat_post(tmp_path):
    from mind_api.src.endpoints import board_write

    cli_world = _seed_board_world(tmp_path, "cli")
    dae_world = _seed_board_world(tmp_path, "dae")

    text = "byte-compat message with unicode café ✓"
    _run_board_cli(cli_world, tmp_path / "cli-meta",
                   ["post", "--channel", "general", "--author", "alpha"], text)
    board_write.post(_FakeCtx(dae_world, {"channel": "general", "author": "alpha"},
                              text.encode("utf-8")))

    cli_lines = _channel_lines(cli_world, "general")
    dae_lines = _channel_lines(dae_world, "general")
    assert len(cli_lines) == len(dae_lines) == 1
    _assert_line_compat(dae_lines[0], cli_lines[0], volatile={"id", "timestamp"})


@pytest.mark.skipif(not BOARD_PY.exists(), reason="core/scripts/board.py missing")
def test_byte_compat_mark_read(tmp_path):
    from mind_api.src.endpoints import board_write

    cli_world = _seed_board_world(tmp_path, "cli")
    dae_world = _seed_board_world(tmp_path, "dae")

    _run_board_cli(cli_world, tmp_path / "cli-meta",
                   ["mark-read", "--channel", "general", "--ids", "msg-1,msg-2"], None)
    board_write.mark_read(_FakeCtx(dae_world,
                                   {"channel": "general", "ids": "msg-1,msg-2"}, b""))

    def _sidecar_lines(world):
        p = world / "board" / "general-reads.jsonl"
        return [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]

    cli_rows = _sidecar_lines(cli_world)
    dae_rows = _sidecar_lines(dae_world)
    assert len(cli_rows) == len(dae_rows) == 2
    for d, c in zip(dae_rows, cli_rows):
        _assert_line_compat(d, c, volatile={"read_at"})
