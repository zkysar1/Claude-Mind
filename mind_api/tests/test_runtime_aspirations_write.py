"""POST /v1/aspirations/add-goal and /v1/aspirations/update-goal — writer
endpoint correctness, history+changelog parity, and basic concurrency.

The endpoints intentionally skip the orchestration gates (origin-signal,
duplication, capability, uncommitted-work). Tests here verify the WRITE
MACHINERY contract:
  - lock acquired
  - history snapshot created
  - atomic write
  - changelog appended
  - jsonl_cache invalidated
  - concurrent writes don't corrupt
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def _post(port: int, path: str, query: dict, body: bytes, *, agent: str = "alpha"):
    qs = urllib.parse.urlencode(query)
    url = f"http://127.0.0.1:{port}{path}?{qs}" if qs else f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


def _read_jsonl(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# add-goal
# ---------------------------------------------------------------------------

# Helper: production-realistic goal payload. Agent-sourced goals MUST cite a
# concrete origin_signal — the daemon's origin-signal gate (PR 7b) blocks
# otherwise. "user_directive" is the canonical user-initiated signal.
def _g(**kwargs) -> dict:
    base = {"title": "Test goal", "status": "pending",
            "origin_signal": "user_directive"}
    base.update(kwargs)
    return base


def test_add_goal_appends_and_allocates_id(running_daemon):
    project_root, port = running_daemon
    live = project_root / "world" / "aspirations.jsonl"

    status, body = _post(
        port, "/v1/aspirations/add-goal",
        {"asp_id": "asp-001", "source": "world"},
        json.dumps(_g()).encode("utf-8"),
        agent="alpha",
    )
    assert status == 200
    resp = json.loads(body)
    assert resp["ok"] is True
    assert resp["goal_id"] == "g-001-01"
    assert resp["aspiration_id"] == "asp-001"

    items = _read_jsonl(live)
    asp = items[0]
    assert asp["id"] == "asp-001"
    assert any(g["id"] == "g-001-01" for g in asp["goals"])


def test_add_goal_honors_caller_provided_id(running_daemon):
    project_root, port = running_daemon
    status, body = _post(
        port, "/v1/aspirations/add-goal",
        {"asp_id": "asp-001", "source": "world"},
        json.dumps(_g(id="g-001-99", title="Explicit id")).encode("utf-8"),
        agent="alpha",
    )
    assert status == 200
    assert json.loads(body)["goal_id"] == "g-001-99"


def test_add_goal_history_snapshot_created(running_daemon):
    # : history.snapshot delegates to _fileops.save_history, whose
    # Stage-2 authoritative store is the CAS-delta layout — assert a manifest
    # lands under .history/snapshots/<rel>/ instead of the legacy per-file
    # uncompressed tree (which is no longer written by default).
    project_root, port = running_daemon
    manifest_dir = (project_root / "world" / ".history" / "snapshots"
                    / "aspirations.jsonl")
    legacy_dir = project_root / "world" / ".history" / "aspirations.jsonl"
    assert not manifest_dir.exists()

    status, _ = _post(
        port, "/v1/aspirations/add-goal",
        {"asp_id": "asp-001", "source": "world"},
        json.dumps(_g()).encode("utf-8"),
        agent="alpha",
    )
    assert status == 200
    assert manifest_dir.exists()
    manifests = [p for p in manifest_dir.iterdir() if p.suffix == ".yaml"]
    assert len(manifests) == 1
    name = manifests[0].name
    assert name.endswith("_alpha.yaml")
    # The manifest carries the summary inline (no .meta sidecar in the CAS store).
    assert "add-goal" in manifests[0].read_text(encoding="utf-8")
    # And the legacy uncompressed tree is NOT re-created (the 13.9G/4days
    # growth shape this unification killed).
    assert not legacy_dir.exists()


def test_add_goal_changelog_appended(running_daemon):
    project_root, port = running_daemon
    changelog = project_root / "world" / "changelog.jsonl"

    status, _ = _post(
        port, "/v1/aspirations/add-goal",
        {"asp_id": "asp-001", "source": "world"},
        json.dumps(_g()).encode("utf-8"),
        agent="alpha",
    )
    assert status == 200
    entries = _read_jsonl(changelog)
    assert len(entries) >= 1
    last = entries[-1]
    assert last["agent"] == "alpha"
    assert last["action"] == "edit"
    assert last["file"] == "aspirations.jsonl"
    assert "add-goal" in last["summary"]


def test_add_goal_missing_aspiration_404(running_daemon):
    _, port = running_daemon
    try:
        _post(
            port, "/v1/aspirations/add-goal",
            {"asp_id": "asp-999", "source": "world"},
            json.dumps(_g()).encode("utf-8"),
            agent="alpha",
        )
    except urllib.error.HTTPError as e:
        assert e.code == 404
        err = json.loads(e.read().decode("utf-8"))
        assert err["error"] == "aspiration_not_found"
    else:
        raise AssertionError("expected 404 for missing aspiration")


def test_add_goal_missing_asp_id_400(running_daemon):
    _, port = running_daemon
    try:
        _post(
            port, "/v1/aspirations/add-goal",
            {"source": "world"},
            b"{}", agent="alpha",
        )
    except urllib.error.HTTPError as e:
        assert e.code == 400
        err = json.loads(e.read().decode("utf-8"))
        assert err["error"] == "missing_param"
    else:
        raise AssertionError("expected 400 for missing asp_id")


def test_add_goal_invalid_id_format_400(running_daemon):
    _, port = running_daemon
    # Bogus goal id should fail _validate_goal
    try:
        _post(
            port, "/v1/aspirations/add-goal",
            {"asp_id": "asp-001", "source": "world"},
            json.dumps(_g(id="not-an-id")).encode("utf-8"),
            agent="alpha",
        )
    except urllib.error.HTTPError as e:
        assert e.code == 400
        err = json.loads(e.read().decode("utf-8"))
        assert err["error"] == "validation_failed"
    else:
        raise AssertionError("expected 400 for invalid goal id")


# ---------------------------------------------------------------------------
# update-goal
# ---------------------------------------------------------------------------

def test_update_goal_sets_field(running_daemon):
    project_root, port = running_daemon
    # Seed a goal first
    _post(port, "/v1/aspirations/add-goal",
          {"asp_id": "asp-001", "source": "world"},
          json.dumps(_g(id="g-001-05")).encode("utf-8"), agent="alpha")

    status, body = _post(
        port, "/v1/aspirations/update-goal",
        {"id": "g-001-05", "field": "status", "source": "world"},
        json.dumps("in-progress").encode("utf-8"),
        agent="alpha",
    )
    assert status == 200
    resp = json.loads(body)
    assert resp["ok"] is True
    assert resp["goal_id"] == "g-001-05"
    assert resp["field"] == "status"

    live = project_root / "world" / "aspirations.jsonl"
    items = _read_jsonl(live)
    g = next(g for g in items[0]["goals"] if g["id"] == "g-001-05")
    assert g["status"] == "in-progress"


def test_update_goal_invalid_status_rejected(running_daemon):
    _, port = running_daemon
    _post(port, "/v1/aspirations/add-goal",
          {"asp_id": "asp-001", "source": "world"},
          json.dumps(_g(id="g-001-06")).encode("utf-8"), agent="alpha")

    try:
        _post(
            port, "/v1/aspirations/update-goal",
            {"id": "g-001-06", "field": "status", "source": "world"},
            json.dumps("garbage").encode("utf-8"),
            agent="alpha",
        )
    except urllib.error.HTTPError as e:
        assert e.code == 400
        err = json.loads(e.read().decode("utf-8"))
        assert err["error"] == "validation_failed"
    else:
        raise AssertionError("expected 400 for invalid status")


def test_update_goal_missing_returns_404(running_daemon):
    _, port = running_daemon
    try:
        _post(
            port, "/v1/aspirations/update-goal",
            {"id": "g-001-77", "field": "status", "source": "world"},
            json.dumps("completed").encode("utf-8"),
            agent="alpha",
        )
    except urllib.error.HTTPError as e:
        assert e.code == 404
        err = json.loads(e.read().decode("utf-8"))
        assert err["error"] == "goal_not_found"
    else:
        raise AssertionError("expected 404 for missing goal")


# ---------------------------------------------------------------------------
# Concurrency — proves file_locks.locked() serialises in-process writers
# ---------------------------------------------------------------------------

def test_concurrent_add_goal_no_corruption(running_daemon):
    """Two threads firing add-goal at the same aspiration produce a JSONL
    file with both goals present and no parse errors."""
    project_root, port = running_daemon
    live = project_root / "world" / "aspirations.jsonl"

    N = 10
    errors = []

    def _hammer(prefix: str):
        for i in range(N):
            goal = _g(title=f"{prefix}-goal-{i}")
            try:
                _post(
                    port, "/v1/aspirations/add-goal",
                    {"asp_id": "asp-001", "source": "world"},
                    json.dumps(goal).encode("utf-8"),
                    agent=prefix,
                )
            except urllib.error.HTTPError as e:
                # Decode the body — 500 responses carry the daemon's actual
                # error message (e.g. "Could not acquire lock: ..." for lock
                # timeouts). Bare HTTPError repr says only "Internal Server
                # Error" which is useless for diagnosing flakes.
                try:
                    body = e.read().decode("utf-8")
                except Exception:
                    body = "<unreadable>"
                errors.append((prefix, i, f"HTTP {e.code}: {body}"))
            except Exception as e:
                errors.append((prefix, i, repr(e)))

    t1 = threading.Thread(target=_hammer, args=("alpha",))
    t2 = threading.Thread(target=_hammer, args=("bravo",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert not errors, f"errors during concurrent add: {errors}"

    items = _read_jsonl(live)  # must parse cleanly
    asp = next(a for a in items if a["id"] == "asp-001")
    goal_ids = {g["id"] for g in asp["goals"]}
    # 2*N writes; expect at most 2*N distinct IDs (allocation is contention-safe
    # because it happens inside the lock). At minimum, we must have 2*N goals
    # if every write succeeded.
    assert len(asp["goals"]) == 2 * N, (
        f"expected {2*N} goals after concurrent writes, got {len(asp['goals'])}"
    )
    # All allocated IDs must be unique (lock-allocated -> max+1 is deterministic).
    assert len(goal_ids) == len(asp["goals"]), (
        f"duplicate goal IDs in {goal_ids}"
    )
