"""Daemon aspirations-add endpoint tests (PR 49).

Covers:
  - Happy path: add a new aspiration with goals
  - Aspiration with no goals (empty goals list)
  - Validation: missing required fields
  - Validation: invalid aspiration ID format
  - Validation: invalid status value
  - Validation: invalid priority value
  - Duplicate ID rejection (live file)
  - Archived ID reuse rejection
  - source=agent writes to agent-local file
  - Origin-signal gate blocks agent-sourced goal without signal
  - Goal-duplication gate block surfaces in response
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest


def _post(port, path, query, body=None, *, agent="alpha", headers=None):
    qs = urllib.parse.urlencode(query)
    url = f"http://127.0.0.1:{port}{path}?{qs}" if qs else f"http://127.0.0.1:{port}{path}"
    data = body if isinstance(body, bytes) else (body.encode("utf-8") if body else None)
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _read_jsonl(path: Path):
    items = []
    if not path.exists():
        return items
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            items.append(json.loads(line))
    return items


def _make_aspiration(asp_id="asp-050", n_goals=1):
    """Build a minimal valid aspiration dict."""
    goals = []
    for i in range(n_goals):
        goals.append({
            "id": f"g-{asp_id[4:]}-{i+1:02d}",
            "title": f"Goal {i+1}",
            "status": "pending",
            "origin_signal": "user_directive",
        })
    return {
        "id": asp_id,
        "title": "Test aspiration",
        "status": "active",
        "priority": "MEDIUM",
        "archived": False,
        "goals": goals,
    }


# --- Happy path -----------------------------------------------------------

def test_add_aspiration_happy_path(running_daemon):
    """Add a new aspiration with one goal — 200 with aspiration in response."""
    root, port = running_daemon
    asp = _make_aspiration("asp-050", n_goals=1)
    status, body = _post(port, "/v1/aspirations/add", {"source": "world"},
                         json.dumps(asp))
    assert status == 200, f"Expected 200 got {status}: {body}"
    resp = json.loads(body)
    assert resp["ok"] is True
    assert resp["aspiration_id"] == "asp-050"
    assert resp["source"] == "world"
    assert resp["aspiration"]["id"] == "asp-050"
    # progress should be recomputed
    assert resp["aspiration"]["progress"]["total_goals"] == 1
    assert resp["aspiration"]["progress"]["completed_goals"] == 0

    # Verify on disk
    items = _read_jsonl(root / "world" / "aspirations.jsonl")
    ids = [a["id"] for a in items]
    assert "asp-050" in ids


def test_add_aspiration_no_goals(running_daemon):
    """Aspiration with zero goals succeeds — progress shows 0/0."""
    root, port = running_daemon
    asp = _make_aspiration("asp-051", n_goals=0)
    status, body = _post(port, "/v1/aspirations/add", {"source": "world"},
                         json.dumps(asp))
    assert status == 200, f"Expected 200 got {status}: {body}"
    resp = json.loads(body)
    assert resp["aspiration"]["progress"]["total_goals"] == 0


# --- Validation errors ----------------------------------------------------

def test_add_missing_required_fields(running_daemon):
    """Aspiration missing 'title' returns 400."""
    _, port = running_daemon
    asp = {"id": "asp-052", "status": "active", "priority": "LOW",
           "archived": False, "goals": []}
    status, body = _post(port, "/v1/aspirations/add", {"source": "world"},
                         json.dumps(asp))
    assert status == 400
    resp = json.loads(body)
    assert "validation_failed" in resp.get("error", "")


def test_add_invalid_asp_id(running_daemon):
    """Invalid aspiration ID format returns 400."""
    _, port = running_daemon
    asp = _make_aspiration("bad-id", n_goals=0)
    status, body = _post(port, "/v1/aspirations/add", {"source": "world"},
                         json.dumps(asp))
    assert status == 400
    resp = json.loads(body)
    assert "validation_failed" in resp.get("error", "")


def test_add_invalid_status(running_daemon):
    """Invalid status value returns 400."""
    _, port = running_daemon
    asp = _make_aspiration("asp-053", n_goals=0)
    asp["status"] = "bogus"
    status, body = _post(port, "/v1/aspirations/add", {"source": "world"},
                         json.dumps(asp))
    assert status == 400


def test_add_invalid_priority(running_daemon):
    """Invalid priority value returns 400."""
    _, port = running_daemon
    asp = _make_aspiration("asp-054", n_goals=0)
    asp["priority"] = "ULTRA"
    status, body = _post(port, "/v1/aspirations/add", {"source": "world"},
                         json.dumps(asp))
    assert status == 400


# --- Duplicate / archived ID rejection ------------------------------------

def test_add_duplicate_id_rejected(running_daemon):
    """Adding an aspiration whose ID already exists returns 400."""
    _, port = running_daemon
    # asp-001 is seeded by conftest
    asp = _make_aspiration("asp-001", n_goals=0)
    status, body = _post(port, "/v1/aspirations/add", {"source": "world"},
                         json.dumps(asp))
    assert status == 400
    resp = json.loads(body)
    assert "duplicate_id" in resp.get("error", "")


def test_add_archived_id_rejected(running_daemon):
    """Adding an aspiration whose ID is in the archive returns 400."""
    root, port = running_daemon
    # Seed an archive entry
    archive_path = root / "world" / "aspirations-archive.jsonl"
    archive_path.write_text(
        json.dumps({"id": "asp-099", "title": "Old", "status": "retired",
                     "priority": "LOW", "archived": True, "goals": []}) + "\n",
        encoding="utf-8",
    )
    asp = _make_aspiration("asp-099", n_goals=0)
    status, body = _post(port, "/v1/aspirations/add", {"source": "world"},
                         json.dumps(asp))
    assert status == 400
    resp = json.loads(body)
    assert "archived_id_reuse" in resp.get("error", "")


# --- source=agent ---------------------------------------------------------

def test_add_source_agent(running_daemon):
    """source=agent writes to agent-local aspirations.jsonl."""
    root, port = running_daemon
    asp = _make_aspiration("asp-200", n_goals=0)
    status, body = _post(port, "/v1/aspirations/add", {"source": "agent"},
                         json.dumps(asp))
    assert status == 200, f"Expected 200 got {status}: {body}"
    resp = json.loads(body)
    assert resp["source"] == "agent"

    items = _read_jsonl(root / "agents" / "alpha" / "aspirations.jsonl")
    ids = [a["id"] for a in items]
    assert "asp-200" in ids


# --- Gate blocking --------------------------------------------------------

def test_add_origin_signal_blocks(running_daemon):
    """Agent-sourced goal without origin_signal is blocked by the gate."""
    _, port = running_daemon
    asp = _make_aspiration("asp-060", n_goals=1)
    # Remove origin_signal to trigger the gate
    asp["goals"][0].pop("origin_signal", None)
    status, body = _post(port, "/v1/aspirations/add",
                         {"source": "agent"},
                         json.dumps(asp))
    # Origin-signal gate should block (400)
    assert status == 400, f"Expected 400 got {status}: {body}"
    resp = json.loads(body)
    assert resp.get("error") == "origin_signal_blocked"


def test_add_invalid_source(running_daemon):
    """Invalid source value returns 400."""
    _, port = running_daemon
    asp = _make_aspiration("asp-061", n_goals=0)
    status, body = _post(port, "/v1/aspirations/add",
                         {"source": "invalid"},
                         json.dumps(asp))
    assert status == 400
    resp = json.loads(body)
    assert "invalid_source" in resp.get("error", "")


# --- Bulk override audit (T1.3) -------------------------------------------

def test_add_bulk_override_audits_ledger(running_daemon):
    """X-Mind-Override-All bypasses gates AND writes to override-bypass-ledger.

    Aspiration has an agent-sourced goal missing origin_signal (would block).
    With X-Mind-Override-All, gates pass and the ledger gets a record naming
    both override_signal and override_duplication as the bulk-silenced slots.
    """
    root, port = running_daemon
    asp = _make_aspiration("asp-070", n_goals=1)
    asp["goals"][0].pop("origin_signal", None)  # would block origin-signal gate
    status, body = _post(port, "/v1/aspirations/add",
                         {"source": "agent"},
                         json.dumps(asp),
                         headers={"X-Mind-Override-All": "bulk justification"})
    assert status == 200, f"Expected 200 got {status}: {body}"

    ledger = root / "world" / "override-bypass-ledger.jsonl"
    assert ledger.exists(), "audit ledger must be written"
    records = [json.loads(l) for l in
               ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    bulk_records = [r for r in records if r.get("justification") == "bulk justification"]
    assert len(bulk_records) == 1
    rec = bulk_records[0]
    assert set(rec["slots_filled"]) == {"override_signal", "override_duplication"}
    assert "origin-signal-gate" in rec["gate_ids"]
    assert "goal-duplication-gate" in rec["gate_ids"]
    assert rec["context"]["caller"] == "aspirations_write.py:add"
    assert rec["context"]["asp_id"] == "asp-070"
    assert rec["context"]["source"] == "agent"


def test_add_per_gate_override_wins_over_bulk(running_daemon):
    """Per-gate header always wins. The bulk fills only unset slots.

    Caller passes both X-Mind-Override-Signal AND X-Mind-Override-All;
    the audit must record override_duplication (bulk-filled) but NOT
    override_signal (caller set it explicitly).
    """
    root, port = running_daemon
    asp = _make_aspiration("asp-071", n_goals=1)
    asp["goals"][0].pop("origin_signal", None)
    status, _ = _post(port, "/v1/aspirations/add",
                      {"source": "agent"},
                      json.dumps(asp),
                      headers={"X-Mind-Override-Signal": "per-gate explicit",
                               "X-Mind-Override-All": "bulk fallback"})
    assert status == 200

    ledger = root / "world" / "override-bypass-ledger.jsonl"
    records = [json.loads(l) for l in
               ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    bulk_records = [r for r in records if r.get("justification") == "bulk fallback"]
    assert len(bulk_records) == 1
    rec = bulk_records[0]
    assert rec["slots_filled"] == ["override_duplication"]
    assert rec["gate_ids"] == ["goal-duplication-gate"]


def test_add_no_bulk_override_no_audit(running_daemon):
    """Successful add without X-Mind-Override-All must not write to ledger."""
    root, port = running_daemon
    asp = _make_aspiration("asp-072", n_goals=1)
    status, _ = _post(port, "/v1/aspirations/add",
                      {"source": "world"},
                      json.dumps(asp))
    assert status == 200

    ledger = root / "world" / "override-bypass-ledger.jsonl"
    # Either file doesn't exist or has no records mentioning asp-072
    if ledger.exists():
        records = [json.loads(l) for l in
                   ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
        asp_072_records = [r for r in records
                           if (r.get("context") or {}).get("asp_id") == "asp-072"]
        assert asp_072_records == []
