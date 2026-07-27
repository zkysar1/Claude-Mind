"""Tests for pending_questions_close — the cross-agent pending-question CLOSE
tool (g-353-10). Runs on the LocalBackend (conftest autouse pins
STORAGE_BACKEND=local; guard-955). Covers: bare-list + dict shapes, other-entry
preservation, idempotency, not-found, dry-run-no-write, and the derived-.lock
regression (lock must NOT be taken on the resource path itself).
"""
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pending_questions_close as pqc  # noqa: E402


def _write(p, doc):
    p.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def test_close_bare_list_preserves_others(tmp_path):
    p = tmp_path / "pq.yaml"
    _write(p, [{"id": "q1", "question": "a?", "status": "pending",
                "default_action": "did it"},
               {"id": "q2", "question": "b?", "status": "pending"}])
    res, code = pqc.close_question("x", "q1", "bravo-on-behalf-of-user",
                                   "default stood", False, str(p))
    assert code == 0
    assert res["action"] == "closed"
    assert res["verified"] is True
    assert res["pre_image"]["status"] == "pending"  # archive-before-overwrite
    got = {q["id"]: q for q in yaml.safe_load(p.read_text())}
    assert got["q1"]["status"] == "answered"
    assert got["q1"]["answered_by"] == "bravo-on-behalf-of-user"
    assert got["q1"]["answer"] == "default stood"
    assert "resolved_at" in got["q1"]
    assert got["q2"]["status"] == "pending"  # OTHER entry untouched


def test_idempotent_already_terminal(tmp_path):
    p = tmp_path / "pq.yaml"
    _write(p, [{"id": "q1", "status": "answered"}])
    res, code = pqc.close_question("x", "q1", "y", "", False, str(p))
    assert code == 0
    assert res["action"] == "already_terminal"


def test_not_found(tmp_path):
    p = tmp_path / "pq.yaml"
    _write(p, [{"id": "q1", "status": "pending"}])
    res, code = pqc.close_question("x", "nope", "y", "", False, str(p))
    assert code == 3
    assert res["action"] == "not_found"


def test_dict_shape(tmp_path):
    p = tmp_path / "pq.yaml"
    _write(p, {"questions": [{"id": "q1", "text": "a?", "status": "pending"}]})
    res, code = pqc.close_question("x", "q1", "y", "evidence", False, str(p))
    assert code == 0
    assert res["action"] == "closed"
    d = yaml.safe_load(p.read_text())
    assert d["questions"][0]["status"] == "answered"


def test_dry_run_does_not_write(tmp_path):
    p = tmp_path / "pq.yaml"
    _write(p, [{"id": "q1", "status": "pending"}])
    res, code = pqc.close_question("x", "q1", "y", "", True, str(p))
    assert code == 0
    assert res["action"] == "would_close"
    # file unchanged
    assert yaml.safe_load(p.read_text())[0]["status"] == "pending"


def test_lock_uses_derived_path_not_resource(tmp_path):
    """Regression: the lock must be on <name>.lock, never the resource file
    itself (else acquire/release create+unlink the file being protected)."""
    p = tmp_path / "pq.yaml"
    _write(p, [{"id": "q1", "status": "pending"}])
    res, code = pqc.close_question("x", "q1", "y", "r", False, str(p))
    assert code == 0 and res["action"] == "closed"
    # the resource still exists (was NOT unlinked by release_lock)
    assert p.exists()
    # no stray .yaml lock; the derived lock is <stem>.lock and is released
    assert not (tmp_path / "pq.lock").exists()


# ── If-Match ConflictError retry path () ───────────────────────────
# LocalBackend's conflict_error is () (matches nothing), so the retry is a
# transparent single pass on it. To exercise the retry + exit-6 paths we inject
# a LocalBackend-shaped fake whose write_text raises a real conflict class that
# `be.conflict_error` names, `fail_times` times before succeeding.
class _ConflictErr(Exception):
    pass


class _ConflictInjectingBackend:
    """Reads/writes the real file, but write_text raises _ConflictErr the first
    `fail_times` calls (simulating own-cloud's If-Match CAS rejecting a stale
    write). conflict_error names _ConflictErr so close_question's retry matches."""

    conflict_error = _ConflictErr

    def __init__(self, fail_times):
        self._fail = fail_times
        self.writes = 0
        self.refreshes = 0

    def acquire_lock(self, lock_path, timeout=15):
        pass

    def release_lock(self, lock_path):
        pass

    def refresh(self, path):
        self.refreshes += 1

    def read_text(self, path):
        return Path(path).read_text(encoding="utf-8")

    def write_text(self, path, content):
        self.writes += 1
        if self.writes <= self._fail:
            raise _ConflictErr("simulated If-Match rejection")
        Path(path).write_text(content, encoding="utf-8")


def test_conflict_retries_once_then_succeeds(tmp_path, monkeypatch):
    """One conflict → refresh + retry → the second write lands (exit 0)."""
    p = tmp_path / "pq.yaml"
    _write(p, [{"id": "q1", "status": "pending"}])
    fake = _ConflictInjectingBackend(fail_times=1)
    monkeypatch.setattr(pqc, "get_backend", lambda: fake)
    res, code = pqc.close_question("x", "q1", "y", "r", False, str(p))
    assert code == 0
    assert res["action"] == "closed"
    assert fake.writes == 2      # first raised, retry wrote
    assert fake.refreshes == 1   # refreshed the stale ETag before re-read
    assert yaml.safe_load(p.read_text())[0]["status"] == "answered"


def test_conflict_persists_returns_exit_6_no_clobber(tmp_path, monkeypatch):
    """Persistent conflict → exit 6 (clean conflict code, NOT a traceback), and
    the file is never clobbered (the fence rejected every stale write)."""
    p = tmp_path / "pq.yaml"
    _write(p, [{"id": "q1", "status": "pending"}])
    fake = _ConflictInjectingBackend(fail_times=99)
    monkeypatch.setattr(pqc, "get_backend", lambda: fake)
    res, code = pqc.close_question("x", "q1", "y", "r", False, str(p))
    assert code == 6
    assert res["action"] == "conflict"
    assert fake.writes == 2      # initial + one retry, then give up (bounded)
    # file untouched — no clobber, safety intact
    assert yaml.safe_load(p.read_text())[0]["status"] == "pending"
