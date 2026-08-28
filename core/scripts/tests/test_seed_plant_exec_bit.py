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

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))


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
