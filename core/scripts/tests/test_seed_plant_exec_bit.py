"""test_seed_plant_exec_bit.py -- .

The seed plant stripped the executable bit off every TEXT file it staged.
Detection had landed twice (g-360-07 source-index mode-strip block, g-360-09
preflight exec_bits/mode_differing) without preservation ever following, and
v2.12.5 planted 628/628 source-executable paths as 100644.

MECHANISM, precisely: do_copy_staged has four staging branches. Three call
shutil.copy2, which carries mode. The fourth -- the text branch -- calls
dst.write_text(), which CREATES the file at the umask default. Every .sh and
every git hook is text, so the strip was total for exactly the population whose
exec bit matters.

The failure is silent by nature: a non-executable git hook does not error, it
never runs, so a downstream Mind adopting the tag operates with its commit
gates absent and nothing red anywhere.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

# The filesystem-level tests below assert st_mode bits that DO NOT EXIST on
# Windows: os.chmod cannot set one and does not error, so they are red on every
# Windows box (measured DESKTOP-O91DLK2, 2026-09-02) for a reason that is not a
# defect in the carry. The index-level tests further down cover that platform --
# and that platform is exactly where the carry needs covering ().
_NO_FS_EXEC_BIT = pytest.mark.skipif(
    os.name == "nt",
    reason="filesystem execute bits do not exist on Windows; the index-level tests cover that path (g-360-16)",
)


def _mk_source(root: Path) -> None:
    """A minimal git repo holding one executable .sh and one plain .md."""
    (root / "core" / "scripts").mkdir(parents=True)
    sh = root / "core" / "scripts" / "hook.sh"
    sh.write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
    sh.chmod(sh.stat().st_mode | 0o755)
    (root / "README.md").write_text("# plain\n", encoding="utf-8")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "seed"]):
        subprocess.run(["git", "-C", str(root), *args], env=env,
                       capture_output=True, check=True)


def _manifest() -> dict:
    return {"include": [{"path": "core/scripts/hook.sh"}, {"path": "README.md"}],
            "transformations": []}


@_NO_FS_EXEC_BIT
def test_plant_preserves_exec_bit_on_text_files():
    """The bit survives the TEXT branch -- the one that writes rather than copies."""
    import _seed_engine as eng

    with tempfile.TemporaryDirectory() as td:
        src, dest = Path(td) / "src", Path(td) / "dest"
        dest.mkdir(parents=True)
        _mk_source(src)

        stats = eng.do_copy_staged(src, dest, _manifest())

        staged_sh = dest / eng.STAGING_DIRNAME / "core" / "scripts" / "hook.sh"
        staged_md = dest / eng.STAGING_DIRNAME / "README.md"
        assert staged_sh.is_file(), "the .sh was not staged at all"

        # THE ASSERTION. Pre-fix this is 0 and the test is RED.
        assert staged_sh.stat().st_mode & stat.S_IXUSR, (
            "staged .sh lost its executable bit -- a git hook planted this way "
            "never runs and never errors (g-360-13)"
        )
        # ADD-ONLY: a non-executable source must NOT acquire the bit.
        assert not (staged_md.stat().st_mode & 0o111), (
            "a plain .md gained an exec bit -- carry_exec_bit must be add-only "
            "for genuinely-executable sources, not a blanket chmod"
        )
        # Positive control on the counters: a bare 0 carried would otherwise be
        # indistinguishable from "no executable sources existed" (guard-2298).
        assert stats["exec_source_executable"] >= 1, (
            f"no source was seen as executable -- the index/fs read is broken, "
            f"so the zero below proves nothing: {stats}"
        )
        assert stats["exec_bits_carried"] >= 1, f"nothing carried: {stats}"


@_NO_FS_EXEC_BIT
def test_index_map_declines_cleanly_outside_a_repo():
    """{} is a DECLINE, and the fs fallback must still carry the bit."""
    from _exec_bits import carry_exec_bit, index_exec_map

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        assert index_exec_map(root) == {}, "non-repo must yield an empty map"
        src, dst = root / "a.sh", root / "b.sh"
        src.write_text("#!/bin/sh\n", encoding="utf-8")
        src.chmod(src.stat().st_mode | 0o755)
        dst.write_text("#!/bin/sh\n", encoding="utf-8")
        assert carry_exec_bit("a.sh", src, dst, {}) is True
        assert dst.stat().st_mode & stat.S_IXUSR


def test_unknown_source_leaves_dest_untouched():
    """resolve_exec -> None must be a no-op, never a strip (add-only asymmetry)."""
    from _exec_bits import carry_exec_bit

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        dst = root / "d.sh"
        dst.write_text("x\n", encoding="utf-8")
        dst.chmod(dst.stat().st_mode | 0o755)
        before = dst.stat().st_mode
        # Source does not exist -> exec_bits None -> resolve_exec (None,"fs")
        assert carry_exec_bit("gone.sh", root / "gone.sh", dst, {}) is False
        assert dst.stat().st_mode == before, "an unknown source must not strip the dest"


# ---------------------------------------------------------------------------
# : the git-level carry and its verifier. These run on EVERY platform
# because they read and write the INDEX, which is what a commit records. The
# filesystem tests above are skipped on Windows precisely because there the
# filesystem bit does not exist and chmod is a silent no-op -- measured on the
# v2.12.47 hop: 15 NEW scripts landed at staging as 100644 while the plant's
# counters said "carried" and seed-verify said FAILS 0.
# ---------------------------------------------------------------------------

_GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], env=_GIT_ENV,
                   capture_output=True, check=True, text=True)


def _write_pair(root: Path) -> None:
    (root / "core" / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "core" / "scripts" / "hook.sh").write_text(
        "#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
    (root / "README.md").write_text("# plain\n", encoding="utf-8")


def _mk_source_committed(root: Path) -> None:
    """Source: hook.sh committed at 100755 -- set in the INDEX, so the fixture
    builds identically on a core.fileMode=false clone."""
    _write_pair(root)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "update-index", "--chmod=+x", "--", "core/scripts/hook.sh")
    _git(root, "commit", "-qm", "seed")


def _mk_dest_planted(root: Path) -> None:
    """Destination: the same files freshly `git add`-ed -- the shape a plant
    leaves behind, with hook.sh at 100644 (no chmod reached the index)."""
    _write_pair(root)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "update-index", "--chmod=-x", "--", "core/scripts/hook.sh")


def test_staged_exec_map_reads_the_index_not_the_filesystem():
    from _exec_bits import staged_exec_map

    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "src"
        src.mkdir()
        _mk_source_committed(src)
        m = staged_exec_map(src)
        assert m == {"core/scripts/hook.sh": True, "README.md": False}, m
        assert staged_exec_map(Path(td) / "not-a-repo") == {}, "non-repo must DECLINE with {}"


def test_index_carry_sets_100755_in_the_dest_index_without_a_filesystem_bit():
    from _exec_bits import carry_index_exec_bits, staged_exec_map

    with tempfile.TemporaryDirectory() as td:
        src, dest = Path(td) / "src", Path(td) / "dest"
        src.mkdir()
        dest.mkdir()
        _mk_source_committed(src)
        _mk_dest_planted(dest)
        assert staged_exec_map(dest)["core/scripts/hook.sh"] is False, "fixture must start stripped"

        res = carry_index_exec_bits(src, dest)
        assert res["pass"] is True, res
        assert res["updated"] == 1 and res["candidates"] == 1, res
        after = staged_exec_map(dest)
        assert after["core/scripts/hook.sh"] is True, "the carry must land in the INDEX"
        assert after["README.md"] is False, "add-only: a plain file must not gain the bit"

        # Idempotent: a second pass finds nothing to do and touches nothing.
        again = carry_index_exec_bits(src, dest)
        assert again["pass"] is True and again["updated"] == 0, again
        assert again["already_executable"] == 1, again


def test_index_carry_declines_loudly_without_a_source_map_or_dest_index():
    from _exec_bits import carry_index_exec_bits

    with tempfile.TemporaryDirectory() as td:
        src, dest = Path(td) / "src", Path(td) / "dest"
        src.mkdir()
        dest.mkdir()
        _mk_source_committed(src)
        res = carry_index_exec_bits(src, dest)  # dest has no .git
        assert res["pass"] is False and "no git index" in res["error"], res
        res = carry_index_exec_bits(Path(td) / "nope", dest)  # source unreadable
        assert res["pass"] is False and "source" in res["error"], res


def test_verify_index_exec_bits_names_the_stripped_path_then_clears():
    from _exec_bits import carry_index_exec_bits, verify_index_exec_bits

    with tempfile.TemporaryDirectory() as td:
        src, dest = Path(td) / "src", Path(td) / "dest"
        src.mkdir()
        dest.mkdir()
        _mk_source_committed(src)
        _mk_dest_planted(dest)
        before = verify_index_exec_bits(src, dest)
        assert before["pass"] is False, before
        assert before["stripped"] == ["core/scripts/hook.sh"], before
        carry_index_exec_bits(src, dest)
        after = verify_index_exec_bits(src, dest)
        assert after["pass"] is True and after["stripped"] == [], after
        assert after["checked"] == 1, after


def test_verify_index_exec_bits_skips_a_non_git_dest_and_fails_a_blind_source():
    from _exec_bits import verify_index_exec_bits

    with tempfile.TemporaryDirectory() as td:
        src, dest = Path(td) / "src", Path(td) / "dest"
        src.mkdir()
        dest.mkdir()
        _mk_source_committed(src)
        _write_pair(dest)  # planted, never git-init'd: no modes exist yet
        res = verify_index_exec_bits(src, dest)
        assert res["pass"] is True and res.get("skipped"), "no index yet is a SKIP, not a fail"
        res = verify_index_exec_bits(Path(td) / "nope", dest)
        assert res["pass"] is False and res.get("error"), "a blind source must not read as clean"


def test_engine_cli_verify_exec_bits_carries_the_verdict_in_its_exit_code():
    """seed-verify consumes the subcommand's rc -- the  shape, so a
    refusal nobody reads is impossible here."""
    import json

    with tempfile.TemporaryDirectory() as td:
        src, dest = Path(td) / "src", Path(td) / "dest"
        src.mkdir()
        dest.mkdir()
        _mk_source_committed(src)
        _mk_dest_planted(dest)
        engine = SCRIPT_DIR / "_seed_engine.py"

        def run(cmd: str) -> tuple[int, dict]:
            p = subprocess.run(
                [sys.executable, str(engine), cmd, "--source", str(src), "--dest", str(dest)],
                capture_output=True, text=True, timeout=120,
            )
            return p.returncode, (json.loads(p.stdout) if p.stdout.strip() else {})

        rc, data = run("verify-exec-bits")
        assert rc == 1 and data["stripped"] == ["core/scripts/hook.sh"], (rc, data)
        rc, data = run("carry-exec-bits")
        assert rc == 0 and data["updated"] == 1, (rc, data)
        rc, data = run("verify-exec-bits")
        assert rc == 0 and data["pass"] is True, (rc, data)
