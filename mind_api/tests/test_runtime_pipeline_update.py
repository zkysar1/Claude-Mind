"""POST /v1/pipeline/update — endpoint correctness tests.

Mirrors test_runtime_pipeline_writers.py pattern: tests verify the WRITE
MACHINERY contract (lock, history, atomic write, changelog, cache invalidate).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest


def _post(port: int, path: str, query: dict = None, body: bytes = b"",
          *, agent: str = "alpha"):
    qs = urllib.parse.urlencode(query) if query else ""
    url = f"http://127.0.0.1:{port}{path}?{qs}" if qs else f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


def _post_expect_error(port: int, path: str, query: dict = None,
                       body: bytes = b"", *, agent: str = "alpha"):
    qs = urllib.parse.urlencode(query) if query else ""
    url = f"http://127.0.0.1:{port}{path}?{qs}" if qs else f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _read_jsonl(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


# A minimal valid pipeline record for tests.
def _rec(**kwargs) -> dict:
    base = {
        "id": "2026-05-14_test-hyp",
        "title": "Test hypothesis about something interesting",
        "stage": "discovered",
        "horizon": "session",
        "type": "calibration",
        "confidence": 0.6,
        "position": "YES this is a valid multi-word position claim",
        "formed_date": "2026-05-14",
        "category": "test-cat",
        "claim": "This is a valid claim field that is longer than twenty characters",
    }
    base.update(kwargs)
    return base


# Seed records that pass full validation (conftest seeds are minimal/reader-only).
_SEED_ACTIVE = json.dumps({
    "id": "2026-05-12_test-active",
    "title": "Test active hypothesis for pipeline update tests",
    "stage": "active",
    "horizon": "session",
    "type": "calibration",
    "confidence": 0.6,
    "position": "YES this is a valid multi-word active hypothesis position",
    "formed_date": "2026-05-12",
    "category": "test-cat",
    "reflected": False,
})

_SEED_RESOLVED = json.dumps({
    "id": "2026-05-12_test-resolved",
    "title": "Test resolved hypothesis for pipeline update tests",
    "stage": "resolved",
    "horizon": "session",
    "type": "calibration",
    "confidence": 0.7,
    "position": "YES this is a valid multi-word resolved hypothesis position",
    "formed_date": "2026-05-12",
    "category": "test-cat",
    "outcome": "CONFIRMED",
    "reflected": True,
})


@pytest.fixture
def pipeline_daemon(running_daemon):
    """Re-seed pipeline.jsonl with records that pass full validation."""
    project_root, port = running_daemon
    live = project_root / "world" / "pipeline.jsonl"
    live.write_text(_SEED_ACTIVE + "\n" + _SEED_RESOLVED + "\n",
                    encoding="utf-8")
    (project_root / "world" / "pipeline-archive.jsonl").write_text(
        "", encoding="utf-8")
    return project_root, port


# ---------------------------------------------------------------------------
# pipeline/update — happy path
# ---------------------------------------------------------------------------

def test_update_replaces_record(pipeline_daemon):
    """Full record replacement: new record overwrites the existing one."""
    project_root, port = pipeline_daemon
    live = project_root / "world" / "pipeline.jsonl"

    replacement = _rec(id="2026-05-12_test-active",
                       title="Replaced title for update test",
                       stage="active",
                       confidence=0.9)

    status, body = _post(
        port, "/v1/pipeline/update",
        {"id": "2026-05-12_test-active"},
        json.dumps(replacement).encode("utf-8"))
    assert status == 200
    resp = json.loads(body)
    assert resp["ok"] is True
    assert resp["record_id"] == "2026-05-12_test-active"
    assert resp["record"]["title"] == "Replaced title for update test"
    assert resp["record"]["confidence"] == 0.9

    items = _read_jsonl(live)
    rec = next(r for r in items if r["id"] == "2026-05-12_test-active")
    assert rec["title"] == "Replaced title for update test"
    assert rec["confidence"] == 0.9


def test_update_preserves_other_records(pipeline_daemon):
    """Updating one record does not alter sibling records."""
    project_root, port = pipeline_daemon
    live = project_root / "world" / "pipeline.jsonl"

    replacement = _rec(id="2026-05-12_test-active",
                       title="Replaced for preservation test",
                       stage="active")

    _post(port, "/v1/pipeline/update",
          {"id": "2026-05-12_test-active"},
          json.dumps(replacement).encode("utf-8"))

    items = _read_jsonl(live)
    resolved = next(r for r in items if r["id"] == "2026-05-12_test-resolved")
    assert resolved["title"] == "Test resolved hypothesis for pipeline update tests"


def test_update_normalizes_record(pipeline_daemon):
    """Normalization runs on the replacement record (e.g. slug derivation)."""
    _, port = pipeline_daemon

    rec = _rec(id="2026-05-12_test-active", stage="active")
    # Remove slug — normalization should derive it from id.
    rec.pop("slug", None)

    status, body = _post(
        port, "/v1/pipeline/update",
        {"id": "2026-05-12_test-active"},
        json.dumps(rec).encode("utf-8"))
    assert status == 200
    resp = json.loads(body)
    assert resp["record"].get("slug") == "test-active"


# ---------------------------------------------------------------------------
# pipeline/update — error paths
# ---------------------------------------------------------------------------

def test_update_missing_id_param(pipeline_daemon):
    """Missing 'id' query parameter returns 400."""
    _, port = pipeline_daemon
    status, body = _post_expect_error(
        port, "/v1/pipeline/update",
        body=json.dumps(_rec()).encode("utf-8"))
    assert status == 400
    assert "missing_param" in body


def test_update_record_not_found(pipeline_daemon):
    """Non-existent record ID returns 404."""
    _, port = pipeline_daemon
    replacement = _rec(id="2026-01-01_nonexistent")
    status, body = _post_expect_error(
        port, "/v1/pipeline/update",
        {"id": "2026-01-01_nonexistent"},
        json.dumps(replacement).encode("utf-8"))
    assert status == 404
    assert "record_not_found" in body


def test_update_invalid_body(pipeline_daemon):
    """Non-JSON body returns 400."""
    _, port = pipeline_daemon
    status, body = _post_expect_error(
        port, "/v1/pipeline/update",
        {"id": "2026-05-12_test-active"},
        b"not json")
    assert status == 400
    assert "invalid_body" in body


def test_update_empty_body(pipeline_daemon):
    """Empty body returns 400."""
    _, port = pipeline_daemon
    status, body = _post_expect_error(
        port, "/v1/pipeline/update",
        {"id": "2026-05-12_test-active"},
        b"")
    assert status == 400
    assert "invalid_body" in body


def test_update_validation_failed(pipeline_daemon):
    """Record that fails validation returns 400."""
    _, port = pipeline_daemon
    bad = _rec(id="2026-05-12_test-active", stage="active")
    del bad["title"]  # missing required field
    status, body = _post_expect_error(
        port, "/v1/pipeline/update",
        {"id": "2026-05-12_test-active"},
        json.dumps(bad).encode("utf-8"))
    assert status == 400
    assert "validation_failed" in body


def test_update_formation_quality_failed(pipeline_daemon):
    """Record that fails formation quality returns 400."""
    _, port = pipeline_daemon
    bad = _rec(id="2026-05-12_test-active", stage="active",
               claim="short")  # claim too short for non-discovered
    status, body = _post_expect_error(
        port, "/v1/pipeline/update",
        {"id": "2026-05-12_test-active"},
        json.dumps(bad).encode("utf-8"))
    assert status == 400
    assert "validation_failed" in body


# NOTE (5 / rb-2239): update() now probes the ARCHIVE when the id is
# absent from live, so multi-field-corrupt archived records have an atomic
# whole-record repair path. The former `test_update_does_not_search_archive`
# (404 on archive-only ids) asserted the retired live-only contract and was
# removed. The current contract is pinned in test_runtime_pipeline_writers.py::
# test_update_reaches_archive and ::test_update_not_found_in_either.


# ---------------------------------------------------------------------------
# pipeline/update — observability
# ---------------------------------------------------------------------------

def test_update_history_snapshot_created(pipeline_daemon):
    """History snapshot is created on update."""
    project_root, port = pipeline_daemon
    history_dir = project_root / "world" / ".history" / "snapshots" / "pipeline.jsonl"
    assert not history_dir.exists()

    replacement = _rec(id="2026-05-12_test-active", stage="active")
    _post(port, "/v1/pipeline/update",
          {"id": "2026-05-12_test-active"},
          json.dumps(replacement).encode("utf-8"))
    assert history_dir.exists()


def test_update_changelog_appended(pipeline_daemon):
    """Changelog entry is appended on update."""
    project_root, port = pipeline_daemon
    cl = project_root / "world" / "changelog.jsonl"

    replacement = _rec(id="2026-05-12_test-active", stage="active")
    _post(port, "/v1/pipeline/update",
          {"id": "2026-05-12_test-active"},
          json.dumps(replacement).encode("utf-8"))
    assert cl.exists()
    entries = _read_jsonl(cl)
    assert any("pipeline-update" in (e.get("summary", "") or "") for e in entries)


def test_update_recomputes_meta(pipeline_daemon):
    """After update, pipeline-meta.json is recomputed (mirrors pipeline.py
    cmd_update which calls _update_meta_counts). Conftest seeds a placeholder
    meta without `last_updated`; _update_meta overwrites it with the full
    computed shape."""
    project_root, port = pipeline_daemon
    meta_path = project_root / "world" / "pipeline-meta.json"

    replacement = _rec(id="2026-05-12_test-active",
                       title="Replaced for meta-recompute test",
                       stage="active")

    status, _ = _post(
        port, "/v1/pipeline/update",
        {"id": "2026-05-12_test-active"},
        json.dumps(replacement).encode("utf-8"))
    assert status == 200

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta.get("last_updated") is not None
    assert meta["stage_counts"]["active"] == 1
    assert meta["stage_counts"]["resolved"] == 1
