""" regression: ``append_changelog`` must be BEST-EFFORT.

Incident (measured 2026-08-21, two boxes within an hour — g-115-7136 foxtrot,
g-115-7140 echo, plus guard-905's 2026-08-06 alpha trace): a transient
``TimeoutError: Could not acquire lock: <world>/changelog.lock`` propagated out
of ``append_changelog`` and ``recurring-close.sh`` reported
"update consecutive_deep failed" for a field that WAS on disk.

The propagation is a FALSE NEGATIVE by construction: every caller invokes
``append_changelog`` AFTER its durable write (``aspirations.py``
``_write_live_under_lock`` makes the append its LAST line; the 7
``locked_*_jsonl`` sites do the same), so a failure here can only lose one
AUDIT ROW — it can never mean the user-visible write failed. The
``consecutive_routine`` branch of ``recurring-close.sh`` does ``sys.exit(1)``
on that error, so a lost audit row could abort a whole recurring close.

The daemon twin ``mind_api/src/changelog.py::append`` has always swallowed
(``except OSError: pass``) and its docstring claimed verbatim to mirror this
function — a parity claim these tests make true (guard-742 class).

Swallowed is NOT silent (guard-1893): each drop is counted in
``_fileops._CHANGELOG_DROPS`` and announced on stderr with the running total.
The tests below assert BOTH halves — that the exception is absorbed AND that
the evidence survives.
"""
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]  # core/scripts
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _fileops  # noqa: E402
from _write_queue import WriteQueueTimeoutError  # noqa: E402
from storage_backend import LocalBackend  # noqa: E402


@pytest.fixture
def world(tmp_path, monkeypatch):
    # guard-652: a fixture pinning WORLD_DIR to a temp dir MUST also pin
    # META_DIR — resolve_base_dir walks WORLD > META > AGENT and a stale real
    # META_DIR makes base-dir resolution nondeterministic.
    w = tmp_path / "world"
    m = tmp_path / "meta"
    w.mkdir()
    m.mkdir()
    monkeypatch.setattr(_fileops, "WORLD_DIR", w)
    monkeypatch.setattr(_fileops, "META_DIR", m)
    # LocalBackend keeps the test hermetic even when a live own-cloud daemon is
    # present on this box (guard-955 / rb-2983 class).
    monkeypatch.setattr(_fileops, "get_backend", lambda: LocalBackend())
    # The tally is a module global with process lifetime — reset it so each
    # test asserts its OWN drops rather than inheriting a sibling's.
    monkeypatch.setattr(_fileops, "_CHANGELOG_DROPS", {"count": 0, "first": None})
    return w


def _target(world_dir):
    """A normal (non-changelog) file, so the  self-skip does not fire."""
    return str(world_dir / "reasoning-bank.jsonl")


# --------------------------------------------------------------------------
# The three best-effort arms
# --------------------------------------------------------------------------

def test_lock_timeout_is_swallowed_and_counted(world, capsys):
    """THE INCIDENT: acquire_lock raises the contention TimeoutError."""
    boom = TimeoutError(f"Could not acquire lock: {world}/changelog.lock")

    def _refuse(lock_path, **kwargs):
        raise boom

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_fileops, "acquire_lock", _refuse)
        # Must NOT raise — this is the whole point of the change.
        _fileops.append_changelog(str(world), "zeta", _target(world), "edit")

    assert _fileops._CHANGELOG_DROPS["count"] == 1
    assert "Could not acquire lock" in _fileops._CHANGELOG_DROPS["first"]
    assert _fileops._CHANGELOG_DROPS["first"].startswith("TimeoutError: ")

    # guard-1893: the swallow must ANNOUNCE. A quiet counter would reproduce
    # the invisible-failure shape this change exists to remove.
    err = capsys.readouterr().err
    assert "changelog row DROPPED at lock" in err
    assert "drops this process: 1" in err

    # No row landed, and no partially-written file was left behind.
    assert not (world / "changelog.jsonl").exists()


