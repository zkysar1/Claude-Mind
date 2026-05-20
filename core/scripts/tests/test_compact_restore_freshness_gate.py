"""Regression test for  freshness gate.

`compact-restore-slots.py::_is_checkpoint_stale` must return True when
wm.yaml mtime exceeds checkpoint mtime (the regression-risk case where
restore would silently drop newer state). Returns False on equal mtimes,
missing files, and stat errors (fail-open).

Canonical incident (zeta session 28 iter 6, 2026-05-13): checkpoint at
03:08:38 (PreCompact), wm.yaml at 03:21:55 with counted_goals carrying
g-115-666 that checkpoint's loop_state did not. Restore would have
dropped g-115-666 from the counter, double-counting on next
learning-gate.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Make the script directory importable so we can grab the function.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_DIR = PROJECT_ROOT / "core" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

# Direct import — the function is module-level + side-effect-free aside
# from filesystem stat calls. We don't need _paths.py initialization since
# the function takes paths as args.
import importlib.util
spec = importlib.util.spec_from_file_location(
    "compact_restore_slots_mod",
    str(SCRIPT_DIR / "compact-restore-slots.py"),
)
mod = importlib.util.module_from_spec(spec)
# Bypass AGENT_DIR-dependent imports at module load time: stash the function
# we need by reading the source and exec'ing only the helper. Safe because
# _is_checkpoint_stale is fully self-contained (no module globals besides args).
source = (SCRIPT_DIR / "compact-restore-slots.py").read_text(encoding="utf-8")
# Extract just the helper function via simple text slice — keep test
# robust to module-level import side effects.
start = source.find("def _is_checkpoint_stale")
end = source.find("\ndef main(")
helper_source = source[start:end]
namespace: dict = {}
exec(helper_source, namespace)
_is_checkpoint_stale = namespace["_is_checkpoint_stale"]


def _touch(p: Path, mtime: float) -> None:
    p.write_text("x", encoding="utf-8")
    os.utime(p, (mtime, mtime))


def test_wm_newer_than_checkpoint_is_stale() -> None:
    """Canonical incident: wm.yaml mtime > checkpoint mtime → stale (skip restore)."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        ck = td_path / "compact-checkpoint.yaml"
        wm = td_path / "working-memory.yaml"
        _touch(ck, 1700000000.0)  # 2023-11-14 22:13:20
        _touch(wm, 1700000900.0)  # 15 min later
        assert _is_checkpoint_stale(ck, wm) is True


def test_checkpoint_newer_than_wm_is_fresh() -> None:
    """Normal autocompact flow: checkpoint mtime > wm.yaml mtime → fresh (restore proceeds)."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        ck = td_path / "compact-checkpoint.yaml"
        wm = td_path / "working-memory.yaml"
        _touch(wm, 1700000000.0)
        _touch(ck, 1700000900.0)
        assert _is_checkpoint_stale(ck, wm) is False


def test_equal_mtimes_treated_as_fresh() -> None:
    """Edge case: identical mtimes → False (treat as fresh; tied checkpoint wins)."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        ck = td_path / "compact-checkpoint.yaml"
        wm = td_path / "working-memory.yaml"
        _touch(ck, 1700000000.0)
        _touch(wm, 1700000000.0)
        assert _is_checkpoint_stale(ck, wm) is False


def test_missing_checkpoint_falls_open() -> None:
    """Missing checkpoint file → False (fail-open; caller handles via existing 'no checkpoint' path)."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        ck = td_path / "compact-checkpoint.yaml"
        wm = td_path / "working-memory.yaml"
        _touch(wm, 1700000000.0)
        # Don't create checkpoint
        assert _is_checkpoint_stale(ck, wm) is False


def test_missing_wm_falls_open() -> None:
    """Missing wm.yaml → False (fail-open; main() handles wm absence separately)."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        ck = td_path / "compact-checkpoint.yaml"
        wm = td_path / "working-memory.yaml"
        _touch(ck, 1700000000.0)
        # Don't create wm
        assert _is_checkpoint_stale(ck, wm) is False


def test_none_paths_fall_open() -> None:
    """None args (e.g. AGENT_DIR unset) → False (fail-open at top of function)."""
    assert _is_checkpoint_stale(None, None) is False
    with tempfile.TemporaryDirectory() as td:
        wm = Path(td) / "working-memory.yaml"
        _touch(wm, 1700000000.0)
        assert _is_checkpoint_stale(None, wm) is False


if __name__ == "__main__":
    test_wm_newer_than_checkpoint_is_stale()
    test_checkpoint_newer_than_wm_is_fresh()
    test_equal_mtimes_treated_as_fresh()
    test_missing_checkpoint_falls_open()
    test_missing_wm_falls_open()
    test_none_paths_fall_open()
    print("ALL PASS — g-115-684 freshness gate locked (6 cases: stale/fresh/equal + 3 fail-open)")
