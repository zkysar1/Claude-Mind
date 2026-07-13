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

Two lock-holding idioms extend the lexical rule (2026-07-10, after the
generic-store/experience s5c endpoints centralized their write sequence):

1. `file_locks.locked_rmw(path, _cycle)` — the closure runs under the lock,
   so any def passed by name as the rmw callback counts as a locked range.
2. AUDITED LOCKED HELPERS — a helper (e.g. store.py `_commit`,
   experience_write.py `_append_record`) that centralizes
   history->write->changelog->invalidate and is ONLY called with the lock
   held. The allowlist below is VERIFIED, not trusted: every same-module
   call site of an audited helper must itself sit inside a locked range
   (with-block or rmw callback), else that call site is reported as a
   violation. A future unlocked caller therefore still FAILS.

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
ENDPOINTS_DIR = REPO_ROOT / "mind_api" / "src" / "endpoints"

# Helpers whose .invalidate() runs under a lock HELD BY EVERY CALLER.
# Membership is verified at test time (see docstring idiom 2): each
# same-module call site must be inside a with-lock or rmw-callback range.
AUDITED_LOCKED_HELPERS: dict[str, set[str]] = {
    # _commit is invoked only from _cycle closures passed to
    # file_locks.locked_rmw(path, _cycle).
    "store.py": {"_commit"},
    # _append_record's call sites sit inside `with file_locks.locked(live)`
    # blocks (add + archive-goal handlers).
    "experience_write.py": {"_append_record"},
}


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


def _is_locked_rmw(call: ast.Call) -> bool:
    """Return True iff `call` is `file_locks.locked_rmw(...)`."""
    fn = call.func
    return (
        isinstance(fn, ast.Attribute)
        and fn.attr == "locked_rmw"
        and isinstance(fn.value, ast.Name)
        and fn.value.id == "file_locks"
    )


def find_violations(src: str, filename: str) -> list[tuple[str, int, str]]:
    """Return [(filename, lineno, reason)] for each .invalidate() call NOT
    inside a locked range, plus each unlocked call site of an audited
    helper. Locked ranges are `with file_locks.locked(...):` blocks and the
    def bodies of callbacks passed by name to `file_locks.locked_rmw`."""
    tree = ast.parse(src, filename=filename)
    lock_ranges: list[tuple[int, int]] = []
    rmw_callback_names: set[str] = set()
    # name -> ALL def ranges with that name (nested closures like the four
    # per-handler `_cycle` defs in store.py share one name)
    def_ranges: dict[str, list[tuple[int, int]]] = {}
    invalidate_lines: list[int] = []
    calls_by_name: dict[str, list[int]] = {}

    for node in ast.walk(tree):
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                ce = item.context_expr
                if isinstance(ce, ast.Call) and _is_file_locks_locked(ce):
                    lock_ranges.append((node.lineno, node.end_lineno or node.lineno))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            def_ranges.setdefault(node.name, []).append(
                (node.lineno, node.end_lineno or node.lineno))
        if isinstance(node, ast.Call):
            if _is_locked_rmw(node) and len(node.args) >= 2 and isinstance(node.args[1], ast.Name):
                rmw_callback_names.add(node.args[1].id)
            if isinstance(node.func, ast.Name):
                calls_by_name.setdefault(node.func.id, []).append(node.lineno)
        if _is_invalidate_call(node):
            invalidate_lines.append(node.lineno)

    for name in rmw_callback_names:
        lock_ranges.extend(def_ranges.get(name, []))

    violations: list[tuple[str, int, str]] = []

    def _locked(ln: int) -> bool:
        return any(start <= ln <= end for start, end in lock_ranges)

    # Verify audited helpers: every call site must itself be locked.
    audited_ok_ranges: list[tuple[int, int]] = []
    for helper in AUDITED_LOCKED_HELPERS.get(filename, set()):
        if helper not in def_ranges:
            violations.append((filename, 0, f"audited helper {helper}() not found — update AUDITED_LOCKED_HELPERS"))
            continue
        unlocked_sites = [ln for ln in calls_by_name.get(helper, []) if not _locked(ln)]
        if unlocked_sites:
            for ln in unlocked_sites:
                violations.append((filename, ln, f"call to audited helper {helper}() outside any locked range"))
        else:
            audited_ok_ranges.extend(def_ranges[helper])

    for ln in invalidate_lines:
        if _locked(ln):
            continue
        if any(start <= ln <= end for start, end in audited_ok_ranges):
            continue
        violations.append((filename, ln, "invalidate() outside `with file_locks.locked()`"))
    return violations


def main() -> int:
    if not ENDPOINTS_DIR.is_dir():
        print(f"FAIL: endpoints dir not found at {ENDPOINTS_DIR}", file=sys.stderr)
        return 2

    failures: list[tuple[str, int, str]] = []
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
        for fn, ln, reason in failures:
            print(f"  {fn}:{ln}  {reason}")
        return 1

    print(
        f"PASS: cache-invalidate-inside-lock invariant intact across "
        f"{files_checked} endpoint module(s) ({files_skipped} underscore-prefixed skipped)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
