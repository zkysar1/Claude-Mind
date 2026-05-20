"""POST /v1/store/{append,replace,merge} for store=journal — H2 Wave 1.

The generic store endpoint (endpoints/store.py, parameterized by
store_registry.STORE_REGISTRY['journal']) must reproduce journal.py's
cmd_add / cmd_update / cmd_merge semantics exactly, over the same daemon
write machinery (file_locks + history + changelog + cache invalidate) as
pipeline_write.py. journal.jsonl is agent-rooted: per resolve_base_dir's
G1 patch, history+changelog land under the AGENT dir, not world.

Mirrors test_runtime_pipeline_writers.py structure.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

import pytest


def _post(port: int, path: str, query: dict = None, body: bytes = b"",
          *, agent: str = "alpha"):
    qs = urllib.parse.urlencode(query) if query else ""
    url = (f"http://127.0.0.1:{port}{path}?{qs}" if qs
           else f"http://127.0.0.1:{port}{path}")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


def _post_err(port: int, path: str, query: dict = None, body: bytes = b"",
              *, agent: str = "alpha"):
    qs = urllib.parse.urlencode(query) if query else ""
    url = (f"http://127.0.0.1:{port}{path}?{qs}" if qs
           else f"http://127.0.0.1:{port}{path}")
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


def _journal(project_root: Path) -> Path:
    return project_root / "agents" / "alpha" / "journal.jsonl"


# A full valid journal record (conftest seeds are minimal — no journal_file).
def _rec(**kw) -> dict:
    base = {
        "session": 3,
        "date": "2026-05-15",
        "journal_file": "alpha/journal/2026/05/2026-05-15.md",
        "goals_completed": ["g-001-09"],
        "key_events": ["wave-1"],
        "tags": ["h2"],
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# store/append  (== journal.py cmd_add)
# ---------------------------------------------------------------------------

def test_append_creates_record(running_daemon):
    project_root, port = running_daemon
    live = _journal(project_root)
    before = len(_read_jsonl(live))  # conftest seeds sessions 1,2

    status, body = _post(port, "/v1/store/append", {"store": "journal"},
                         json.dumps(_rec()).encode("utf-8"))
    assert status == 200
    resp = json.loads(body)
    assert resp["ok"] is True
    assert resp["record"]["session"] == 3

    after = _read_jsonl(live)
    assert len(after) == before + 1
    assert any(r["session"] == 3 for r in after)


def test_append_auto_allocates_session(running_daemon):
    """No session in body -> max+1 (conftest seeds 1,2 -> 3). Mirrors
    journal.py cmd_add get_max_session+1 inside the lock."""
    _, port = running_daemon
    rec = _rec()
    del rec["session"]
    status, body = _post(port, "/v1/store/append", {"store": "journal"},
                         json.dumps(rec).encode("utf-8"))
    assert status == 200
    assert json.loads(body)["record"]["session"] == 3


def test_append_auto_fills_date(running_daemon):
    """date omitted -> today (defaults_dynamic), matching journal.py
    cmd_add `if "date" not in rec: rec["date"]=today`."""
    _, port = running_daemon
    rec = _rec()
    del rec["date"]
    rec["journal_file"] = (
        f"alpha/journal/{date.today():%Y/%m}/{date.today().isoformat()}.md")
    status, body = _post(port, "/v1/store/append", {"store": "journal"},
                         json.dumps(rec).encode("utf-8"))
    assert status == 200
    assert json.loads(body)["record"]["date"] == date.today().isoformat()


def test_append_rejects_duplicate_session(running_daemon):
    _, port = running_daemon
    status, body = _post_err(port, "/v1/store/append", {"store": "journal"},
                             json.dumps(_rec(session=1)).encode("utf-8"))
    assert status == 409
    assert "Duplicate" in body


def test_append_rejects_missing_journal_file(running_daemon):
    _, port = running_daemon
    bad = _rec()
    del bad["journal_file"]
    status, body = _post_err(port, "/v1/store/append", {"store": "journal"},
                             json.dumps(bad).encode("utf-8"))
    assert status == 400
    assert "validation_failed" in body


def test_append_rejects_bad_journal_file_pattern(running_daemon):
    _, port = running_daemon
    status, body = _post_err(
        port, "/v1/store/append", {"store": "journal"},
        json.dumps(_rec(journal_file="alpha/notes/x.md")).encode("utf-8"))
    assert status == 400
    assert "validation_failed" in body


def test_append_unknown_store(running_daemon):
    _, port = running_daemon
    status, body = _post_err(port, "/v1/store/append", {"store": "nope"},
                             json.dumps(_rec()).encode("utf-8"))
    assert status == 400
    assert "unknown_store" in body


def test_append_missing_store_param(running_daemon):
    _, port = running_daemon
    status, body = _post_err(port, "/v1/store/append", None,
                             json.dumps(_rec()).encode("utf-8"))
    assert status == 400
    assert "missing_param" in body


def test_append_history_and_changelog(running_daemon):
    """journal.jsonl is agent-rooted; resolve_base_dir G1 patch routes
    history+changelog under the AGENT dir."""
    project_root, port = running_daemon
    hist = project_root / "agents" / "alpha" / ".history" / "journal.jsonl"
    cl = project_root / "agents" / "alpha" / "changelog.jsonl"
    assert not hist.exists()

    _post(port, "/v1/store/append", {"store": "journal"},
          json.dumps(_rec()).encode("utf-8"))

    assert hist.exists()
    entries = _read_jsonl(cl)
    assert any("store-append journal" in (e.get("summary", "") or "")
               for e in entries)


# ---------------------------------------------------------------------------
# store/replace  (== journal.py cmd_update)
# ---------------------------------------------------------------------------

def test_replace_full_record(running_daemon):
    project_root, port = running_daemon
    live = _journal(project_root)

    new = _rec(session=2, tags=["replaced"], goals_completed=["g-XXX"])
    status, body = _post(port, "/v1/store/replace",
                         {"store": "journal", "id": "2"},
                         json.dumps(new).encode("utf-8"))
    assert status == 200
    assert json.loads(body)["record"]["tags"] == ["replaced"]

    rec = next(r for r in _read_jsonl(live) if r["session"] == 2)
    assert rec["tags"] == ["replaced"]
    assert rec["goals_completed"] == ["g-XXX"]


def test_replace_accepts_session_dash_form(running_daemon):
    """journal.py accepts 'session-N' or bare 'N' — id_coerce handles both."""
    project_root, port = running_daemon
    new = _rec(session=2, tags=["dash"])
    status, _ = _post(port, "/v1/store/replace",
                      {"store": "journal", "id": "session-2"},
                      json.dumps(new).encode("utf-8"))
    assert status == 200
    rec = next(r for r in _read_jsonl(_journal(project_root))
               if r["session"] == 2)
    assert rec["tags"] == ["dash"]


def test_replace_id_mismatch(running_daemon):
    _, port = running_daemon
    status, body = _post_err(port, "/v1/store/replace",
                             {"store": "journal", "id": "2"},
                             json.dumps(_rec(session=5)).encode("utf-8"))
    assert status == 400
    assert "id_mismatch" in body


def test_replace_not_found(running_daemon):
    _, port = running_daemon
    status, body = _post_err(port, "/v1/store/replace",
                             {"store": "journal", "id": "99"},
                             json.dumps(_rec(session=99)).encode("utf-8"))
    assert status == 404


def test_replace_validates(running_daemon):
    _, port = running_daemon
    bad = _rec(session=2)
    del bad["journal_file"]
    status, body = _post_err(port, "/v1/store/replace",
                             {"store": "journal", "id": "2"},
                             json.dumps(bad).encode("utf-8"))
    assert status == 400
    assert "validation_failed" in body


# ---------------------------------------------------------------------------
# store/merge  (== journal.py cmd_merge — union/append/scalar, NO re-validate)
# ---------------------------------------------------------------------------

def test_merge_union_and_append_and_scalar(running_daemon):
    project_root, port = running_daemon
    live = _journal(project_root)
    # conftest session 2: goals_completed=[,], key_events=[],
    # tags=[routine].
    patch = {
        "goals_completed": ["", "g-NEW"],  # union: dedup 
        "key_events": ["e1", "e2"],                 # append
        "tags": ["routine", "extra"],               # union: dedup routine
        "hypotheses_resolved": 4,                    # scalar overwrite
    }
    status, body = _post(port, "/v1/store/merge",
                         {"store": "journal", "id": "2"},
                         json.dumps(patch).encode("utf-8"))
    assert status == 200
    rec = json.loads(body)["record"]
    assert rec["goals_completed"] == ["g-001-02", "g-001-03", "g-NEW"]
    assert rec["key_events"] == ["e1", "e2"]
    assert rec["tags"] == ["routine", "extra"]
    assert rec["hypotheses_resolved"] == 4

    on_disk = next(r for r in _read_jsonl(live) if r["session"] == 2)
    assert on_disk["goals_completed"] == ["g-001-02", "g-001-03", "g-NEW"]


def test_merge_not_found(running_daemon):
    _, port = running_daemon
    status, _ = _post_err(port, "/v1/store/merge",
                          {"store": "journal", "id": "99"},
                          json.dumps({"tags": ["x"]}).encode("utf-8"))
    assert status == 404


def test_merge_history_and_changelog(running_daemon):
    project_root, port = running_daemon
    cl = project_root / "agents" / "alpha" / "changelog.jsonl"
    _post(port, "/v1/store/merge", {"store": "journal", "id": "2"},
          json.dumps({"tags": ["m"]}).encode("utf-8"))
    entries = _read_jsonl(cl)
    assert any("store-merge journal 2" in (e.get("summary", "") or "")
               for e in entries)
