"""GET /v1/meta/impk/compute + POST /v1/meta/impk/snapshot.

Two layers:
  1. HTTP round-trip (running_daemon): routes wired, empty-file compute,
     POST snapshot, post-snapshot compute, missing/invalid param -> 400.
  2. Byte-compat (direct handler vs the REAL CLI meta-impk.py): compute STDOUT
     equals the CLI's across all three branches (insufficient_data / computed-
     with-older / computed-no-older); snapshot's written improvement-velocity.yaml
     (CSafeDumper) + changelog match the CLI's, with the now()-stamped entry date
     and changelog timestamp normalised.

CLI redirected with MIND_META; MIND_AGENT="alpha" matches the daemon header.
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
IMPK_PY = REPO_ROOT / "core" / "scripts" / "meta-impk.py"


def _seed_velocity(meta: Path, n_entries: int) -> Path:
    """Write improvement-velocity.yaml with n deterministic entries."""
    meta.mkdir(parents=True, exist_ok=True)
    entries = [
        {"goal_id": f"g-1-{i:02d}",
         "date": f"2026-05-20T10:{i:02d}:00",
         "learning_value": round(0.30 + 0.05 * i, 4)}
        for i in range(n_entries)
    ]
    data = {"entries": entries, "rolling_averages": {}}
    p = meta / "improvement-velocity.yaml"
    p.write_text(
        yaml.safe_dump(data, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    return p


def _run_cli(meta: Path, args, agent="alpha", check_rc=True):
    env = dict(os.environ)
    env["MIND_META"] = str(meta)
    env["MIND_WORLD"] = str(meta.parent / "world")
    env["MIND_AGENT"] = agent
    env["MIND_AGENT_DIR"] = str(meta.parent / "agents" / agent)
    proc = subprocess.run(
        [sys.executable, str(IMPK_PY), *args],
        text=True, encoding="utf-8", env=env, cwd=str(REPO_ROOT),
        capture_output=True, timeout=60,
    )
    if check_rc:
        assert proc.returncode == 0, (
            f"CLI meta-impk.py failed (rc={proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return proc


class _FakePaths:
    def __init__(self, meta: Path):
        self.meta = meta


class _FakeCtx:
    def __init__(self, meta: Path, query=None, headers=None, body=None):
        self.paths = _FakePaths(meta)
        self.query = query or {}
        self.headers = headers if headers is not None else {"x-mind-agent": "alpha"}
        self.body = body


_DATE_LINE = re.compile(r"^(\s*date:\s*).*$", re.MULTILINE)


def _norm_yaml_text(meta: Path) -> str:
    """Read improvement-velocity.yaml, normalise every `date:` value (only the
    new entry's date is now()-stamped, but normalise all for safety)."""
    return _DATE_LINE.sub(r"\1<TS>", (meta / "improvement-velocity.yaml").read_text(encoding="utf-8"))


def _norm_changelog(meta: Path):
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


def _http(port, method, path, query=None, body=None, agent="alpha"):
    qs = ("?" + urllib.parse.urlencode(query)) if query else ""
    url = f"http://127.0.0.1:{port}{path}{qs}"
    data = body.encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, method=method, data=data)
    if agent:
        req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


# ---------------------------------------------------------------------------
# HTTP round-trip
# ---------------------------------------------------------------------------

def test_compute_empty(running_daemon):
    _, port = running_daemon
    status, body = _http(port, "GET", "/v1/meta/impk/compute",
                         {"window": "5", "metric": "accuracy"})
    assert status == 200
    result = json.loads(body)
    assert result["direction"] == "insufficient_data"
    assert result["entries_available"] == 0


def test_snapshot_then_compute(running_daemon):
    project_root, port = running_daemon
    _seed_velocity(project_root / "meta", 4)
    status, body = _http(port, "POST", "/v1/meta/impk/snapshot",
                         {"goal_id": "g-9-99", "learning_value": "0.7"})
    assert status == 200
    assert json.loads(body)["status"] == "recorded"
    # 5 entries now; window=2 -> computed branch.
    status, body = _http(port, "GET", "/v1/meta/impk/compute",
                         {"window": "2", "metric": "accuracy"})
    assert status == 200
    assert "imp_at_k" in json.loads(body)


def test_compute_missing_metric_defaults_ok(running_daemon):
    """: `metric` is OPTIONAL — it defaults to the single
    learning_value series, so a MISSING metric is NOT a 400. The evolve
    Step 0.7 caller relies on this (aspirations-evolve/SKILL.md invokes
    `meta-impk.sh compute --window 10` with no --metric). A missing metric
    computes normally and emits no caller-label note (metric == default)."""
    _, port = running_daemon
    status, body = _http(port, "GET", "/v1/meta/impk/compute", {"window": "5"})
    assert status == 200
    result = json.loads(body)
    assert result["series"] == "learning_value"
    assert "metric_label" not in result
    assert "note" not in result


