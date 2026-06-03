"""POST /v1/meta/transfer-export + POST /v1/meta/transfer-import.

Layers:
  1. HTTP round-trip (running_daemon): export route wired (X-Mind-Agent
     required -> 400 without it), import route wired, missing-input -> 404.
  2. Byte-compat (direct handler vs the REAL CLI meta-transfer.py):
       - export: bundle file (RAW default-Dumper, timestamp-normalised) +
         transfer/_index.yaml (locked CSafeDumper, path+timestamp-normalised) +
         stdout {status,strategies} (path differs by temp dir, compared apart).
       - import: the 3 merged strategy files byte-for-byte (no timestamps in
         strategy data -> direct compare) + stdout {changes,details}
         byte-identical (deterministic, timestamp-free). Clamp [0,3] +
         existing-keys-only weight merge + full depth_allocation replace +
         list-append-with-source all exercised; the changes COUNT tracks only
         weights + depth_allocation (list appends excluded) — verified.
       - dry-run: stdout byte-for-byte, no writes.

CLI redirected via MIND_META + MIND_AGENT_DIR; the bundle output / input
paths are passed as absolute strings to both sides so the dual-Dumper split
and merge semantics are compared on identical inputs.
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
MT_PY = REPO_ROOT / "core" / "scripts" / "meta-transfer.py"

_GOAL_SEL = (
    "weights:\n"
    "  novelty: 1.0\n"
    "  completion_pressure: 1.5\n"
    "selection_heuristics:\n"
    "  - rule: existing-heuristic\n"
)
_REFLECTION = (
    "depth_allocation:\n"
    "  micro: 0.2\n"
    "  session: 0.5\n"
    "trigger_overrides:\n"
    "  - name: existing-trigger\n"
)
_ENCODING = (
    "priority_rules:\n"
    "  - rule: existing-rule\n"
)
_META_STATE = "total_meta_changes: 42\n"

_BUNDLE = {
    "exported": "2026-01-01T00:00:00",
    "source_agent": "exporter-x",
    "total_goals_at_export": 7,
    "strategies": {
        "goal_selection": {
            "weights": {
                "novelty": 2.5,            # existing, in range -> merged 2.5
                "completion_pressure": 5.0,  # existing, clamped -> 3.0
                "nonexistent_key": 9.0,    # not existing -> skipped
            },
            "selection_heuristics": [{"rule": "imported-heuristic"}],
        },
        "reflection": {
            "depth_allocation": {"micro": 0.9, "long": 0.1},  # full replace
            "trigger_overrides": [{"name": "imported-trigger"}],
        },
        "encoding": {
            "priority_rules": [{"rule": "imported-rule"}],
        },
    },
}


def _seed_meta(meta: Path):
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "goal-selection-strategy.yaml").write_text(_GOAL_SEL, encoding="utf-8")
    (meta / "reflection-strategy.yaml").write_text(_REFLECTION, encoding="utf-8")
    (meta / "encoding-strategy.yaml").write_text(_ENCODING, encoding="utf-8")
    (meta / "meta.yaml").write_text(_META_STATE, encoding="utf-8")
    (meta / "transfer").mkdir(parents=True, exist_ok=True)


def _seed_agent(agent: Path, name="test-agent"):
    agent.mkdir(parents=True, exist_ok=True)
    (agent / "self.md").write_text(
        '---\nname: "{}"\n---\n\nIdentity body.\n'.format(name), encoding="utf-8")


def _run_cli(meta, agent, args, check_rc=True):
    env = dict(os.environ)
    env["MIND_META"] = str(meta)
    env["MIND_AGENT"] = "alpha"
    env["MIND_AGENT_DIR"] = str(agent)
    proc = subprocess.run(
        [sys.executable, str(MT_PY), *args],
        text=True, encoding="utf-8", env=env, cwd=str(REPO_ROOT),
        capture_output=True, timeout=60,
    )
    if check_rc:
        assert proc.returncode == 0, (
            f"CLI meta-transfer.py failed (rc={proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return proc


class _FakePaths:
    def __init__(self, meta, agent):
        self.meta = meta
        self.world = meta.parent / "world"
        self.agent = agent
        self.project_root = REPO_ROOT


class _FakeCtx:
    def __init__(self, meta, agent, query=None, body=None, headers=None):
        self.paths = _FakePaths(meta, agent)
        self.query = query or {}
        self.body = body
        self.headers = headers if headers is not None else {}


_TS_RE = re.compile(r'(exported):\s*\S+')


def _norm_ts(text):
    return _TS_RE.sub(r'\1: <TS>', text)


def _norm_index(text, meta: Path):
    # Normalise the temp-dir bundle path prefix + the exported timestamp.
    out = text.replace(str(meta), "<META>").replace(meta.as_posix(), "<META>")
    return _norm_ts(out)


def _http(port, method, path, query=None, body=None, headers=None):
    qs = ("?" + urllib.parse.urlencode(query)) if query else ""
    url = f"http://127.0.0.1:{port}{path}{qs}"
    data = body.encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, method=method, data=data)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.read().decode("utf-8")


# ---------------------------------------------------------------------------
# HTTP round-trip
# ---------------------------------------------------------------------------

def test_export_requires_agent_header(running_daemon):
    _, port = running_daemon
    try:
        _http(port, "POST", "/v1/meta/transfer-export",
              body=json.dumps({"output": "x.yaml"}))
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "missing_agent_header"
    else:
        raise AssertionError("expected 400 without X-Mind-Agent")


def test_export_route_wired(running_daemon):
    pr, port = running_daemon
    status, body = _http(port, "POST", "/v1/meta/transfer-export",
                         body=json.dumps({"output": "rt-bundle.yaml"}),
                         headers={"X-Mind-Agent": "alpha"})
    assert status == 200
    out = json.loads(body)
    assert out["status"] == "exported"
    assert out["strategies"] == ["goal_selection", "reflection", "encoding"]
    # Bundle landed under meta/transfer/.
    assert (pr / "meta" / "transfer" / "rt-bundle.yaml").exists()
    assert (pr / "meta" / "transfer" / "_index.yaml").exists()


def test_import_missing_input_404(running_daemon):
    _, port = running_daemon
    try:
        _http(port, "POST", "/v1/meta/transfer-import",
              body=json.dumps({"input": "/nonexistent/ghost-bundle.yaml"}))
    except urllib.error.HTTPError as e:
        assert e.code == 404
        assert json.loads(e.read())["error"] == "not_found"
    else:
        raise AssertionError("expected 404 for missing bundle")


def test_import_route_wired(running_daemon):
    pr, port = running_daemon
    bundle_path = pr / "meta" / "rt-import-bundle.yaml"
    bundle_path.write_text(yaml.safe_dump(_BUNDLE, sort_keys=False), encoding="utf-8")
    status, body = _http(port, "POST", "/v1/meta/transfer-import",
                         body=json.dumps({"input": str(bundle_path)}))
    assert status == 200
    out = json.loads(body)
    assert out["status"] == "imported"
    # conftest meta has no pre-existing weights -> only depth_allocation merges.
    assert out["changes"] == 1


# ---------------------------------------------------------------------------
# Byte-compat
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not MT_PY.exists(), reason="core/scripts/meta-transfer.py missing")
class TestByteCompat:
    def test_export(self, tmp_path):
        from mind_api.src.meta import meta_transfer
        cli_meta = tmp_path / "cli" / "meta"
        dmn_meta = tmp_path / "dmn" / "meta"
        agent = tmp_path / "agents" / "alpha"
        _seed_meta(cli_meta)
        _seed_meta(dmn_meta)
        _seed_agent(agent)

        cli_bundle = cli_meta / "transfer" / "bundle.yaml"
        dmn_bundle = dmn_meta / "transfer" / "bundle.yaml"
        cli_out = _run_cli(cli_meta, agent, ["export", "--output", str(cli_bundle)]).stdout
        resp = meta_transfer.export(_FakeCtx(
            dmn_meta, agent, headers={"x-ayoai-agent": "alpha"},
            body=json.dumps({"output": str(dmn_bundle)}).encode("utf-8")))
        assert resp.status == 200
        dmn_out = resp.body.decode("utf-8")

        # stdout: status + strategies match (path differs by temp dir).
        cj, dj = json.loads(cli_out), json.loads(dmn_out)
        assert cj["status"] == dj["status"] == "exported"
        assert cj["strategies"] == dj["strategies"] == [
            "goal_selection", "reflection", "encoding"]

        # Bundle file: RAW default-Dumper, timestamp-normalised byte-compat.
        cli_b = cli_bundle.read_text(encoding="utf-8")
        dmn_b = dmn_bundle.read_text(encoding="utf-8")
        assert _norm_ts(dmn_b) == _norm_ts(cli_b)
        loaded = yaml.safe_load(dmn_b)
        assert loaded["source_agent"] == "test-agent"
        assert loaded["total_goals_at_export"] == 42

        # _index.yaml: locked CSafeDumper, path+timestamp-normalised byte-compat.
        cli_i = (cli_meta / "transfer" / "_index.yaml").read_text(encoding="utf-8")
        dmn_i = (dmn_meta / "transfer" / "_index.yaml").read_text(encoding="utf-8")
        assert _norm_index(dmn_i, dmn_meta) == _norm_index(cli_i, cli_meta)
        # New _index file -> history.snapshot returns None (no snapshot), matching
        # CLI save_history; changelog.append always fires (the CSafeDumper locked
        # path's observable side-effect, distinguishing it from the bundle's RAW
        # default-Dumper write which has neither).
        assert (dmn_meta / ".history").exists() == (cli_meta / ".history").exists()
        assert (dmn_meta / "changelog.jsonl").exists()
        assert (cli_meta / "changelog.jsonl").exists()

    def test_import(self, tmp_path):
        from mind_api.src.meta import meta_transfer
        cli_meta = tmp_path / "cli" / "meta"
        dmn_meta = tmp_path / "dmn" / "meta"
        agent = tmp_path / "agents" / "alpha"
        _seed_meta(cli_meta)
        _seed_meta(dmn_meta)
        _seed_agent(agent)
        bundle_path = tmp_path / "bundle.yaml"
        bundle_path.write_text(yaml.safe_dump(_BUNDLE, sort_keys=False), encoding="utf-8")

        cli_out = _run_cli(cli_meta, agent, ["import", "--input", str(bundle_path)]).stdout
        resp = meta_transfer.import_bundle(_FakeCtx(
            dmn_meta, agent, body=json.dumps({"input": str(bundle_path)}).encode("utf-8")))
        assert resp.status == 200
        dmn_out = resp.body.decode("utf-8")

        # stdout byte-identical (deterministic, timestamp-free).
        assert dmn_out == cli_out
        out = json.loads(dmn_out)
        # 2 weight merges (novelty + completion_pressure) + 1 depth_allocation.
        assert out["changes"] == 3
        fields = [c["field"] for c in out["details"]]
        assert fields == ["weights.novelty", "weights.completion_pressure",
                          "depth_allocation"]
        # clamp [0,3] applied to the out-of-range weight.
        cp = next(c for c in out["details"] if c["field"] == "weights.completion_pressure")
        assert cp["value"] == 3.0

        # The 3 merged strategy files: no timestamps -> direct byte-compat.
        for fname in ("goal-selection-strategy.yaml", "reflection-strategy.yaml",
                      "encoding-strategy.yaml"):
            cli_t = (cli_meta / fname).read_text(encoding="utf-8")
            dmn_t = (dmn_meta / fname).read_text(encoding="utf-8")
            assert dmn_t == cli_t, f"merged {fname} differs"

        # Semantic spot-checks on the daemon-written files.
        gs = yaml.safe_load((dmn_meta / "goal-selection-strategy.yaml").read_text("utf-8"))
        assert gs["weights"] == {"novelty": 2.5, "completion_pressure": 3.0}
        assert gs["selection_heuristics"][-1] == {
            "rule": "imported-heuristic", "source": "transfer from exporter-x"}
        ref = yaml.safe_load((dmn_meta / "reflection-strategy.yaml").read_text("utf-8"))
        assert ref["depth_allocation"] == {"micro": 0.9, "long": 0.1}  # full replace
        enc = yaml.safe_load((dmn_meta / "encoding-strategy.yaml").read_text("utf-8"))
        assert enc["priority_rules"][-1] == {
            "rule": "imported-rule", "source": "transfer from exporter-x"}

    def test_import_dry_run_no_write(self, tmp_path):
        from mind_api.src.meta import meta_transfer
        cli_meta = tmp_path / "cli" / "meta"
        dmn_meta = tmp_path / "dmn" / "meta"
        agent = tmp_path / "agents" / "alpha"
        _seed_meta(cli_meta)
        _seed_meta(dmn_meta)
        _seed_agent(agent)
        bundle_path = tmp_path / "bundle.yaml"
        bundle_path.write_text(yaml.safe_dump(_BUNDLE, sort_keys=False), encoding="utf-8")

        cli_out = _run_cli(cli_meta, agent,
                           ["import", "--input", str(bundle_path), "--dry-run"]).stdout
        resp = meta_transfer.import_bundle(_FakeCtx(
            dmn_meta, agent,
            body=json.dumps({"input": str(bundle_path), "dry_run": True}).encode("utf-8")))
        assert resp.status == 200
        assert resp.body.decode("utf-8") == cli_out
        out = json.loads(resp.body.decode("utf-8"))
        assert out["dry_run"] is True
        assert [c["strategy"] for c in out["changes"]] == [
            "goal_selection", "reflection", "encoding"]

        # Dry-run wrote nothing — strategy files unchanged from seed.
        assert (dmn_meta / "goal-selection-strategy.yaml").read_text("utf-8") == _GOAL_SEL
        assert (dmn_meta / "reflection-strategy.yaml").read_text("utf-8") == _REFLECTION
        assert (dmn_meta / "encoding-strategy.yaml").read_text("utf-8") == _ENCODING
