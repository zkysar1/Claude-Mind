"""POST /v1/pipeline/archive-sweep — endpoint correctness tests.

Mirrors pipeline.py cmd_archive_sweep: batch sweep of resolved records
whose outcome_date is older than ARCHIVE_AGE_DAYS (3). Verifies the write
machinery contract (lock, history, atomic write, changelog, cache invalidate,
meta recomputation).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
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


def _valid_rec(**kwargs) -> dict:
    """A minimal valid pipeline record."""
    base = {
        "id": "2026-05-01_sweep-test",
        "title": "Sweep test hypothesis about something interesting",
        "stage": "resolved",
        "horizon": "session",
        "type": "calibration",
        "confidence": 0.6,
        "position": "YES this is a valid multi-word position claim",
        "formed_date": "2026-05-01",
        "category": "test-cat",
        "outcome": "CONFIRMED",
        "reflected": True,
    }
    base.update(kwargs)
    return base


def _old_date() -> str:
    """A date older than ARCHIVE_AGE_DAYS (3)."""
    return (date.today() - timedelta(days=5)).isoformat()


def _recent_date() -> str:
    """A date within ARCHIVE_AGE_DAYS (3)."""
    return date.today().isoformat()


@pytest.fixture
def archive_daemon(running_daemon):
    """Seed pipeline.jsonl with records for archive-sweep testing."""
    project_root, port = running_daemon
    live = project_root / "world" / "pipeline.jsonl"
    archive = project_root / "world" / "pipeline-archive.jsonl"
    archive.write_text("", encoding="utf-8")
    return project_root, port


# ---------------------------------------------------------------------------
# Test: empty pipeline — nothing to archive
# ---------------------------------------------------------------------------

def test_archive_sweep_empty(archive_daemon):
    project_root, port = archive_daemon
    live = project_root / "world" / "pipeline.jsonl"
    live.write_text("", encoding="utf-8")

    status, body = _post(port, "/v1/pipeline/archive-sweep")
    assert status == 200
    resp = json.loads(body)
    assert resp["ok"] is True
    assert resp["archived_count"] == 0


# ---------------------------------------------------------------------------
# Test: no eligible records (resolved but outcome_date too recent)
# ---------------------------------------------------------------------------

def test_archive_sweep_nothing_eligible(archive_daemon):
    project_root, port = archive_daemon
    live = project_root / "world" / "pipeline.jsonl"
    rec = _valid_rec(outcome_date=_recent_date())
    live.write_text(json.dumps(rec) + "\n", encoding="utf-8")

    status, body = _post(port, "/v1/pipeline/archive-sweep")
    assert status == 200
    resp = json.loads(body)
    assert resp["archived_count"] == 0

    # Record still in live
    items = _read_jsonl(live)
    assert len(items) == 1
    assert items[0]["stage"] == "resolved"


# ---------------------------------------------------------------------------
# Test: single eligible record archived
# ---------------------------------------------------------------------------

def test_archive_sweep_single(archive_daemon):
    project_root, port = archive_daemon
    live = project_root / "world" / "pipeline.jsonl"
    archive = project_root / "world" / "pipeline-archive.jsonl"

    rec = _valid_rec(outcome_date=_old_date())
    live.write_text(json.dumps(rec) + "\n", encoding="utf-8")

    status, body = _post(port, "/v1/pipeline/archive-sweep")
    assert status == 200
    resp = json.loads(body)
    assert resp["archived_count"] == 1

    # 6: stays in live as a stage=archived tombstone with a prune
    # clock (the own-cloud union-by-id merge cannot express a cross-file
    # removal, so the stage flip converges fleet-wide before the prune).
    live_items = _read_jsonl(live)
    assert len(live_items) == 1
    assert live_items[0]["stage"] == "archived"
    assert live_items[0]["archived_date"] == date.today().isoformat()

    # Appended to archive
    archive_items = _read_jsonl(archive)
    assert len(archive_items) == 1
    assert archive_items[0]["id"] == "2026-05-01_sweep-test"
    assert archive_items[0]["stage"] == "archived"

    # Age the tombstone past PRUNE_GRACE_DAYS (14) and re-sweep: the live
    # copy is physically pruned; the archive copy is untouched.
    aged = dict(live_items[0])
    aged["archived_date"] = (date.today() - timedelta(days=15)).isoformat()
    live.write_text(json.dumps(aged) + "\n", encoding="utf-8")

    status, body = _post(port, "/v1/pipeline/archive-sweep")
    assert status == 200
    resp = json.loads(body)
    assert resp["archived_count"] == 0
    assert resp["pruned_count"] == 1
    assert _read_jsonl(live) == []
    archive_items = _read_jsonl(archive)
    assert len(archive_items) == 1  # still exactly one deduped copy


# ---------------------------------------------------------------------------
# Test: multiple eligible + non-eligible mix
# ---------------------------------------------------------------------------

def test_archive_sweep_mixed(archive_daemon):
    project_root, port = archive_daemon
    live = project_root / "world" / "pipeline.jsonl"
    archive = project_root / "world" / "pipeline-archive.jsonl"

    old_1 = _valid_rec(id="2026-04-01_old-one", outcome_date=_old_date())
    old_2 = _valid_rec(id="2026-04-02_old-two", outcome_date=_old_date())
    recent = _valid_rec(id="2026-05-10_recent", outcome_date=_recent_date())
    active = _valid_rec(id="2026-05-10_active", stage="active",
                        outcome=None, reflected=False)

    lines = "\n".join(json.dumps(r) for r in [old_1, old_2, recent, active])
    live.write_text(lines + "\n", encoding="utf-8")

    status, body = _post(port, "/v1/pipeline/archive-sweep")
    assert status == 200
    resp = json.loads(body)
    assert resp["archived_count"] == 2

    # 6: swept records stay in live as stage=archived tombstones;
    # ineligible records keep their stage.
    live_items = _read_jsonl(live)
    by_id = {r["id"]: r for r in live_items}
    assert by_id["2026-05-10_recent"]["stage"] == "resolved"
    assert by_id["2026-05-10_active"]["stage"] == "active"
    assert by_id["2026-04-01_old-one"]["stage"] == "archived"
    assert by_id["2026-04-02_old-two"]["stage"] == "archived"

    archive_items = _read_jsonl(archive)
    archive_ids = {r["id"] for r in archive_items}
    assert "2026-04-01_old-one" in archive_ids
    assert "2026-04-02_old-two" in archive_ids
    assert "2026-05-10_recent" not in archive_ids
    assert "2026-05-10_active" not in archive_ids


# ---------------------------------------------------------------------------
# Test: resolved record without outcome_date is skipped
# ---------------------------------------------------------------------------

def test_archive_sweep_no_outcome_date_skipped(archive_daemon):
    project_root, port = archive_daemon
    live = project_root / "world" / "pipeline.jsonl"

    rec = _valid_rec()  # no outcome_date
    live.write_text(json.dumps(rec) + "\n", encoding="utf-8")

    status, body = _post(port, "/v1/pipeline/archive-sweep")
    assert status == 200
    resp = json.loads(body)
    assert resp["archived_count"] == 0

    # Still in live
    items = _read_jsonl(live)
    assert len(items) == 1


# ---------------------------------------------------------------------------
# Test: idempotency — second sweep archives nothing
# ---------------------------------------------------------------------------

def test_archive_sweep_idempotent(archive_daemon):
    project_root, port = archive_daemon
    live = project_root / "world" / "pipeline.jsonl"

    rec = _valid_rec(outcome_date=_old_date())
    live.write_text(json.dumps(rec) + "\n", encoding="utf-8")

    # First sweep archives it
    status, body = _post(port, "/v1/pipeline/archive-sweep")
    assert status == 200
    assert json.loads(body)["archived_count"] == 1

    # Second sweep — nothing left to archive
    status, body = _post(port, "/v1/pipeline/archive-sweep")
    assert status == 200
    assert json.loads(body)["archived_count"] == 0


# ---------------------------------------------------------------------------
# Test: history snapshot created on archive
# ---------------------------------------------------------------------------

def test_archive_sweep_history_created(archive_daemon):
    project_root, port = archive_daemon
    live = project_root / "world" / "pipeline.jsonl"
    history_dir = project_root / "world" / ".history" / "snapshots" / "pipeline.jsonl"

    rec = _valid_rec(outcome_date=_old_date())
    live.write_text(json.dumps(rec) + "\n", encoding="utf-8")

    assert not history_dir.exists()
    status, _ = _post(port, "/v1/pipeline/archive-sweep")
    assert status == 200
    assert history_dir.exists()


# ---------------------------------------------------------------------------
# Test: changelog appended on archive
# ---------------------------------------------------------------------------

def test_archive_sweep_changelog(archive_daemon):
    project_root, port = archive_daemon
    live = project_root / "world" / "pipeline.jsonl"
    cl = project_root / "world" / "changelog.jsonl"

    rec = _valid_rec(outcome_date=_old_date())
    live.write_text(json.dumps(rec) + "\n", encoding="utf-8")

    _post(port, "/v1/pipeline/archive-sweep")
    assert cl.exists()
    entries = _read_jsonl(cl)
    assert any("pipeline-archive-sweep" in (e.get("summary", "") or "")
               for e in entries)


# ---------------------------------------------------------------------------
# Test: meta recomputed after archive
# ---------------------------------------------------------------------------

def test_archive_sweep_meta_updated(archive_daemon):
    project_root, port = archive_daemon
    live = project_root / "world" / "pipeline.jsonl"
    meta_path = project_root / "world" / "pipeline-meta.json"

    rec = _valid_rec(outcome_date=_old_date())
    live.write_text(json.dumps(rec) + "\n", encoding="utf-8")

    _post(port, "/v1/pipeline/archive-sweep")

    assert meta_path.exists()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["stage_counts"]["archived"] == 1
    assert meta["stage_counts"]["resolved"] == 0


# ---------------------------------------------------------------------------
# Test: malformed outcome_date does not crash sweep
# ---------------------------------------------------------------------------

def test_archive_sweep_bad_date_skipped(archive_daemon):
    project_root, port = archive_daemon
    live = project_root / "world" / "pipeline.jsonl"

    rec = _valid_rec(outcome_date="not-a-date")
    live.write_text(json.dumps(rec) + "\n", encoding="utf-8")

    status, body = _post(port, "/v1/pipeline/archive-sweep")
    assert status == 200
    resp = json.loads(body)
    assert resp["archived_count"] == 0

    # Record stays in live (not archived, not lost)
    items = _read_jsonl(live)
    assert len(items) == 1