def test_compute_invalid_window_400(running_daemon):
    _, port = running_daemon
    try:
        _http(port, "GET", "/v1/meta/impk/compute", {"window": "xx", "metric": "a"})
    except urllib.error.HTTPError as e:
        assert e.code == 400
    else:
        raise AssertionError("expected 400 for non-int window")


def test_snapshot_missing_goal_400(running_daemon):
    _, port = running_daemon
    try:
        _http(port, "POST", "/v1/meta/impk/snapshot", {"learning_value": "0.5"})
    except urllib.error.HTTPError as e:
        assert e.code == 400
    else:
        raise AssertionError("expected 400 for missing goal_id")


# ---------------------------------------------------------------------------
# Byte-compat
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not IMPK_PY.exists(), reason="core/scripts/meta-impk.py missing")
class TestByteCompat:
    def _compute_check(self, meta, window, metric):
        from mind_api.src.meta import meta_impk
        cli_out = _run_cli(meta, ["compute", "--window", str(window),
                                  "--metric", metric]).stdout
        resp = meta_impk.compute(_FakeCtx(meta, {"window": str(window), "metric": metric}))
        assert resp.body.decode("utf-8") == cli_out

    def test_compute_insufficient(self, tmp_path):
        meta = tmp_path / "m"
        _seed_velocity(meta, 3)
        self._compute_check(meta, 5, "accuracy")  # 3 < 5 -> insufficient

    def test_compute_with_older(self, tmp_path):
        meta = tmp_path / "m"
        _seed_velocity(meta, 6)
        self._compute_check(meta, 3, "accuracy")  # 6 >= 2*3 -> older window present

    def test_compute_no_older(self, tmp_path):
        meta = tmp_path / "m"
        _seed_velocity(meta, 3)
        self._compute_check(meta, 3, "accuracy")  # len==window -> older empty branch

    def test_compute_empty_file(self, tmp_path):
        meta = tmp_path / "m"
        meta.mkdir(parents=True)
        self._compute_check(meta, 5, "accuracy")

    def test_snapshot(self, tmp_path):
        from mind_api.src.meta import meta_impk
        cli_meta = tmp_path / "cli"
        dmn_meta = tmp_path / "dmn"
        _seed_velocity(cli_meta, 4)
        _seed_velocity(dmn_meta, 4)
        q = {"goal_id": "g-9-99", "learning_value": "0.72", "category": "deep"}
        cli_out = _run_cli(cli_meta, [
            "snapshot", "--goal-id", "g-9-99", "--learning-value", "0.72",
            "--category", "deep"]).stdout
        resp = meta_impk.snapshot(_FakeCtx(dmn_meta, q))
        assert resp.body.decode("utf-8") == cli_out
        assert json.loads(cli_out)["status"] == "recorded"
        # velocity.yaml byte-identical modulo the now()-stamped entry date.
        assert _norm_yaml_text(dmn_meta) == _norm_yaml_text(cli_meta)
        # rolling_averages recomputed identically (structural sanity).
        cli_data = yaml.safe_load((cli_meta / "improvement-velocity.yaml").read_text(encoding="utf-8"))
        dmn_data = yaml.safe_load((dmn_meta / "improvement-velocity.yaml").read_text(encoding="utf-8"))
        assert cli_data["rolling_averages"] == dmn_data["rolling_averages"]
        assert len(dmn_data["entries"]) == 5
        # changelog: one "edit" entry, lines_changed=0, summary="".
        assert _norm_changelog(dmn_meta) == _norm_changelog(cli_meta)
        assert _norm_changelog(dmn_meta)[0]["lines_changed"] == 0
        assert _norm_changelog(dmn_meta)[0]["summary"] == ""
        assert (dmn_meta / ".history").exists()

    def test_snapshot_active_changes(self, tmp_path):
        # Exercise the active_meta_changes split path.
        from mind_api.src.meta import meta_impk
        cli_meta = tmp_path / "cli"
        dmn_meta = tmp_path / "dmn"
        _seed_velocity(cli_meta, 2)
        _seed_velocity(dmn_meta, 2)
        cli_out = _run_cli(cli_meta, [
            "snapshot", "--goal-id", "g-2", "--learning-value", "0.4",
            "--active-changes", "mc-001, mc-002 ,"]).stdout
        resp = meta_impk.snapshot(_FakeCtx(dmn_meta, {
            "goal_id": "g-2", "learning_value": "0.4",
            "active_changes": "mc-001, mc-002 ,"}))
        assert resp.body.decode("utf-8") == cli_out
        assert _norm_yaml_text(dmn_meta) == _norm_yaml_text(cli_meta)
        dmn_data = yaml.safe_load((dmn_meta / "improvement-velocity.yaml").read_text(encoding="utf-8"))
        assert dmn_data["entries"][-1]["active_meta_changes"] == ["mc-001", "mc-002"]
