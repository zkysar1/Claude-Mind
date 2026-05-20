"""POST /v1/pipeline/move, /v1/pipeline/add, /v1/pipeline/update-field —
writer endpoint correctness, history+changelog parity.

Mirrors test_runtime_aspirations_write.py pattern: tests verify the WRITE
MACHINERY contract (lock, history, atomic write, changelog, cache invalidate).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


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


import pytest


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
    }
    base.update(kwargs)
    return base


# Seed records that pass full validation (conftest seeds are minimal/reader-only).
_SEED_ACTIVE = json.dumps({
    "id": "2026-05-12_test-active",
    "title": "Test active hypothesis for pipeline writer tests",
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
    "title": "Test resolved hypothesis for pipeline writer tests",
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
# pipeline/add
# ---------------------------------------------------------------------------

def test_add_creates_record(pipeline_daemon):
    project_root, port = pipeline_daemon
    live = project_root / "world" / "pipeline.jsonl"
    before = len(_read_jsonl(live))

    status, body = _post(
        port, "/v1/pipeline/add", body=json.dumps(_rec()).encode("utf-8"))
    assert status == 200
    resp = json.loads(body)
    assert resp["ok"] is True
    assert resp["record_id"] == "2026-05-14_test-hyp"

    after = _read_jsonl(live)
    assert len(after) == before + 1
    assert any(r["id"] == "2026-05-14_test-hyp" for r in after)


def test_add_rejects_duplicate(pipeline_daemon):
    project_root, port = pipeline_daemon
    # First add succeeds
    _post(port, "/v1/pipeline/add", body=json.dumps(_rec()).encode("utf-8"))
    # Second add with same id fails
    status, body = _post_expect_error(
        port, "/v1/pipeline/add",
        body=json.dumps(_rec()).encode("utf-8"))
    assert status == 409
    assert "Duplicate" in body


def test_add_rejects_invalid_record(pipeline_daemon):
    _, port = pipeline_daemon
    bad = _rec()
    del bad["title"]
    status, body = _post_expect_error(
        port, "/v1/pipeline/add", body=json.dumps(bad).encode("utf-8"))
    assert status == 400
    assert "validation_failed" in body


def test_add_defaults_stage_to_discovered(pipeline_daemon):
    _, port = pipeline_daemon
    rec = _rec()
    del rec["stage"]
    status, body = _post(
        port, "/v1/pipeline/add", body=json.dumps(rec).encode("utf-8"))
    assert status == 200
    resp = json.loads(body)
    assert resp["record"]["stage"] == "discovered"


def test_add_history_snapshot_created(pipeline_daemon):
    project_root, port = pipeline_daemon
    history_dir = project_root / "world" / ".history" / "pipeline.jsonl"
    assert not history_dir.exists()

    status, _ = _post(
        port, "/v1/pipeline/add", body=json.dumps(_rec()).encode("utf-8"))
    assert status == 200
    assert history_dir.exists()


def test_add_changelog_appended(pipeline_daemon):
    project_root, port = pipeline_daemon
    cl = project_root / "world" / "changelog.jsonl"

    _post(port, "/v1/pipeline/add", body=json.dumps(_rec()).encode("utf-8"))
    assert cl.exists()
    entries = _read_jsonl(cl)
    assert any("pipeline-add" in (e.get("summary", "") or "") for e in entries)


# ---------------------------------------------------------------------------
# pipeline/move
# ---------------------------------------------------------------------------

def test_move_changes_stage(pipeline_daemon):
    project_root, port = pipeline_daemon
    live = project_root / "world" / "pipeline.jsonl"

    # The conftest seeds "2026-05-12_test-active" at stage=active.
    status, body = _post(
        port, "/v1/pipeline/move",
        {"id": "2026-05-12_test-active", "stage": "resolved"},
        json.dumps({
            "outcome": "CONFIRMED",
            "claim": "This is a sufficiently long claim field for validation",
            "rationale": "Test resolution rationale for this hypothesis",
        }).encode("utf-8"))
    assert status == 200
    resp = json.loads(body)
    assert resp["ok"] is True
    assert resp["stage"] == "resolved"
    assert resp["record"]["stage"] == "resolved"

    items = _read_jsonl(live)
    rec = next((r for r in items if r["id"] == "2026-05-12_test-active"), None)
    assert rec is not None
    assert rec["stage"] == "resolved"


def test_move_to_archived_transfers_record(pipeline_daemon):
    project_root, port = pipeline_daemon
    live = project_root / "world" / "pipeline.jsonl"
    archive = project_root / "world" / "pipeline-archive.jsonl"

    before_live = len(_read_jsonl(live))
    before_archive = len(_read_jsonl(archive))

    # Move the resolved record (seeded by conftest) to archived.
    status, body = _post(
        port, "/v1/pipeline/move",
        {"id": "2026-05-12_test-resolved", "stage": "archived"})
    assert status == 200

    after_live = _read_jsonl(live)
    after_archive = _read_jsonl(archive)
    assert len(after_live) == before_live - 1
    assert len(after_archive) == before_archive + 1
    assert not any(r["id"] == "2026-05-12_test-resolved" for r in after_live)
    assert any(r["id"] == "2026-05-12_test-resolved" for r in after_archive)


def test_move_not_found(pipeline_daemon):
    _, port = pipeline_daemon
    status, body = _post_expect_error(
        port, "/v1/pipeline/move",
        {"id": "9999-01-01_nonexistent", "stage": "resolved"})
    assert status == 404


def test_move_invalid_stage(pipeline_daemon):
    _, port = pipeline_daemon
    status, body = _post_expect_error(
        port, "/v1/pipeline/move",
        {"id": "2026-05-12_test-active", "stage": "bogus"})
    assert status == 400
    assert "invalid_stage" in body


def test_move_merges_data(pipeline_daemon):
    project_root, port = pipeline_daemon
    live = project_root / "world" / "pipeline.jsonl"

    status, body = _post(
        port, "/v1/pipeline/move",
        {"id": "2026-05-12_test-active", "stage": "discovered"},
        json.dumps({"notes": "merged note"}).encode("utf-8"))
    assert status == 200

    items = _read_jsonl(live)
    rec = next(r for r in items if r["id"] == "2026-05-12_test-active")
    assert rec["notes"] == "merged note"
    assert rec["stage"] == "discovered"


def test_move_history_and_changelog(pipeline_daemon):
    project_root, port = pipeline_daemon
    history_dir = project_root / "world" / ".history" / "pipeline.jsonl"
    cl = project_root / "world" / "changelog.jsonl"

    _post(port, "/v1/pipeline/move",
          {"id": "2026-05-12_test-active", "stage": "discovered"})

    assert history_dir.exists()
    entries = _read_jsonl(cl)
    assert any("pipeline-move" in (e.get("summary", "") or "") for e in entries)


def test_move_recomputes_meta(pipeline_daemon):
    """After move, pipeline-meta.json is recomputed (mirrors pipeline.py
    cmd_move which calls _update_meta_counts). Conftest seeds a placeholder
    meta without `last_updated`; _update_meta overwrites it with the full
    computed shape."""
    project_root, port = pipeline_daemon
    meta_path = project_root / "world" / "pipeline-meta.json"

    status, _ = _post(port, "/v1/pipeline/move",
                      {"id": "2026-05-12_test-active", "stage": "discovered"})
    assert status == 200

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta.get("last_updated") is not None
    assert meta["stage_counts"]["active"] == 0
    assert meta["stage_counts"]["discovered"] == 1
    assert meta["stage_counts"]["resolved"] == 1


# ---------------------------------------------------------------------------
# pipeline/update-field
# ---------------------------------------------------------------------------

def test_update_field_changes_value(pipeline_daemon):
    project_root, port = pipeline_daemon
    live = project_root / "world" / "pipeline.jsonl"

    status, body = _post(
        port, "/v1/pipeline/update-field",
        {"id": "2026-05-12_test-active", "field": "confidence",
         "value": "0.9"})
    assert status == 200
    resp = json.loads(body)
    assert resp["ok"] is True
    assert resp["record"]["confidence"] == 0.9

    items = _read_jsonl(live)
    rec = next(r for r in items if r["id"] == "2026-05-12_test-active")
    assert rec["confidence"] == 0.9


def test_update_field_not_found(pipeline_daemon):
    _, port = pipeline_daemon
    status, _ = _post_expect_error(
        port, "/v1/pipeline/update-field",
        {"id": "9999-01-01_nope", "field": "confidence", "value": "0.5"})
    assert status == 404


def test_update_field_rejects_dotted(pipeline_daemon):
    _, port = pipeline_daemon
    status, body = _post_expect_error(
        port, "/v1/pipeline/update-field",
        {"id": "2026-05-12_test-active", "field": "nested.key",
         "value": "foo"})
    assert status == 400
    assert "dotted_field_rejected" in body


def test_update_field_rejects_invalid_value(pipeline_daemon):
    _, port = pipeline_daemon
    # confidence must be 0.0-1.0
    status, body = _post_expect_error(
        port, "/v1/pipeline/update-field",
        {"id": "2026-05-12_test-active", "field": "confidence",
         "value": "5.0"})
    assert status == 400
    assert "validation_failed" in body


def test_update_field_reflected_sets_date(pipeline_daemon):
    project_root, port = pipeline_daemon
    live = project_root / "world" / "pipeline.jsonl"

    status, body = _post(
        port, "/v1/pipeline/update-field",
        {"id": "2026-05-12_test-active", "field": "reflected",
         "value": "true"})
    assert status == 200

    items = _read_jsonl(live)
    rec = next(r for r in items if r["id"] == "2026-05-12_test-active")
    assert rec["reflected"] is True
    assert rec.get("reflected_date") is not None


def test_update_field_history_and_changelog(pipeline_daemon):
    project_root, port = pipeline_daemon
    cl = project_root / "world" / "changelog.jsonl"

    _post(port, "/v1/pipeline/update-field",
          {"id": "2026-05-12_test-active", "field": "confidence",
           "value": "0.7"})

    entries = _read_jsonl(cl)
    assert any("pipeline-update-field" in (e.get("summary", "") or "")
               for e in entries)


def test_update_field_recomputes_meta(pipeline_daemon):
    """After update-field, pipeline-meta.json is recomputed (mirrors pipeline.py
    cmd_update_field which calls _update_meta_counts)."""
    project_root, port = pipeline_daemon
    meta_path = project_root / "world" / "pipeline-meta.json"

    status, _ = _post(port, "/v1/pipeline/update-field",
                      {"id": "2026-05-12_test-active", "field": "confidence",
                       "value": "0.9"})
    assert status == 200

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta.get("last_updated") is not None
    assert meta["stage_counts"]["active"] == 1
    assert meta["stage_counts"]["resolved"] == 1
