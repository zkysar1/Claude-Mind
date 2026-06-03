"""POST/GET /v1/meta/dead-ends/{add,check,read,increment,review}.

Two layers:
  1. HTTP round-trip (running_daemon): routes wired, empty-meta read, POST-body
     add, no-header reads, not-found -> 200, validation -> 400.
  2. Byte-compat (direct handler vs the REAL CLI meta-dead-ends.py): the
     response body equals the CLI's STDOUT byte-for-byte, AND the written
     dead-ends.jsonl + changelog.jsonl match the CLI's, across add/increment/
     review. CLI and daemon run against SEPARATE temp meta dirs (both mutate),
     then files are diffed (changelog timestamp + review reviewed_at normalised,
     since both stamp now() independently).

The CLI subprocess is redirected with MIND_META (the documented unit-test
override in core/scripts/_paths.py) so it reads/writes a temp meta, never the
real meta/dead-ends.jsonl. MIND_AGENT="alpha" matches the daemon's
X-Mind-Agent header so changelog/.history attribution lines up.

MERGE DETERMINISM HAZARD: meta-dead-ends.py cmd_add merges overlapping records
with `existing["evidence"] = list(set(a + b))` (line 129). set() ordering of
str depends on per-process hash randomisation, so two PROCESSES (CLI subprocess
vs in-process daemon) can serialise the evidence list in different orders — this
is a property of the CLI itself (CLI-vs-CLI also diverges), not a daemon
regression. The merge byte-compat test therefore uses single-unique-element
evidence so set()->list is order-trivial; the value_range union + failure_pattern
overwrite are still exercised.
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
DEAD_ENDS_PY = REPO_ROOT / "core" / "scripts" / "meta-dead-ends.py"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

# Deterministic seed records (fixed timestamps; no now()-volatility for reads).
_SEED = [
    {"id": "de-001", "strategy_file": "goal-selection-strategy.yaml",
     "field": "weights.recency", "failure_pattern": "overfit recent goals",
     "status": "active", "category": "meta_weight",
     "registered": "2026-05-20T10:00:00", "times_matched": 2,
     "value_range": [0.8, 1.0], "evidence": ["g-001-50"]},
    {"id": "de-002", "strategy_file": "reflection-strategy.yaml",
     "field": "depth", "failure_pattern": "shallow reflection",
     "status": "reviewed", "category": "meta_heuristic",
     "registered": "2026-05-20T10:01:00", "times_matched": 0,
     "value_pattern": "naive"},
    {"id": "de-003", "strategy_file": "goal-selection-strategy.yaml",
     "field": "weights.recency", "failure_pattern": "retired one",
     "status": "retired", "category": "meta_weight",
     "registered": "2026-05-20T10:02:00", "times_matched": 9,
     "value_range": [0.0, 0.2]},
]


def _seed_meta(meta: Path, records=_SEED) -> Path:
    """Write dead-ends.jsonl the way _fileops stores it (ensure_ascii=True)."""
    meta.mkdir(parents=True, exist_ok=True)
    p = meta / "dead-ends.jsonl"
    p.write_text(
        "".join(json.dumps(r, ensure_ascii=True) + "\n" for r in records),
        encoding="utf-8",
    )
    return p


def _run_cli(meta: Path, args, stdin_input=None, agent="alpha", check_rc=True):
    env = dict(os.environ)
    env["MIND_META"] = str(meta)
    env["MIND_WORLD"] = str(meta.parent / "world")
    env["MIND_AGENT"] = agent
    env["MIND_AGENT_DIR"] = str(meta.parent / "agents" / agent)
    proc = subprocess.run(
        [sys.executable, str(DEAD_ENDS_PY), *args],
        text=True, encoding="utf-8", env=env, cwd=str(REPO_ROOT),
        input=stdin_input, capture_output=True, timeout=60,
    )
    if check_rc:
        assert proc.returncode == 0, (
            f"CLI meta-dead-ends.py failed (rc={proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return proc


class _FakePaths:
    def __init__(self, meta: Path):
        self.meta = meta


class _FakeCtx:
    def __init__(self, meta: Path, query=None, headers=None, body=None):
        self.paths = _FakePaths(meta)
        self.query = query or {}
        self.headers = headers if headers is not None else {"x-ayoai-agent": "alpha"}
        self.body = body


def _norm_changelog(meta: Path):
    """Read meta/changelog.jsonl, blank the volatile timestamp, return entries."""
    p = meta / "changelog.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        e["timestamp"] = "<TS>"
        out.append(e)
    return out


def _norm_records(meta: Path, blank_fields=()):
    """Read meta/dead-ends.jsonl, blank named volatile fields, return records."""
    p = meta / "dead-ends.jsonl"
    if not p.exists():
        return None
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        for f in blank_fields:
            if f in r:
                r[f] = "<TS>"
        out.append(r)
    return out


def _http(port, method, path, query=None, body=None, agent="alpha"):
    qs = ("?" + urllib.parse.urlencode(query)) if query else ""
    url = f"http://127.0.0.1:{port}{path}{qs}"
    data = body.encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, method=method, data=data)
    if agent:
        req.add_header("X-Mind-Agent", agent)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


# ---------------------------------------------------------------------------
# HTTP round-trip (conftest meta)
# ---------------------------------------------------------------------------

def test_read_empty_meta(running_daemon):
    # conftest never seeds meta/dead-ends.jsonl -> "[]" body, not 404.
    _, port = running_daemon
    status, body = _http(port, "GET", "/v1/meta/dead-ends/read")
    assert status == 200
    assert body == "[]\n"


def test_add_then_read_roundtrip(running_daemon):
    _, port = running_daemon
    rec = {"strategy_file": "s.yaml", "field": "f", "failure_pattern": "fp",
           "registered": "2026-05-20T09:00:00", "status": "active",
           "category": "meta_weight", "times_matched": 0,
           "value_range": [1.0, 2.0]}
    status, body = _http(port, "POST", "/v1/meta/dead-ends/add", body=json.dumps(rec))
    assert status == 200
    outcome = json.loads(body)
    assert outcome["status"] == "added"
    assert outcome["id"] == "de-001"
    # Read it back.
    status, body = _http(port, "GET", "/v1/meta/dead-ends/read")
    assert status == 200
    recs = json.loads(body)
    assert len(recs) == 1 and recs[0]["id"] == "de-001"


def test_check_blocked_roundtrip(running_daemon):
    project_root, port = running_daemon
    _seed_meta(project_root / "meta")
    status, body = _http(port, "GET", "/v1/meta/dead-ends/check",
                         {"file": "goal-selection-strategy.yaml",
                          "field": "weights.recency", "value": "0.9"})
    assert status == 200
    result = json.loads(body)
    assert result["blocked"] is True
    assert result["matches"][0]["id"] == "de-001"


def test_read_no_header_ok(running_daemon):
    # Meta-scoped read: no X-Mind-Agent header required.
    project_root, port = running_daemon
    _seed_meta(project_root / "meta")
    status, body = _http(port, "GET", "/v1/meta/dead-ends/read", agent=None)
    assert status == 200
    assert json.loads(body)  # non-empty list


def test_increment_not_found_roundtrip(running_daemon):
    project_root, port = running_daemon
    _seed_meta(project_root / "meta")
    status, body = _http(port, "POST", "/v1/meta/dead-ends/increment", {"id": "de-999"})
    assert status == 200
    assert json.loads(body)["error"] == "Dead end de-999 not found"


def test_add_invalid_category_400(running_daemon):
    _, port = running_daemon
    rec = {"strategy_file": "s.yaml", "field": "f", "failure_pattern": "fp",
           "category": "bogus"}
    try:
        _http(port, "POST", "/v1/meta/dead-ends/add", body=json.dumps(rec))
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "invalid_category"
    else:
        raise AssertionError("expected 400 for invalid category")


def test_add_missing_field_400(running_daemon):
    _, port = running_daemon
    rec = {"strategy_file": "s.yaml", "category": "meta_weight"}  # no field/failure_pattern
    try:
        _http(port, "POST", "/v1/meta/dead-ends/add", body=json.dumps(rec))
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "missing_field"
    else:
        raise AssertionError("expected 400 for missing field")


def test_add_empty_body_400(running_daemon):
    _, port = running_daemon
    try:
        _http(port, "POST", "/v1/meta/dead-ends/add", body="")
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "invalid_body"
    else:
        raise AssertionError("expected 400 for empty body")


# ---------------------------------------------------------------------------
# Byte-compat: daemon handler body == real CLI stdout (+ written files)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not DEAD_ENDS_PY.exists(), reason="core/scripts/meta-dead-ends.py missing")
class TestByteCompat:
    def _meta(self, tmp_path, name, records=_SEED):
        meta = tmp_path / name
        if records is not None:
            _seed_meta(meta, records)
        else:
            meta.mkdir(parents=True, exist_ok=True)
        return meta

    # ---- reads: direct handler vs CLI stdout -----------------------------
    def _read_check(self, meta, cli_args, query, handler_name):
        from mind_api.src.meta import meta_dead_ends
        cli_out = _run_cli(meta, cli_args).stdout
        handler = getattr(meta_dead_ends, handler_name)
        resp = handler(_FakeCtx(meta, query))
        assert resp.body.decode("utf-8") == cli_out

    def test_read_all(self, tmp_path):
        self._read_check(self._meta(tmp_path, "m"), ["read"], {}, "read")

    def test_read_active(self, tmp_path):
        self._read_check(self._meta(tmp_path, "m"), ["read", "--active"],
                         {"active": "1"}, "read")

    def test_read_category(self, tmp_path):
        self._read_check(self._meta(tmp_path, "m"),
                         ["read", "--category", "meta_weight"],
                         {"category": "meta_weight"}, "read")

    def test_read_empty(self, tmp_path):
        self._read_check(self._meta(tmp_path, "m", records=None),
                         ["read"], {}, "read")

    def test_check_blocked_range(self, tmp_path):
        self._read_check(
            self._meta(tmp_path, "m"),
            ["check", "--file", "goal-selection-strategy.yaml",
             "--field", "weights.recency", "--value", "0.9"],
            {"file": "goal-selection-strategy.yaml",
             "field": "weights.recency", "value": "0.9"}, "check")

    def test_check_not_blocked(self, tmp_path):
        self._read_check(
            self._meta(tmp_path, "m"),
            ["check", "--file", "goal-selection-strategy.yaml",
             "--field", "weights.recency", "--value", "0.1"],
            {"file": "goal-selection-strategy.yaml",
             "field": "weights.recency", "value": "0.1"}, "check")

    def test_check_pattern_match(self, tmp_path):
        # de-002 reviewed + value_pattern "naive" -> substring match blocks.
        self._read_check(
            self._meta(tmp_path, "m"),
            ["check", "--file", "reflection-strategy.yaml",
             "--field", "depth", "--value", "very NAIVE approach"],
            {"file": "reflection-strategy.yaml",
             "field": "depth", "value": "very NAIVE approach"}, "check")

    # ---- writes: daemon vs CLI on SEPARATE meta dirs ---------------------
    def test_add_appended(self, tmp_path):
        from mind_api.src.meta import meta_dead_ends
        rec = {"strategy_file": "encoding-strategy.yaml", "field": "lane",
               "failure_pattern": "wrong lane", "registered": "2026-05-20T11:00:00",
               "status": "active", "category": "encoding_rule", "times_matched": 0,
               "value_range": [3.0, 7.0], "evidence": ["g-009-01"]}
        cli_meta = self._meta(tmp_path, "cli")
        dmn_meta = self._meta(tmp_path, "dmn")
        cli_out = _run_cli(cli_meta, ["add"], stdin_input=json.dumps(rec)).stdout
        resp = meta_dead_ends.add(_FakeCtx(dmn_meta, body=json.dumps(rec).encode("utf-8")))
        assert resp.body.decode("utf-8") == cli_out
        assert json.loads(cli_out)["status"] == "added"
        # dead-ends.jsonl byte-identical (registered supplied -> no now()).
        assert (dmn_meta / "dead-ends.jsonl").read_bytes() == \
               (cli_meta / "dead-ends.jsonl").read_bytes()
        # changelog matches (timestamp normalised) and fired exactly once.
        assert _norm_changelog(dmn_meta) == _norm_changelog(cli_meta)
        assert len(_norm_changelog(dmn_meta)) == 1
        assert _norm_changelog(dmn_meta)[0]["summary"] == ""
        assert _norm_changelog(dmn_meta)[0]["file"] == "dead-ends.jsonl"
        # .history snapshot fired on both.
        assert (dmn_meta / ".history").exists()
        assert (cli_meta / ".history").exists()

    def test_add_merged(self, tmp_path):
        from mind_api.src.meta import meta_dead_ends
        # Single-unique-element evidence -> set()->list order-trivial.
        rec = {"strategy_file": "goal-selection-strategy.yaml",
               "field": "weights.recency", "failure_pattern": "updated pattern",
               "registered": "2026-05-20T11:30:00", "status": "active",
               "category": "meta_weight", "times_matched": 0,
               "value_range": [0.9, 1.2], "evidence": ["g-001-50"]}
        cli_meta = self._meta(tmp_path, "cli")
        dmn_meta = self._meta(tmp_path, "dmn")
        cli_out = _run_cli(cli_meta, ["add"], stdin_input=json.dumps(rec)).stdout
        resp = meta_dead_ends.add(_FakeCtx(dmn_meta, body=json.dumps(rec).encode("utf-8")))
        assert resp.body.decode("utf-8") == cli_out
        out = json.loads(cli_out)
        assert out["status"] == "merged" and out["id"] == "de-001"
        assert (dmn_meta / "dead-ends.jsonl").read_bytes() == \
               (cli_meta / "dead-ends.jsonl").read_bytes()
        # Sanity: value_range got unioned to [0.8, 1.2].
        merged = _norm_records(dmn_meta)[0]
        assert merged["value_range"] == [0.8, 1.2]
        assert merged["failure_pattern"] == "updated pattern"
        assert _norm_changelog(dmn_meta) == _norm_changelog(cli_meta)

    def test_increment_found(self, tmp_path):
        from mind_api.src.meta import meta_dead_ends
        cli_meta = self._meta(tmp_path, "cli")
        dmn_meta = self._meta(tmp_path, "dmn")
        cli_out = _run_cli(cli_meta, ["increment", "de-001"]).stdout
        resp = meta_dead_ends.increment(_FakeCtx(dmn_meta, {"id": "de-001"}))
        assert resp.body.decode("utf-8") == cli_out
        assert json.loads(cli_out) == {"status": "incremented", "id": "de-001"}
        # No new timestamp in the record -> dead-ends.jsonl byte-identical.
        assert (dmn_meta / "dead-ends.jsonl").read_bytes() == \
               (cli_meta / "dead-ends.jsonl").read_bytes()
        # times_matched bumped 2 -> 3.
        assert _norm_records(dmn_meta)[0]["times_matched"] == 3
        assert _norm_changelog(dmn_meta) == _norm_changelog(cli_meta)

    def test_increment_not_found(self, tmp_path):
        from mind_api.src.meta import meta_dead_ends
        cli_meta = self._meta(tmp_path, "cli")
        dmn_meta = self._meta(tmp_path, "dmn")
        before = (cli_meta / "dead-ends.jsonl").read_bytes()
        cli_out = _run_cli(cli_meta, ["increment", "de-777"]).stdout
        resp = meta_dead_ends.increment(_FakeCtx(dmn_meta, {"id": "de-777"}))
        assert resp.body.decode("utf-8") == cli_out
        assert json.loads(cli_out) == {"error": "Dead end de-777 not found"}
        # No write, no changelog on the not-found path.
        assert (cli_meta / "dead-ends.jsonl").read_bytes() == before
        assert (dmn_meta / "dead-ends.jsonl").read_bytes() == before
        assert not (cli_meta / "changelog.jsonl").exists()
        assert not (dmn_meta / "changelog.jsonl").exists()

    def test_review_found(self, tmp_path):
        from mind_api.src.meta import meta_dead_ends
        cli_meta = self._meta(tmp_path, "cli")
        dmn_meta = self._meta(tmp_path, "dmn")
        cli_out = _run_cli(cli_meta, ["review", "de-001"]).stdout
        resp = meta_dead_ends.review(_FakeCtx(dmn_meta, {"id": "de-001"}))
        assert resp.body.decode("utf-8") == cli_out
        assert json.loads(cli_out) == {"status": "reviewed", "id": "de-001"}
        # reviewed_at is now()-stamped independently -> normalise before compare.
        assert _norm_records(dmn_meta, blank_fields=("reviewed_at",)) == \
               _norm_records(cli_meta, blank_fields=("reviewed_at",))
        rec = _norm_records(dmn_meta)[0]
        assert rec["status"] == "reviewed" and "reviewed_at" in rec
        assert _norm_changelog(dmn_meta) == _norm_changelog(cli_meta)

    def test_review_not_found(self, tmp_path):
        from mind_api.src.meta import meta_dead_ends
        cli_meta = self._meta(tmp_path, "cli")
        dmn_meta = self._meta(tmp_path, "dmn")
        cli_out = _run_cli(cli_meta, ["review", "de-888"]).stdout
        resp = meta_dead_ends.review(_FakeCtx(dmn_meta, {"id": "de-888"}))
        assert resp.body.decode("utf-8") == cli_out
        assert json.loads(cli_out) == {"error": "Dead end de-888 not found"}
        assert not (dmn_meta / "changelog.jsonl").exists()
