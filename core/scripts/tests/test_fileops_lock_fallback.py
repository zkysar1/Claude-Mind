"""Regression guard for the fresh-box own-cloud bare-subprocess lock break.

g-334-03 / rb-2764. On an own-cloud box ``STORAGE_BACKEND=own-cloud`` is ambient
in every subprocess, but ``WORLD_PATH``/``META_PATH`` are exported only by
*sourcing* ``_paths.sh``. A bare ``py -3 core/scripts/X.py`` subprocess therefore
selects the own-cloud backend, yet ``OwnCloudBackend.from_env()`` cannot resolve a
governed root and raises. Previously ``_fileops.acquire_lock`` propagated that
raise and the caller skipped its whole read-modify-write (e.g.
``loop-state-bump-counters --reset-alignment`` / ``--evolution-fired`` silently
no-op'd their counter writes on every own-cloud box).

``_fileops._lock_backend()`` now falls back to a LOCAL file lock for exactly this
condition (own-cloud requested AND no governed root resolvable), which is always
correct — without the root env no governed path is constructible, so the lock can
only be for a local path. Sourced wrappers (env present) keep the DDB lock, and a
``from_env`` failure with the governed root PRESENT (missing bucket/table/creds)
is re-raised, not masked.

These tests reproduce the exact env condition of the break — the fresh-box smoke
check so the next new own-cloud box cannot regress this class silently.

File basename starts with ``test_`` so domain-leak-check.sh skips it (the
own-cloud env tokens here are test infrastructure, not a domain leak).
"""
import os
import sys
from pathlib import Path

import pytest

# core/scripts on sys.path so `import _fileops` resolves (mirrors the import
# convention used by the sibling test modules in this directory).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _fileops  # noqa: E402
from storage_backend import reset_backend_for_tests, LocalBackend  # noqa: E402

_GOVERNED_ROOT_VARS = ("MIND_WORLD", "WORLD_PATH", "MIND_META", "META_PATH")


@pytest.fixture
def clean_backend():
    """Reset the process-wide backend singleton before and after each test.

    The conftest autouse env-restore pins STORAGE_BACKEND back to local but does
    NOT reset the cached ``_ACTIVE_BACKEND`` — so the test must clear it itself,
    both to force a fresh selection under the monkeypatched env and to avoid
    leaking a fallback LocalBackend into later tests.
    """
    reset_backend_for_tests()
    # Also reset the once-per-process warn flag so the observability note is
    # deterministic across tests that exercise the fallback path.
    _fileops._LOCK_FALLBACK_WARNED = False
    yield
    reset_backend_for_tests()
    _fileops._LOCK_FALLBACK_WARNED = False


def _force_own_cloud_no_governed_root(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    for v in _GOVERNED_ROOT_VARS:
        monkeypatch.delenv(v, raising=False)


def test_own_cloud_bare_subprocess_acquire_lock_falls_back_to_local(
        tmp_path, monkeypatch, clean_backend):
    """The reproduction: own-cloud selected + governed root absent -> acquire_lock
    creates a LOCAL lock file instead of raising (previously from_env raised)."""
    _force_own_cloud_no_governed_root(monkeypatch)
    lock_path = tmp_path / "working-memory.yaml.lock"

    # Must NOT raise. Previously OwnCloudBackend.from_env raised here and the
    # caller's read-modify-write was skipped.
    _fileops.acquire_lock(lock_path, stale_seconds=10)
    assert lock_path.exists(), "local lock file should have been created"

    # Release is symmetric (same fallback) and removes the file.
    _fileops.release_lock(lock_path)
    assert not lock_path.exists(), "release_lock should remove the local lock"


def test_lock_backend_helper_returns_local_when_governed_root_absent(
        monkeypatch, clean_backend):
    """_lock_backend() picks LocalBackend under the fresh-box condition."""
    _force_own_cloud_no_governed_root(monkeypatch)
    assert isinstance(_fileops._lock_backend(), LocalBackend)


def test_fallback_warns_once_per_process(monkeypatch, clean_backend, capsys):
    """The observability note fires exactly once per process, not per lock."""
    _force_own_cloud_no_governed_root(monkeypatch)
    _fileops._lock_backend()
    first = capsys.readouterr().err
    _fileops._lock_backend()
    second = capsys.readouterr().err
    assert "own-cloud" in first and "g-334-03" in first
    assert second == "", "fallback note must not repeat within the same process"


def test_env_present_but_from_env_fails_re_raises_not_masked(
        monkeypatch, clean_backend):
    """Negative guard: governed root PRESENT but from_env fails for another
    reason (missing bucket/table) -> the helper RE-RAISES rather than masking a
    real misconfiguration with a local lock."""
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    monkeypatch.setenv("WORLD_PATH", "/tmp/some-world-root")  # governed root present
    # Force from_env to fail on a missing required own-cloud table var.
    for v in ("STORAGE_S3_BUCKET", "STORAGE_DDB_LOCK_TABLE",
              "STORAGE_DDB_SESSIONS_TABLE"):
        monkeypatch.delenv(v, raising=False)
    with pytest.raises(RuntimeError):
        _fileops._lock_backend()


def test_local_backend_default_path_unaffected(tmp_path, monkeypatch, clean_backend):
    """The common local-box path (STORAGE_BACKEND=local) is untouched — no
    fallback branch, plain local file lock."""
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    lock_path = tmp_path / "x.lock"
    _fileops.acquire_lock(lock_path)
    assert lock_path.exists()
    _fileops.release_lock(lock_path)
    assert not lock_path.exists()
