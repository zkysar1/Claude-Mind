"""test_seed_swap_fail_loud.py —  regression.

Bug shape: seed-transplant.sh L239 captured `_seed_engine.py swap` STDOUT only,
under the top-of-file `set -e`. When the swap engine exited non-zero AFTER the
moves landed (a post-move cleanup step raising, esp. on Windows), its traceback
went to STDERR (dropped by the stdout-only $(...) capture) and `set -e` killed
the wrapper SILENTLY before [moved: N] and before --commit — a scary mid-swap
death on a tree that was actually fully swapped, with NO diagnostic (v2.1.1 prod
promotion 2026-07-05).

Two-part fix:
  (wrapper) mirror the build-plan defensive pattern — capture stderr, `set +e`
    around the substitution, check rc, fail LOUD with the engine stderr, and
    distinguish engine-crash-after-swap (exit 8) from per-file swap failures
    (exit 6). [not exercised here — bash-level]
  (engine, THIS test) do_swap's post-move staging removal no longer silently
    swallows errors via ignore_errors=True: a cleanup failure is REPORTED as a
    named `staging_cleanup_error` field, the moves still count as succeeded, and
    do_swap does NOT raise.

Run: py -3 -m pytest core/scripts/tests/test_seed_swap_fail_loud.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

ENGINE_PATH = CORE_SCRIPTS / "_seed_engine.py"
_spec = importlib.util.spec_from_file_location("_seed_engine_swap_t", ENGINE_PATH)
_engine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_engine)


def _mk_staging(dest: Path, files: dict) -> Path:
    """Create a .seed-staging dir under dest with the given {relpath: content}."""
    staging = dest / _engine.STAGING_DIRNAME
    for rel, content in files.items():
        p = staging / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return staging


def test_do_swap_happy_path_removes_staging(tmp_path):
    """Baseline: all files move, staging removed, no cleanup error reported."""
    dest = tmp_path / "dest"
    dest.mkdir()
    _mk_staging(dest, {"core/a.py": "A\n", "world/n.md": "# n\n"})

    result = _engine.do_swap(dest)

    assert result["moved"] == 2, result
    assert result["failures"] == []
    assert "staging_cleanup_error" not in result, "clean run must not report a cleanup error"
    assert (dest / "core" / "a.py").read_text(encoding="utf-8") == "A\n"
    assert (dest / "world" / "n.md").read_text(encoding="utf-8") == "# n\n"
    assert not (dest / _engine.STAGING_DIRNAME).exists(), "staging dir must be removed on success"


def test_do_swap_reports_staging_cleanup_error_not_silent(tmp_path, monkeypatch):
    """: a post-move staging-cleanup failure is REPORTED as a named
    field (fail-loud), the moves still count as succeeded, and do_swap does NOT
    raise — eliminating the silent-post-swap-death."""
    dest = tmp_path / "dest"
    dest.mkdir()
    _mk_staging(dest, {"core/a.py": "A\n"})

    real_rmtree = _engine.shutil.rmtree

    def fake_rmtree(path, ignore_errors=False, **kw):
        # The clean-removal attempt raises (simulating a Windows staging lock
        # after the moves already landed); the best-effort ignore_errors sweep
        # really removes it so tmp_path is left clean.
        if not ignore_errors:
            raise OSError("simulated staging lock (Windows post-move)")
        return real_rmtree(path, ignore_errors=True)

    monkeypatch.setattr(_engine.shutil, "rmtree", fake_rmtree)

    result = _engine.do_swap(dest)  # MUST NOT raise

    assert result["moved"] == 1, result
    assert result["failures"] == []
    assert "staging_cleanup_error" in result, "cleanup error must be surfaced, not swallowed"
    assert "rmtree" in result["staging_cleanup_error"]
    assert "simulated staging lock" in result["staging_cleanup_error"]
    # The move itself still landed — the swap is NOT a failure.
    assert (dest / "core" / "a.py").read_text(encoding="utf-8") == "A\n"


def test_do_swap_per_file_failure_path_unchanged(tmp_path, monkeypatch):
    """A genuine per-file move failure still returns failures[] (exit-6 path) and
    does NOT trigger the staging-cleanup branch (staging is preserved for retry)."""
    dest = tmp_path / "dest"
    dest.mkdir()
    _mk_staging(dest, {"core/a.py": "A\n"})

    def fake_copy2(src, dst, *a, **kw):
        raise OSError("simulated copy failure")

    monkeypatch.setattr(_engine.shutil, "copy2", fake_copy2)

    result = _engine.do_swap(dest)

    assert result["moved"] == 0
    assert len(result["failures"]) == 1
    assert "staging_cleanup_error" not in result, "failure path must not run staging cleanup"
    assert (dest / _engine.STAGING_DIRNAME).exists(), "staging preserved for retry on failure"


if __name__ == "__main__":
    import subprocess
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-v"]))
