"""4 meta-yaml routes: read/set/append/log (generic meta/ YAML store).

Layers:
  1. HTTP round-trip (running_daemon): set -> read-back; append; log;
     bad-suffix -> 400; field-not-found -> 404.
  2. Byte-compat (direct handler vs the REAL CLI meta-yaml.py):
       - read: all 4 branches — whole-file YAML (DEFAULT Dumper), field YAML
         (dict/list), field scalar (str+\n), field+json, whole-file+json.
       - set non-strategy file: target YAML (CSafeDumper) + meta-log record
         (RAW append, date-normalised). No backpressure/generation side-effects.
       - set strategy file (fresh): target + meta-log + backpressure.yaml monitor
         created; strategy-generations.yaml NOT created (transition needs a
         pre-initialised generations file).
       - set goal-selection weight out-of-range: clamped identically both sides.
       - append: target YAML byte-compat.
       - log: meta-log.jsonl VERBATIM-append byte-compat (raw bytes preserved).

Bounds (load_bounds) read from the REAL core/config/meta.yaml via
project_root=REPO_ROOT for both CLI and daemon -> identical clamp.
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
MY_PY = REPO_ROOT / "core" / "scripts" / "meta-yaml.py"

_SEED = (
    "name: test\n"
    "weights:\n"
    "  novelty: 1.0\n"
    "  pressure: 2.0\n"
    "items:\n"
    "- a\n"
    "- b\n"
    "flag: true\n"
)


def _run_cli(meta, args, stdin=None, check_rc=True):
    env = dict(os.environ)
    env["MIND_META"] = str(meta)
    env["MIND_AGENT"] = "alpha"
    env["MIND_AGENT_DIR"] = str(meta.parent / "agents" / "alpha")
    proc = subprocess.run(
        [sys.executable, str(MY_PY), *args],
        text=True, encoding="utf-8", env=env, cwd=str(REPO_ROOT),
        input=stdin, capture_output=True, timeout=60,
    )
    if check_rc:
        assert proc.returncode == 0, (
            f"CLI meta-yaml.py failed (rc={proc.returncode}):\n"
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
        self.headers = headers if headers is not None else {"x-mind-agent": "alpha"}


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

def test_set_then_read_back(running_daemon):
    _, port = running_daemon
    s, _ = _http(port, "POST", "/v1/meta/yaml/set", body=json.dumps({
        "file": "rt-store.yaml", "dotpath": "a.b", "value": "42"}))
    assert s == 200
    s, b = _http(port, "GET", "/v1/meta/yaml/read",
                 {"file": "rt-store.yaml", "field": "a.b", "json": "1"})
    assert s == 200 and json.loads(b) == 42


def test_read_bad_suffix_400(running_daemon):
    _, port = running_daemon
    try:
        _http(port, "GET", "/v1/meta/yaml/read", {"file": "noext"})
    except urllib.error.HTTPError as e:
        assert e.code == 400 and json.loads(e.read())["error"] == "bad_suffix"
    else:
        raise AssertionError("expected 400 for non-.yaml target")


def test_read_field_not_found_404(running_daemon):
    _, port = running_daemon
    _http(port, "POST", "/v1/meta/yaml/set",
          body=json.dumps({"file": "rt-nf.yaml", "dotpath": "x", "value": "1"}))
    try:
        _http(port, "GET", "/v1/meta/yaml/read", {"file": "rt-nf.yaml", "field": "missing"})
    except urllib.error.HTTPError as e:
        assert e.code == 404
    else:
        raise AssertionError("expected 404 for missing field")


def test_log_and_append_wired(running_daemon):
    _, port = running_daemon
    rec = '{"meta_change_id": "mc-x", "note": "hi"}'
    s, _ = _http(port, "POST", "/v1/meta/yaml/log", body=rec)
    assert s == 200
    s, _ = _http(port, "POST", "/v1/meta/yaml/append",
                 body=json.dumps({"file": "rt-arr.yaml", "dotpath": "list", "item": {"k": 1}}))
    assert s == 200


# ---------------------------------------------------------------------------
# Byte-compat
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not MY_PY.exists(), reason="core/scripts/meta-yaml.py missing")
class TestByteCompat:
    def _meta(self, tmp_path, name):
        m = tmp_path / name / "meta"
        m.mkdir(parents=True, exist_ok=True)
        (m.parent / "agents" / "alpha").mkdir(parents=True, exist_ok=True)
        return m

    def test_read_branches(self, tmp_path):
        from mind_api.src.meta import meta_yaml
        m = self._meta(tmp_path, "r")
        (m / "store.yaml").write_text(_SEED, encoding="utf-8")

        cases = [
            ([], {"file": "store.yaml"}),                                   # whole YAML
            (["--field", "weights"], {"file": "store.yaml", "field": "weights"}),   # dict YAML
            (["--field", "items"], {"file": "store.yaml", "field": "items"}),       # list YAML
            (["--field", "name"], {"file": "store.yaml", "field": "name"}),         # scalar
            (["--field", "flag"], {"file": "store.yaml", "field": "flag"}),         # bool scalar
            (["--field", "weights", "--json"],
             {"file": "store.yaml", "field": "weights", "json": "1"}),              # field json
            (["--json"], {"file": "store.yaml", "json": "1"}),                      # whole json
        ]
        for cli_args, q in cases:
            cli = _run_cli(m, ["read", "store.yaml", *cli_args]).stdout
            resp = meta_yaml.read(_FakeCtx(m, query=q))
            assert resp.body.decode("utf-8") == cli, f"branch mismatch for {q}"

    def test_read_field_not_found(self, tmp_path):
        from mind_api.src.meta import meta_yaml
        m = self._meta(tmp_path, "rnf")
        (m / "store.yaml").write_text(_SEED, encoding="utf-8")
        proc = _run_cli(m, ["read", "store.yaml", "--field", "ghost"], check_rc=False)
        assert proc.returncode == 1
        resp = meta_yaml.read(_FakeCtx(m, query={"file": "store.yaml", "field": "ghost"}))
        assert resp.status == 404

    def test_set_non_strategy(self, tmp_path):
        from mind_api.src.meta import meta_yaml
        cm = self._meta(tmp_path, "csa")
        dm = self._meta(tmp_path, "dsa")
        _run_cli(cm, ["set", "skill-gaps.yaml", "threshold", "100"])
        resp = meta_yaml.set_field(_FakeCtx(dm, body=json.dumps({
            "file": "skill-gaps.yaml", "dotpath": "threshold", "value": "100"}).encode("utf-8")))
        assert resp.status == 200
        # target file byte-compat (CSafeDumper).
        assert (dm / "skill-gaps.yaml").read_text("utf-8") == \
            (cm / "skill-gaps.yaml").read_text("utf-8")
        loaded = yaml.safe_load((dm / "skill-gaps.yaml").read_text("utf-8"))
        assert loaded["threshold"] == 100
        # meta-log record byte-compat (date-normalised).
        assert _norm((dm / "meta-log.jsonl").read_text("utf-8")) == \
            _norm((cm / "meta-log.jsonl").read_text("utf-8"))
        rec = json.loads((dm / "meta-log.jsonl").read_text("utf-8").strip())
        assert rec["meta_change_id"] == "mc-001" and rec["new_value"] == 100
        # non-strategy -> no backpressure / generations side-effects.
        assert not (dm / "backpressure.yaml").exists()
        assert not (dm / "strategy-generations.yaml").exists()

    def test_set_strategy_file_side_effects(self, tmp_path):
        from mind_api.src.meta import meta_yaml
        cm = self._meta(tmp_path, "css")
        dm = self._meta(tmp_path, "dss")
        args = ["set", "goal-selection-strategy.yaml", "weights.novelty", "1.5"]
        _run_cli(cm, args)
        resp = meta_yaml.set_field(_FakeCtx(dm, body=json.dumps({
            "file": "goal-selection-strategy.yaml", "dotpath": "weights.novelty",
            "value": "1.5"}).encode("utf-8")))
        assert resp.status == 200
        # target + meta-log + backpressure all byte-compat (timestamps normalised).
        assert (dm / "goal-selection-strategy.yaml").read_text("utf-8") == \
            (cm / "goal-selection-strategy.yaml").read_text("utf-8")
        assert _norm((dm / "meta-log.jsonl").read_text("utf-8")) == \
            _norm((cm / "meta-log.jsonl").read_text("utf-8"))
        assert (dm / "backpressure.yaml").exists() and (cm / "backpressure.yaml").exists()
        assert _norm((dm / "backpressure.yaml").read_text("utf-8")) == \
            _norm((cm / "backpressure.yaml").read_text("utf-8"))
        mon = yaml.safe_load((dm / "backpressure.yaml").read_text("utf-8"))["active_monitors"][0]
        assert mon["meta_change_id"] == "mc-001" and mon["new_value"] == 1.5
        # generations file NOT created (transition needs a pre-initialised file).
        assert not (dm / "strategy-generations.yaml").exists()
        assert not (cm / "strategy-generations.yaml").exists()

    def test_set_weight_clamped(self, tmp_path):
        from mind_api.src.meta import meta_yaml
        cm = self._meta(tmp_path, "ccl")
        dm = self._meta(tmp_path, "dcl")
        # 99.0 is well above any plausible weight_bounds.max -> clamped both sides.
        _run_cli(cm, ["set", "goal-selection-strategy.yaml", "weights.novelty", "99.0"])
        meta_yaml.set_field(_FakeCtx(dm, body=json.dumps({
            "file": "goal-selection-strategy.yaml", "dotpath": "weights.novelty",
            "value": "99.0"}).encode("utf-8")))
        assert (dm / "goal-selection-strategy.yaml").read_text("utf-8") == \
            (cm / "goal-selection-strategy.yaml").read_text("utf-8")
        stored = yaml.safe_load(
            (dm / "goal-selection-strategy.yaml").read_text("utf-8"))["weights"]["novelty"]
        assert stored < 99.0  # clamp applied

    def test_append(self, tmp_path):
        from mind_api.src.meta import meta_yaml
        cm = self._meta(tmp_path, "cap")
        dm = self._meta(tmp_path, "dap")
        item = '{"x": 1, "y": "z"}'
        _run_cli(cm, ["append", "arr.yaml", "items"], stdin=item)
        resp = meta_yaml.append_item(_FakeCtx(dm, body=json.dumps({
            "file": "arr.yaml", "dotpath": "items", "item": {"x": 1, "y": "z"}}).encode("utf-8")))
        assert resp.status == 200
        assert (dm / "arr.yaml").read_text("utf-8") == (cm / "arr.yaml").read_text("utf-8")
        loaded = yaml.safe_load((dm / "arr.yaml").read_text("utf-8"))
        assert loaded["items"] == [{"x": 1, "y": "z"}]

    def test_log_verbatim(self, tmp_path):
        from mind_api.src.meta import meta_yaml
        cm = self._meta(tmp_path, "clg")
        dm = self._meta(tmp_path, "dlg")
        # Deliberately non-canonical spacing to prove VERBATIM append.
        rec = '{"meta_change_id":"mc-custom",  "date":"2026-01-01T00:00:00","z":1,"a":2}'
        _run_cli(cm, ["log"], stdin=rec)
        resp = meta_yaml.log_record(_FakeCtx(dm, body=rec.encode("utf-8")))
        assert resp.status == 200
        # Both append the raw record verbatim + "\n" -> byte-identical.
        assert (dm / "meta-log.jsonl").read_text("utf-8") == \
            (cm / "meta-log.jsonl").read_text("utf-8")
        assert (dm / "meta-log.jsonl").read_text("utf-8") == rec + "\n"
