"""GET /v1/changelog/{read,stats}.

Two layers:
  1. HTTP round-trip (running_daemon): endpoints wired, empty-world behaviour,
     post-seed reads, no agent-header gate, invalid-param handling.
  2. Byte-compat (direct handler vs the REAL CLI changelog.py): the response
     body equals the CLI's STDOUT byte-for-byte across read/stats with every
     filter combination, including the unicode json.loads->json.dumps
     (ensure_ascii=False) round-trip and the --since-filtered-to-empty stats
     path (which differs from the truly-empty-file path).

The CLI subprocess is redirected with MIND_WORLD (the documented unit-test
override in core/scripts/_paths.py) so it reads a temp world, never the real
world/changelog.jsonl. Subprocess output is captured with encoding="utf-8" so
universal-newline translation normalises Windows \\r\\n -> \\n on both sides.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CHANGELOG_PY = REPO_ROOT / "core" / "scripts" / "changelog.py"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

# Fixed-timestamp entries (deterministic for non-since byte-compat). Includes a
# unicode entry to exercise the ensure_ascii=True (stored) -> ensure_ascii=False
# (emitted) round-trip, and a file-path repeat to give stats a >1 count.
_ENTRIES = [
    {"timestamp": "2026-05-20T10:00:00", "agent": "alpha",
     "file": "world/knowledge/a.md", "action": "edit", "summary": "first", "lines_changed": 3},
    {"timestamp": "2026-05-20T10:01:00", "agent": "bravo",
     "file": "world/board/general.jsonl", "action": "create", "summary": "", "lines_changed": 0},
    {"timestamp": "2026-05-20T10:02:00", "agent": "alpha",
     "file": "world/knowledge/b.md", "action": "edit", "summary": "third", "lines_changed": 12},
    {"timestamp": "2026-05-20T10:03:00", "agent": "charlie",
     "file": "meta/strategy.yaml", "action": "delete", "summary": "removed", "lines_changed": 0},
    {"timestamp": "2026-05-20T10:04:00", "agent": "alpha",
     "file": "world/knowledge/a.md", "action": "edit", "summary": "again", "lines_changed": 1},
    {"timestamp": "2026-05-20T10:05:00", "agent": "bravo",
     "file": "world/pipeline.jsonl", "action": "restore", "summary": "restored", "lines_changed": 5},
    {"timestamp": "2026-05-20T10:06:00", "agent": "délta",
     "file": "world/café.md", "action": "edit", "summary": "déjà vu — naïve", "lines_changed": 2},
]


def _seed(world: Path, entries) -> Path:
    """Write entries the way _fileops actually stores them (ensure_ascii=True)."""
    world.mkdir(parents=True, exist_ok=True)
    p = world / "changelog.jsonl"
    p.write_text(
        "".join(json.dumps(e, ensure_ascii=True) + "\n" for e in entries),
        encoding="utf-8",
    )
    return p


def _run_cli(world: Path, args) -> str:
    env = dict(os.environ)
    env["MIND_WORLD"] = str(world)
    env["MIND_META"] = str(world.parent / "meta")
    env["MIND_AGENT"] = "alpha"
    env["MIND_AGENT_DIR"] = str(world.parent / "agent")
    proc = subprocess.run(
        [sys.executable, str(CHANGELOG_PY), *args],
        text=True, encoding="utf-8", env=env, cwd=str(REPO_ROOT),
        capture_output=True, timeout=60,
    )
    assert proc.returncode == 0, (
        f"CLI changelog.py failed (rc={proc.returncode}):\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return proc.stdout


class _FakePaths:
    def __init__(self, world: Path):
        self.world = world


class _FakeCtx:
    def __init__(self, world: Path, query: dict):
        self.paths = _FakePaths(world)
        self.query = query
        self.headers = {}


def _get(port, path, query=None):
    qs = ("?" + urllib.parse.urlencode(query)) if query else ""
    url = f"http://127.0.0.1:{port}{path}{qs}"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


# ---------------------------------------------------------------------------
# HTTP round-trip (conftest world)
# ---------------------------------------------------------------------------

def test_read_empty_world(running_daemon):
    # conftest never seeds world/changelog.jsonl -> empty body, not 404.
    _, port = running_daemon
    status, body = _get(port, "/v1/changelog/read", {"json": "1"})
    assert status == 200
    assert body == ""


def test_stats_empty_world(running_daemon):
    _, port = running_daemon
    status, body = _get(port, "/v1/changelog/stats")
    assert status == 200
    assert body == "No changelog entries.\n"


def test_read_after_seed(running_daemon):
    project_root, port = running_daemon
    _seed(project_root / "world", _ENTRIES)
    status, body = _get(port, "/v1/changelog/read", {"json": "1"})
    assert status == 200
    lines = [ln for ln in body.split("\n") if ln]
    assert len(lines) == len(_ENTRIES)
    assert json.loads(lines[0])["agent"] == "alpha"
    # Unicode round-trip: stored escaped, emitted as literal UTF-8.
    assert "déjà vu — naïve" in body


def test_stats_after_seed(running_daemon):
    project_root, port = running_daemon
    _seed(project_root / "world", _ENTRIES)
    status, body = _get(port, "/v1/changelog/stats")
    assert status == 200
    assert body.startswith("Changelog stats (all time): 7 entries\n")
    assert "By agent:\n  alpha: 3\n" in body
    assert "Top files:\n" in body


def test_read_no_agent_header_ok(running_daemon):
    # World-scoped read: no X-Mind-Agent header required (the _get helper sends none).
    project_root, port = running_daemon
    _seed(project_root / "world", _ENTRIES)
    status, body = _get(port, "/v1/changelog/read", {"agent": "alpha"})
    assert status == 200
    assert "alpha" in body


def test_read_invalid_last_400(running_daemon):
    project_root, port = running_daemon
    _seed(project_root / "world", _ENTRIES)
    try:
        _get(port, "/v1/changelog/read", {"last": "xyz"})
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "invalid_param"
    else:
        raise AssertionError("expected 400 for non-integer last")


# ---------------------------------------------------------------------------
# Byte-compat: daemon handler body == real CLI stdout
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not CHANGELOG_PY.exists(), reason="core/scripts/changelog.py missing")
class TestByteCompat:
    def _world(self, tmp_path, entries=_ENTRIES):
        world = tmp_path / "world"
        _seed(world, entries)
        return world

    def _check(self, world, cli_args, query, handler_name):
        from mind_api.src.endpoints import changelog
        cli_out = _run_cli(world, cli_args)
        handler = getattr(changelog, handler_name)
        resp = handler(_FakeCtx(world, query))
        assert resp.body.decode("utf-8") == cli_out

    def test_read_json(self, tmp_path):
        self._check(self._world(tmp_path), ["read", "--json"], {"json": "1"}, "read")

    def test_read_text(self, tmp_path):
        self._check(self._world(tmp_path), ["read"], {}, "read")

    def test_read_json_last(self, tmp_path):
        self._check(self._world(tmp_path), ["read", "--json", "--last", "3"],
                    {"json": "1", "last": "3"}, "read")

    def test_read_json_agent(self, tmp_path):
        self._check(self._world(tmp_path), ["read", "--json", "--agent", "alpha"],
                    {"json": "1", "agent": "alpha"}, "read")

    def test_read_json_file_substring(self, tmp_path):
        self._check(self._world(tmp_path), ["read", "--json", "--file", "knowledge"],
                    {"json": "1", "file": "knowledge"}, "read")

    def test_read_text_unicode_roundtrip(self, tmp_path):
        # Text mode also surfaces the literal-UTF-8 summary; covers the f-string path.
        self._check(self._world(tmp_path), ["read"], {}, "read")

    def test_read_empty(self, tmp_path):
        world = tmp_path / "world"
        world.mkdir()  # no changelog.jsonl
        self._check(world, ["read", "--json"], {"json": "1"}, "read")

    def test_stats(self, tmp_path):
        self._check(self._world(tmp_path), ["stats"], {}, "stats")

    def test_stats_empty(self, tmp_path):
        world = tmp_path / "world"
        world.mkdir()
        self._check(world, ["stats"], {}, "stats")

    def test_stats_since_window(self, tmp_path):
        # Recent + old entries; --since 1h includes only recent. Comfortable
        # margins (5min vs 3days) absorb the sub-second now() drift between the
        # in-process handler and the CLI subprocess.
        now = datetime.now()
        recent = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
        old = (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S")
        entries = [
            {"timestamp": recent, "agent": "alpha", "file": "f1", "action": "edit",
             "summary": "r1", "lines_changed": 1},
            {"timestamp": old, "agent": "bravo", "file": "f2", "action": "edit",
             "summary": "o1", "lines_changed": 1},
            {"timestamp": recent, "agent": "alpha", "file": "f3", "action": "create",
             "summary": "r2", "lines_changed": 1},
            {"timestamp": old, "agent": "charlie", "file": "f4", "action": "delete",
             "summary": "o2", "lines_changed": 1},
        ]
        world = tmp_path / "world"
        _seed(world, entries)
        self._check(world, ["read", "--json", "--since", "1h"],
                    {"json": "1", "since": "1h"}, "read")
        self._check(world, ["stats", "--since", "1h"], {"since": "1h"}, "stats")

    def test_stats_since_filtered_to_empty(self, tmp_path):
        # All entries OLD; --since 1h filters to empty. The CLI does NOT early-
        # return "No changelog entries." here (that fires only on an empty FILE,
        # before --since). It emits the full stats format with 0 entries.
        old = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S")
        entries = [
            {"timestamp": old, "agent": "alpha", "file": "f1", "action": "edit",
             "summary": "o1", "lines_changed": 1},
            {"timestamp": old, "agent": "bravo", "file": "f2", "action": "edit",
             "summary": "o2", "lines_changed": 1},
        ]
        world = tmp_path / "world"
        _seed(world, entries)
        cli_out = _run_cli(world, ["stats", "--since", "1h"])
        # Sanity: this is the full-format-with-0 path, not the early return.
        assert cli_out != "No changelog entries.\n"
        assert "0 entries" in cli_out
        from mind_api.src.endpoints import changelog
        resp = changelog.stats(_FakeCtx(world, {"since": "1h"}))
        assert resp.body.decode("utf-8") == cli_out

    def test_stats_invalid_since_no_filter(self, tmp_path):
        # "5x" is unparseable -> period shows "(last 5x)" but NO filtering.
        self._check(self._world(tmp_path), ["stats", "--since", "5x"],
                    {"since": "5x"}, "stats")
