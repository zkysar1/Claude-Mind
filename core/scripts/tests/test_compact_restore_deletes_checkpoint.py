"""Regression test for  — script-side checkpoint deletion.

`compact-restore-slots.py::_delete_checkpoint_safely` must:
  - delete the checkpoint file when present (the success path)
  - tolerate a missing checkpoint (idempotent — missing_ok=True)
  - return True on successful or no-op deletion
  - return False (fail-quiet) on OSError, without raising

The helper is called from three clean return paths in main():
  - stale-skip   (g-115-684 freshness gate fires)
  - empty-checkpoint
  - restored-clean

It is NOT called from:
  - the 'no checkpoint' early return (nothing to delete)
  - the wm-uninitialized error exit (preserve for next-iteration retry)

Canonical incident (delta session, 2026-05-21): the previous LLM-owned
Phase -0.5c Step 3 ("Delete checkpoint") silently dropped on the
stale-skip path — observed mid-iter when compact-restore-slots.sh
correctly detected wm.yaml 464s fresher and SKIPPED restore, but the
checkpoint file lingered on disk until manual `rm -f` cleanup. The
script-side helper closes that gap by owning the lifecycle.
"""

from __future__ import annotations

import sys
import tempfile
import importlib.util
from pathlib import Path

# Make the script directory importable so we can grab the function.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_DIR = PROJECT_ROOT / "core" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

# Direct import via text-slice + exec — bypasses AGENT_DIR-dependent module
# load (assert_agent_dir would raise without MIND_AGENT set). Same pattern
# as test_compact_restore_freshness_gate.py.
source = (SCRIPT_DIR / "compact-restore-slots.py").read_text(encoding="utf-8")
start = source.find("def _delete_checkpoint_safely")
end = source.find("\ndef main(")
helper_source = source[start:end]
# Inject `sys` since the helper writes WARN to sys.stderr on OSError —
# without it the exec'd function raises NameError on the failure path.
namespace: dict = {"sys": sys}
exec(helper_source, namespace)
_delete_checkpoint_safely = namespace["_delete_checkpoint_safely"]


def test_deletes_existing_checkpoint() -> None:
    """Canonical path: checkpoint exists → delete returns True, file gone."""
    with tempfile.TemporaryDirectory() as td:
        ck = Path(td) / "compact-checkpoint.yaml"
        ck.write_text("all_slots: {}\n", encoding="utf-8")
        assert ck.exists()
        result = _delete_checkpoint_safely(ck, "test-deletes")
        assert result is True
        assert not ck.exists()


def test_missing_checkpoint_is_noop() -> None:
    """missing_ok=True: no checkpoint → return True (no-op), no exception."""
    with tempfile.TemporaryDirectory() as td:
        ck = Path(td) / "compact-checkpoint.yaml"
        # Don't create the file
        assert not ck.exists()
        result = _delete_checkpoint_safely(ck, "test-noop")
        assert result is True
        assert not ck.exists()


def test_oserror_returns_false() -> None:
    """OSError on unlink → return False (fail-quiet), no exception propagates."""
    with tempfile.TemporaryDirectory() as td:
        # Path to a NESTED file under a non-existent directory is one way to
        # provoke OSError on unlink, but missing_ok=True suppresses it.
        # Instead, use a Path-like object whose unlink raises directly.
        class FailingPath:
            def unlink(self, missing_ok: bool = False) -> None:
                raise OSError(13, "Permission denied")

        result = _delete_checkpoint_safely(FailingPath(), "test-oserror")
        assert result is False  # fail-quiet


def test_called_with_each_reason_string() -> None:
    """All three reasons exercise the same code path; smoke-check none raises.

    Reasons are stable identifiers for the call sites and surface in stderr
    on OSError. Any future call site that adds a new reason should add a
    case here so the audit trail stays complete.
    """
    for reason in ("stale-skip", "empty-checkpoint", "restored-clean"):
        with tempfile.TemporaryDirectory() as td:
            ck = Path(td) / "compact-checkpoint.yaml"
            ck.write_text("x\n", encoding="utf-8")
            assert _delete_checkpoint_safely(ck, reason) is True
            assert not ck.exists()


if __name__ == "__main__":
    test_deletes_existing_checkpoint()
    test_missing_checkpoint_is_noop()
    test_oserror_returns_false()
    test_called_with_each_reason_string()
    print("ALL PASS — g-115-962 script-side checkpoint deletion (4 cases)")
