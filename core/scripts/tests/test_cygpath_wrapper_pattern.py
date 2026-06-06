"""test_cygpath_wrapper_pattern.py -  regression test.

Pins the cygpath -w conversion in utilization-stats.sh and
insight-trigger-sweep.sh after the 2026-05-17 fix.

Origin of bug: Under Git Bash on Windows, `$(cd ... && pwd)` returns POSIX
form /c/... which Windows python3 misinterprets as drive C: with a literal
subdir c/, yielding FileNotFoundError on C:\\c\\...\\<script>.py. The fix
converts SCRIPT_DIR via cygpath -w when available (falling through to plain
SCRIPT_DIR on Linux/macOS where cygpath is absent and POSIX paths work
natively).

WRAPPERS_NEEDING_CYGPATH is discovered dynamically (see
_discover_direct_python_wrappers below): every core/scripts/*.sh that execs
python3 with a SCRIPT_DIR-derived path is checked, and daemon-only wrappers
are excluded automatically. As of 2026-06-03 the only direct-python wrapper
left is insight-trigger-sweep.sh — utilization-stats.sh migrated to daemon-only
on 2026-05-29 and correctly drops out of the dynamic list (which is what this
file's prior hardcoded 2-entry list failed to reflect, breaking this suite).

Refs: g-115-892 (this fix), .claude/rules/no-python-cli-fallback.md
(daemon-only migration that bypassed these legacy direct-python wrappers).
"""
from __future__ import annotations

from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parent.parent


def _discover_direct_python_wrappers() -> list[str]:
    """core/scripts/*.sh wrappers that `exec python3 "$SCRIPT_DIR.../<x>.py"`.

    Self-maintaining: daemon-only wrappers (which route through rt_call in
    _runtime.sh, no direct python3 exec) are excluded automatically, so a
    wrapper migrating to daemon-only — e.g. utilization-stats.sh on 2026-05-29 —
    drops off this list with no manual edit, and a new direct-python wrapper is
    picked up the moment it lands. Replaces a hardcoded 2-entry list that went
    stale on that migration. An empty result is correct (no wrapper execs
    python3 directly -> none can carry the Windows path-mangling bug) and passes
    the checks below vacuously.
    """
    wrappers = []
    for sh in sorted(CORE_SCRIPTS.glob("*.sh")):
        for line in sh.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("exec python3") and "SCRIPT_DIR" in line:
                wrappers.append(sh.name)
                break
    return wrappers


WRAPPERS_NEEDING_CYGPATH = _discover_direct_python_wrappers()


def test_wrappers_use_cygpath_conversion() -> None:
    """All wrappers that exec python3 with SCRIPT_DIR must convert to native."""
    for wrapper in WRAPPERS_NEEDING_CYGPATH:
        path = CORE_SCRIPTS / wrapper
        src = path.read_text(encoding="utf-8")

        # Conversion variable must be defined.
        assert "SCRIPT_DIR_NATIVE" in src, (
            f"{wrapper}: missing SCRIPT_DIR_NATIVE variable"
        )
        # cygpath -w must be invoked on SCRIPT_DIR.
        assert 'cygpath -w "$SCRIPT_DIR"' in src, (
            f"{wrapper}: missing 'cygpath -w \"$SCRIPT_DIR\"' conversion"
        )
        # The non-Windows fallthrough must keep SCRIPT_DIR unchanged.
        assert 'SCRIPT_DIR_NATIVE="$SCRIPT_DIR"' in src, (
            f"{wrapper}: missing Linux/macOS fallthrough"
        )
        # exec must use the converted variable, NOT raw SCRIPT_DIR.
        assert 'exec python3 "$SCRIPT_DIR_NATIVE/' in src, (
            f"{wrapper}: exec must use $SCRIPT_DIR_NATIVE not raw $SCRIPT_DIR"
        )
        # Regression guard: no exec line may use the raw form.
        for line in src.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("exec python3"):
                assert '"$SCRIPT_DIR/' not in line, (
                    f"{wrapper}: regression -- exec line uses raw "
                    f"$SCRIPT_DIR: {line!r}"
                )


def test_wrappers_have_documentation_comment() -> None:
    """The cygpath block must carry a comment naming  so the next
    reader can find the rationale without git-archaeology."""
    for wrapper in WRAPPERS_NEEDING_CYGPATH:
        path = CORE_SCRIPTS / wrapper
        src = path.read_text(encoding="utf-8")
        assert "g-115-892" in src, (
            f"{wrapper}: cygpath block missing g-115-892 comment anchor"
        )


if __name__ == "__main__":
    test_wrappers_use_cygpath_conversion()
    test_wrappers_have_documentation_comment()
    print("PASS: cygpath conversion applied to all path-mangling-vulnerable wrappers")
