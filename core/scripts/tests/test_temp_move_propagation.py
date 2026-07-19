"""2 part 2 — sweep temp/ move-propagation unit tests.

propagate_temp_moves deletes the remote ROOT key agents/<a>/temp/<name> only
when the LOCAL drained/<name> twin exists (the drained copy IS the archive —
archive-before-delete). These tests pin the safety predicates with a fake
backend; no daemon, no S3.
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_sync():
    spec = importlib.util.spec_from_file_location(
        "owncloud_sync_under_test",
        REPO_ROOT / "core" / "scripts" / "owncloud_sync.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


SYNC = _load_sync()


class FakeBackend:
    """Remote store = {posix-rel-under-agents-root: last_modified_epoch}."""

    def __init__(self, agents_root: Path, remote: dict):
        self._roots = [(str(agents_root), "agents")]
        self._agents_root = agents_root
        self.remote = dict(remote)
        self.deleted = []

    def _rel(self, path: Path) -> str:
        return Path(path).relative_to(self._agents_root).as_posix()

    def list_dir(self, path: Path):
        prefix = self._rel(path) + "/"
        names = set()
        for k in self.remote:
            if k.startswith(prefix):
                names.add(k[len(prefix):].split("/")[0])
        return sorted(names)

    def head_last_modified(self, path: Path):
        return self.remote.get(self._rel(path))

    def delete_object(self, path: Path) -> bool:
        rel = self._rel(path)
        if rel not in self.remote:
            return False
        del self.remote[rel]
        self.deleted.append(rel)
        return True


class NoDeleteBackend:
    """Backend WITHOUT delete_object (LocalBackend shape) — must no-op."""

    def __init__(self, agents_root: Path):
        self._roots = [(str(agents_root), "agents")]

    def list_dir(self, path: Path):  # pragma: no cover — never reached
        raise AssertionError("list_dir must not be called on no-op path")


def _mk_agent(tmp_path: Path, agent: str, drained_names=()):
    d = tmp_path / agent / "temp" / "drained"
    d.mkdir(parents=True)
    old = time.time() - 3600
    for name in drained_names:
        p = d / name
        p.write_text("drained body", encoding="utf-8")
        # drain-floor an hour in the past so "remote newer" cases can be
        # constructed above it and normal cases below it.
        import os
        os.utime(p, (old, old))
    return tmp_path / agent


def test_deletes_root_twin_of_drained_file(tmp_path):
    _mk_agent(tmp_path, "alpha", ["doc-a.md"])
    be = FakeBackend(tmp_path, {"alpha/temp/doc-a.md": time.time() - 7200})
    stats = SYNC.propagate_temp_moves(be)
    assert stats["deleted"] == 1
    assert be.deleted == ["alpha/temp/doc-a.md"]
    assert stats["deleted_keys"] == ["agents/alpha/temp/doc-a.md"]


def test_live_root_file_without_drained_twin_never_deleted(tmp_path):
    _mk_agent(tmp_path, "alpha", ["doc-a.md"])
    be = FakeBackend(tmp_path, {
        "alpha/temp/doc-a.md": time.time() - 7200,
        "alpha/temp/live-doc.md": time.time() - 7200,   # genuine live doc
    })
    stats = SYNC.propagate_temp_moves(be)
    assert "alpha/temp/live-doc.md" in be.remote          # untouched
    assert be.deleted == ["alpha/temp/doc-a.md"]
    assert stats["deleted"] == 1


def test_remote_newer_than_drain_floor_skipped(tmp_path):
    _mk_agent(tmp_path, "alpha", ["doc-a.md"])
    be = FakeBackend(tmp_path, {"alpha/temp/doc-a.md": time.time() + 600})
    stats = SYNC.propagate_temp_moves(be)
    assert stats["skipped_remote_newer"] == 1
    assert stats["deleted"] == 0
    assert "alpha/temp/doc-a.md" in be.remote


def test_dry_run_deletes_nothing(tmp_path):
    _mk_agent(tmp_path, "alpha", ["doc-a.md"])
    be = FakeBackend(tmp_path, {"alpha/temp/doc-a.md": time.time() - 7200})
    stats = SYNC.propagate_temp_moves(be, dry_run=True)
    assert stats["would_delete"] == 1
    assert stats["deleted"] == 0
    assert "alpha/temp/doc-a.md" in be.remote


def test_ephemera_and_subdirs_ignored(tmp_path):
    _mk_agent(tmp_path, "alpha", ["doc-a.md", "notes.py"])
    be = FakeBackend(tmp_path, {
        "alpha/temp/notes.py": time.time() - 7200,        # non-md/json ephemera
        "alpha/temp/drained/doc-a.md": time.time() - 7200,  # subdir content
    })
    stats = SYNC.propagate_temp_moves(be)
    assert stats["deleted"] == 0
    assert stats["candidates"] == 0
    assert set(be.remote) == {"alpha/temp/notes.py",
                              "alpha/temp/drained/doc-a.md"}


def test_backend_without_delete_object_noops(tmp_path):
    _mk_agent(tmp_path, "alpha", ["doc-a.md"])
    be = NoDeleteBackend(tmp_path)
    stats = SYNC.propagate_temp_moves(be)
    assert stats == {"agents_checked": 0, "candidates": 0, "deleted": 0,
                     "would_delete": 0, "skipped_remote_newer": 0,
                     "errors": 0, "deleted_keys": []}


def test_owned_set_excludes_unowned_agent(tmp_path):
    _mk_agent(tmp_path, "alpha", ["doc-a.md"])
    _mk_agent(tmp_path, "bravo", ["doc-b.md"])
    be = FakeBackend(tmp_path, {
        "alpha/temp/doc-a.md": time.time() - 7200,
        "bravo/temp/doc-b.md": time.time() - 7200,
    })
    stats = SYNC.propagate_temp_moves(be, owned={"bravo"})
    assert be.deleted == ["bravo/temp/doc-b.md"]
    assert stats["deleted"] == 1
    assert "alpha/temp/doc-a.md" in be.remote