def test_append_failure_is_swallowed_and_lock_still_released(world, capsys):
    """An OSError from the append itself (disk-full, permission, wrong inode
    type) is swallowed, and the finally arm still releases the lock — a leaked
    lock would wedge every later write on this path.

    The failure is induced by making ``changelog.jsonl`` a DIRECTORY, so the
    real ``open(..., 'a')`` raises ``IsADirectoryError`` (an OSError) from the
    genuine code path. Patching ``builtins.open`` would also intercept pytest's
    own machinery and acquire_lock's backend — a synthetic probe measuring
    something production never sees (probe-with-canonical-code-path.md)."""
    released = []
    real_release = _fileops.release_lock

    def _spy_release(lock_path):
        released.append(lock_path)
        return real_release(lock_path)

    (world / "changelog.jsonl").mkdir()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_fileops, "release_lock", _spy_release)
        _fileops.append_changelog(str(world), "zeta", _target(world), "edit")

    assert _fileops._CHANGELOG_DROPS["count"] == 1
    assert _fileops._CHANGELOG_DROPS["first"].startswith(("IsADirectoryError: ", "PermissionError: "))
    assert len(released) == 1, "lock was not released after an append failure"
    assert not (world / "changelog.lock").exists()
    assert "changelog row DROPPED at append" in capsys.readouterr().err


def test_release_failure_is_swallowed(world, capsys):
    """A release failure must not resurrect the propagation the other two arms
    just stopped — the lock's stale-break recovers an unreleased lock, so there
    is nothing a caller could do with the exception except mis-report again."""
    def _boom(lock_path):
        raise OSError(13, "Permission denied")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_fileops, "release_lock", _boom)
        _fileops.append_changelog(str(world), "zeta", _target(world), "edit")

    # The append itself SUCCEEDED — only the release failed.
    entry = json.loads((world / "changelog.jsonl").read_text().strip().splitlines()[-1])
    assert entry["file"] == "reasoning-bank.jsonl"
    assert _fileops._CHANGELOG_DROPS["count"] == 1
    assert "changelog row DROPPED at release" in capsys.readouterr().err


# --------------------------------------------------------------------------
# Blast radius: what must STILL propagate
# --------------------------------------------------------------------------

def test_write_queue_backpressure_still_propagates(world):
    """``except OSError`` was chosen so backpressure stays loud.
    ``WriteQueueBackpressure`` subclasses RuntimeError precisely to stay
    "distinct from the raw lock TimeoutError by design" (_write_queue.py:68) —
    a shed-load refusal is a real condition, not an audit-row blip."""
    def _refuse(lock_path, **kwargs):
        raise WriteQueueTimeoutError("queue wait bound exceeded")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_fileops, "acquire_lock", _refuse)
        with pytest.raises(WriteQueueTimeoutError):
            _fileops.append_changelog(str(world), "zeta", _target(world), "edit")

    assert _fileops._CHANGELOG_DROPS["count"] == 0, (
        "backpressure was miscounted as a dropped audit row"
    )


def test_timeout_error_is_an_oserror_subclass():
    """Positive control for the `except OSError` arm. The whole fix rests on
    this being true; assert it rather than trusting the MRO from memory."""
    assert issubclass(TimeoutError, OSError)
    assert not issubclass(WriteQueueTimeoutError, OSError)


# --------------------------------------------------------------------------
# Happy path unchanged
# --------------------------------------------------------------------------

def test_happy_path_writes_and_counts_no_drops(world):
    _fileops.append_changelog(str(world), "zeta", _target(world), "edit", summary="s", lines_changed=3)
    entry = json.loads((world / "changelog.jsonl").read_text().strip().splitlines()[-1])
    assert entry["file"] == "reasoning-bank.jsonl"
    assert entry["action"] == "edit"
    assert entry["summary"] == "s"
    assert entry["lines_changed"] == 3
    assert _fileops._CHANGELOG_DROPS["count"] == 0
    assert _fileops._CHANGELOG_DROPS["first"] is None
