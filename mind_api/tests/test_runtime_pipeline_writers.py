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
    history_dir = project_root / "world" / ".history" / "snapshots" / "pipeline.jsonl"
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

    # The pipeline_daemon fixture seeds "2026-05-12_test-active" at stage=active.
    status, body = _post(
        port, "/v1/pipeline/move",
        {"id": "2026-05-12_test-active", "stage": "resolved"},
        json.dumps({
            "outcome": "CONFIRMED",
            "claim": "This is a sufficiently long claim field for validation",
            "rationale": "Test resolution rationale for this hypothesis",
            # : CONFIRMED/CORRECTED moves must carry >=1 verifiable
            # evidence pointer (file:line shape satisfies the gate).
            "outcome_detail": "verified by mind_api/tests/test_runtime_pipeline_writers.py:1",
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


def test_move_to_resolved_requires_evidence(pipeline_daemon):
    """: a CONFIRMED/CORRECTED move with no evidence pointer in any
    resolution field (and no experience_ref / evidence_for / evidence_override)
    is refused, so the calibration number stays independently auditable."""
    _, port = pipeline_daemon
    status, body = _post_expect_error(
        port, "/v1/pipeline/move",
        {"id": "2026-05-12_test-active", "stage": "resolved"},
        json.dumps({
            "outcome": "CONFIRMED",
            "claim": "This is a sufficiently long claim field for validation",
            "rationale": "Test resolution rationale for this hypothesis",
        }).encode("utf-8"))
    assert status == 400
    assert "resolution_evidence_required" in body


def test_move_to_archived_tombstones_record(pipeline_daemon):
    """6 tombstone-in-live archival: the record STAYS in live as a
    stage=archived tombstone (the own-cloud union-by-id merge cannot express
    a cross-file removal), the archive gains exactly one deduped copy, and
    archive_sweep prunes the tombstone after PRUNE_GRACE_DAYS."""
    project_root, port = pipeline_daemon
    live = project_root / "world" / "pipeline.jsonl"
    archive = project_root / "world" / "pipeline-archive.jsonl"

    before_live = len(_read_jsonl(live))

    # Move the resolved record (fixture-seeded) to archived.
    status, body = _post(
        port, "/v1/pipeline/move",
        {"id": "2026-05-12_test-resolved", "stage": "archived"})
    assert status == 200

    # Live keeps the record as a stage=archived tombstone with a prune clock.
    after_live = _read_jsonl(live)
    assert len(after_live) == before_live
    tomb = next(r for r in after_live if r["id"] == "2026-05-12_test-resolved")
    assert tomb["stage"] == "archived"
    assert tomb.get("archived_date")

    def _archive_copies():
        return [r for r in _read_jsonl(archive)
                if r["id"] == "2026-05-12_test-resolved"]

    # Archive holds exactly one copy.
    assert len(_archive_copies()) == 1

    # Re-moving the tombstone is dedup-safe: still exactly one archive copy.
    status, _ = _post(
        port, "/v1/pipeline/move",
        {"id": "2026-05-12_test-resolved", "stage": "archived"})
    assert status == 200
    assert len(_archive_copies()) == 1


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
    history_dir = project_root / "world" / ".history" / "snapshots" / "pipeline.jsonl"
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


# ---------------------------------------------------------------------------
# pipeline/update (whole-record) — 5: archive-reach
# ---------------------------------------------------------------------------

def test_update_replaces_live_record(pipeline_daemon):
    """Baseline: update() replaces a LIVE record in place (whole-record path)."""
    project_root, port = pipeline_daemon
    live = project_root / "world" / "pipeline.jsonl"

    replacement = _rec(
        id="2026-05-12_test-active", stage="active", horizon="session",
        confidence=0.85,
        position="YES this is a valid multi-word replacement position",
        title="Replaced active hypothesis whole record",
        claim="Replacement whole-record claim well over twenty characters long",
    )
    status, body = _post(
        port, "/v1/pipeline/update", {"id": "2026-05-12_test-active"},
        json.dumps(replacement).encode("utf-8"))
    assert status == 200
    resp = json.loads(body)
    assert resp["ok"] is True
    assert resp["record"]["confidence"] == 0.85

    items = _read_jsonl(live)
    rec = next(r for r in items if r["id"] == "2026-05-12_test-active")
    assert rec["confidence"] == 0.85
    assert "Replaced" in rec["title"]


def test_update_reaches_archive(pipeline_daemon):
    """5: update() probes the archive when the id is absent from
    live, so a multi-field-corrupt ARCHIVED record — which rejects every
    single-field update-field repair because the whole record re-validates on
    each field write — can be repaired via this atomic whole-record path.
    Before the fix, update() was live-only and 404'd on archived ids (rb-2239)."""
    project_root, port = pipeline_daemon
    live = project_root / "world" / "pipeline.jsonl"
    archive = project_root / "world" / "pipeline-archive.jsonl"

    # Seed an archived record directly (bypasses validation, mirroring how
    # real archived records — including the corrupt ones — reach the file).
    arch_seed = _rec(
        id="2026-04-06_archived-hyp", stage="archived", horizon="session",
        title="Archived hypothesis needing whole-record repair",
        claim="Original archived claim well over twenty characters in length",
        outcome="CONFIRMED",
    )
    archive.write_text(json.dumps(arch_seed) + "\n", encoding="utf-8")

    replacement = _rec(
        id="2026-04-06_archived-hyp", stage="archived", horizon="session",
        confidence=0.8,
        position="YES this is a valid multi-word repaired position",
        title="Repaired archived hypothesis whole record",
        claim="Repaired whole-record claim well over twenty characters in length",
        outcome="CONFIRMED",
    )
    status, body = _post(
        port, "/v1/pipeline/update", {"id": "2026-04-06_archived-hyp"},
        json.dumps(replacement).encode("utf-8"))
    assert status == 200
    resp = json.loads(body)
    assert resp["ok"] is True

    # The archive file carries the repaired record; live stays untouched.
    arch_items = _read_jsonl(archive)
    rec = next(r for r in arch_items if r["id"] == "2026-04-06_archived-hyp")
    assert rec["confidence"] == 0.8
    assert "Repaired" in rec["title"]
    assert not any(r["id"] == "2026-04-06_archived-hyp"
                   for r in _read_jsonl(live))


def test_update_not_found_in_either(pipeline_daemon):
    """update() 404s when the id is in neither live nor archive."""
    _, port = pipeline_daemon
    replacement = _rec(
        id="9999-01-01_nonexistent", stage="active", horizon="session",
        position="YES this is a valid multi-word position claim",
        claim="Nonexistent record claim well over twenty characters in length",
    )
    status, body = _post_expect_error(
        port, "/v1/pipeline/update", {"id": "9999-01-01_nonexistent"},
        json.dumps(replacement).encode("utf-8"))
    assert status == 404
