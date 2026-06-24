# Regression tests for the promotion preflight drift gate
# (core/scripts/promotion-preflight.py). Runnable two ways:
#   py -3 core/scripts/tests/test_promotion_preflight.py     (standalone)
#   py -3 -m pytest core/scripts/tests/test_promotion_preflight.py -q
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "promotion-preflight.py"


def _run(src: Path, tgt: Path, *extra: str) -> int:
    """Run the gate via the SAME interpreter (portable; no py-launcher dep)."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--source", str(src), "--target", str(tgt), *extra],
        capture_output=True, text=True,
    ).returncode


def _mk(base: Path, rel: str, content: str = "x\n") -> None:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def test_clean_subset_exits_0(tmp_path):
    src, tgt = tmp_path / "src", tmp_path / "tgt"
    _mk(src, "core/scripts/foo.sh", "echo hi\n")
    _mk(tgt, "core/scripts/foo.sh", "echo hi\n")
    assert _run(src, tgt) == 0


def test_target_only_framework_file_is_orphan_drift_exit_2(tmp_path):
    src, tgt = tmp_path / "src", tmp_path / "tgt"
    _mk(src, "core/scripts/foo.sh", "echo hi\n")
    _mk(tgt, "core/scripts/foo.sh", "echo hi\n")
    _mk(tgt, "core/scripts/bar.sh", "echo extra\n")  # target leads -> would be orphaned
    assert _run(src, tgt) == 2


def test_pyc_and_pycache_noise_excluded_exit_0(tmp_path):
    src, tgt = tmp_path / "src", tmp_path / "tgt"
    _mk(src, "core/scripts/foo.sh", "echo hi\n")
    _mk(tgt, "core/scripts/foo.sh", "echo hi\n")
    _mk(tgt, "core/scripts/__pycache__/foo.cpython-312.pyc")  # build artifact
    _mk(tgt, "core/scripts/tests/_tmp_run_test/out.txt")       # temp test artifact
    assert _run(src, tgt) == 0


def test_differing_file_does_not_block_by_default_but_blocks_strict(tmp_path):
    src, tgt = tmp_path / "src", tmp_path / "tgt"
    _mk(src, "core/scripts/foo.sh", "echo SOURCE\n")
    _mk(tgt, "core/scripts/foo.sh", "echo TARGET\n")  # differs, direction unknown
    assert _run(src, tgt) == 0            # default: differing alone is review-only
    assert _run(src, tgt, "--strict") == 2  # strict: differing blocks


def test_deployment_local_difference_not_blocking(tmp_path):
    src, tgt = tmp_path / "src", tmp_path / "tgt"
    _mk(src, "CLAUDE.md", "dev deployment\n")
    _mk(tgt, "CLAUDE.md", "prod deployment\n")  # legit per-deployment difference
    _mk(src, "core/scripts/foo.sh", "echo hi\n")
    _mk(tgt, "core/scripts/foo.sh", "echo hi\n")
    assert _run(src, tgt) == 0            # deployment-local diff never blocks
    assert _run(src, tgt, "--strict") == 0


def test_target_only_skill_is_not_core_orphan_drift(tmp_path):
    # A target-only SKILL (usually domain/forged) must not trip the core
    # orphan-risk blocker by default (it lands in the "verify domain-local" bucket).
    src, tgt = tmp_path / "src", tmp_path / "tgt"
    _mk(src, "core/scripts/foo.sh", "echo hi\n")
    _mk(tgt, "core/scripts/foo.sh", "echo hi\n")
    _mk(tgt, ".claude/skills/manage-website/SKILL.md", "---\n---\ndomain skill\n")
    assert _run(src, tgt) == 0


def _main() -> int:
    import tempfile
    failures = 0
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        with tempfile.TemporaryDirectory() as d:
            try:
                t(Path(d))
                print(f"PASS  {t.__name__}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL  {t.__name__}  {e}")
            except Exception as e:  # noqa: BLE001
                failures += 1
                print(f"ERROR {t.__name__}  {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())
