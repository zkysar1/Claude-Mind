"""Verify cache-invalidate-inside-lock invariant in runtime daemon endpoints.

Background: g-115-674 hardened mind_api/src/endpoints/aspirations_write.py to
keep `_jsonl_cache().invalidate(live_path)` calls INSIDE the
`with file_locks.locked(live_path):` critical section. Out-of-lock invalidate
re-opens an eventual-consistency window where a reader can stat the new
mtime, miss the size-or-mtime check on a pathological tick collision, and
serve stale data. See: jsonl-read-modify-write-race.

This test asserts: every `.invalidate(...)` call in any non-underscore-
prefixed module under mind_api/src/endpoints/ lives lexically inside a
`with file_locks.locked(...):` block. Static AST analysis is sufficient —
the failure mode is a non-deterministic same-tick mtime race that cannot
be reliably triggered at test time.

Exit codes:
    0 — invariant intact (PASS)
    1 — invariant violated (FAIL with site list)
    2 — fatal error walking modules

Companion to verify-learning S48.11a.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
ENDPOINTS_DIR = REPO_ROOT / "core" / "runtime" / "endpoints"


def _is_file_locks_locked(call: ast.Call) -> bool:
    """Return True iff `call` is `file_locks.locked(...)`."""
    fn = call.func
    return (
        isinstance(fn, ast.Attribute)
        and fn.attr == "locked"
        and isinstance(fn.value, ast.Name)
        and fn.value.id == "file_locks"
    )


def _is_invalidate_call(node: ast.AST) -> bool:
    """Return True iff `node` is `<anything>.invalidate(...)`."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "invalidate"
    )


def find_violations(src: str, filename: str) -> list[tuple[str, int]]:
    """Return [(filename, lineno)] for each .invalidate() call NOT inside
    a `with file_locks.locked(...):` block (any depth, same module)."""
    tree = ast.parse(src, filename=filename)
    lock_ranges: list[tuple[int, int]] = []
    invalidate_lines: list[int] = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                ce = item.context_expr
                if isinstance(ce, ast.Call) and _is_file_locks_locked(ce):
                    lock_ranges.append((node.lineno, node.end_lineno or node.lineno))
        if _is_invalidate_call(node):
            invalidate_lines.append(node.lineno)

    violations = []
    for ln in invalidate_lines:
        inside_lock = any(start <= ln <= end for start, end in lock_ranges)
        if not inside_lock:
            violations.append((filename, ln))
    return violations


def main() -> int:
    if not ENDPOINTS_DIR.is_dir():
        print(f"FAIL: endpoints dir not found at {ENDPOINTS_DIR}", file=sys.stderr)
        return 2

    failures: list[tuple[str, int]] = []
    files_checked = 0
    files_skipped = 0

    for py_file in sorted(ENDPOINTS_DIR.glob("*.py")):
        if py_file.name.startswith("_"):
            files_skipped += 1
            continue
        src = py_file.read_text(encoding="utf-8")
        if ".invalidate(" not in src:
            continue
        files_checked += 1
        try:
            failures.extend(find_violations(src, py_file.name))
        except SyntaxError as e:
            print(f"FAIL: {py_file.name}: parse error {e}", file=sys.stderr)
            return 2

    if failures:
        print(
            f"FAIL: cache-invalidate-inside-lock invariant violated in "
            f"{len(failures)} site(s) -- g-115-674 regression risk:"
        )
        for fn, ln in failures:
            print(f"  {fn}:{ln}  invalidate() outside `with file_locks.locked()`")
        return 1

    print(
        f"PASS: cache-invalidate-inside-lock invariant intact across "
        f"{files_checked} endpoint module(s) ({files_skipped} underscore-prefixed skipped)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
