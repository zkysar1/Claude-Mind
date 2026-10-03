"""Directories the Bash edit recorder never walks, and the same test for logged rows.

bash-edit-record.sh walks core/ and .claude/ for files changed since its cursor
and logs each one to <agent>/session/uncommitted-edits.jsonl, the ledger read to
decide who wrote a file git is about to commit. A path git ignores is never a
commit candidate, so a row for one is noise (g-115-11716: 97% of one box's
159,548 rows, and 40,659 files stat'ed on every Bash call, 36,607 of them under
core/.pycache).

Skipping needs no git, which the recorder must not run on every Bash call: the
entries below are the .gitignore directory rules that sit under the two scan
roots, and test_bash_edit_record.py pins both directions against that file.
uncommitted-edits-prune.py uses the same test to drop rows already logged.
"""
import fnmatch

SCAN_ROOTS = ("core", ".claude")

# Skipped at any depth (`**/.history/`, `__pycache__/`, ... in .gitignore, plus .git itself).
SKIP_DIRS = frozenset({".git", ".python-shim", "__pycache__", "node_modules", ".pytest_cache", ".history"})

# Anchored directory rules, spelled as .gitignore spells them without the trailing slash.
SKIP_GLOBS = ("core/logs", "core/.pycache", "core/scripts/tests/_tmp_*", ".claude/worktrees")


def skip_dir(rel_dir, name):
    """True when directory `name` inside `rel_dir` (project-root-relative, POSIX) is not walked."""
    return name in SKIP_DIRS or any(fnmatch.fnmatchcase(f"{rel_dir}/{name}", g) for g in SKIP_GLOBS)


def is_skipped_path(rel_file):
    """True when a logged `file` lies under a directory skip_dir refuses, inside the scan roots.

    A row outside core/ and .claude/ (a world path, an absolute legacy path) was not written by
    the walk and is never classed as skipped: absence of proof keeps the row.
    """
    parts = rel_file.replace("\\", "/").split("/")
    if parts[0] not in SCAN_ROOTS:
        return False
    return any(skip_dir("/".join(parts[:i]), parts[i]) for i in range(1, len(parts) - 1))
