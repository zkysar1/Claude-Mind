#!/usr/bin/env python3
"""Refuse a POSIX-only call inside a pytest skipif predicate.

WHY THIS EXISTS (2026-09-22, g-115-10621). A `skipif` predicate is evaluated at
MODULE SCOPE, so a POSIX-only call inside it raises AttributeError during
COLLECTION on Windows -- which pytest reports as rc=2 for the ENTIRE CHUNK, not
just the offending file. One line in test_owncloud_endpoint_flip.py took chunk
02 of a 4-chunk full suite with it: 772 of 1545 files never ran and the run
emitted `VERDICT: INVALID` after 1h53m.

    @pytest.mark.skipif(os.geteuid() != 0, ...)                      # voids the chunk
    @pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, ...)   # correct

Neither shape is caught by running the file alone -- on a POSIX box both pass.
The behavioural rule is guard-7328.

AST, NOT TEXT -- and this is load-bearing, not a style preference. The first
version of this checker scanned for the `skipif` keyword and balanced parens,
and it flagged its OWN docstring and its own test fixtures' string literals: a
text predicate cannot tell code from prose ABOUT code, so any file documenting
the defect becomes a false positive. That is fatal here, because the file most
likely to quote the bad shape is the guard against it (and a permanently-red
check is worse than no check -- guard-329). Parsing decorators as AST nodes
removes the class entirely.

Scope note (guard-5611): the population is a GLOB over the test tree, not a
grep for a naming convention, so a conforming-behaviour / non-conforming-name
file cannot hide from it.

Exit 0 = PASS (or nothing to scan), 1 = FAIL (offending sites named).
"""
import ast
import glob
import os
import sys

# Attribute accesses that do not exist on Windows, as (module, attr) pairs.
POSIX_ONLY_ATTRS = {
    ("os", "geteuid"), ("os", "getuid"),
    ("os", "getgid"), ("os", "getegid"),
    ("os", "getlogin"),
}
# Modules with no Windows implementation at all: ANY attribute access raises.
POSIX_ONLY_MODULES = {"pwd", "grp", "fcntl", "termios"}


def _is_skipif(node):
    """True if this decorator AST node is a skipif call."""
    func = node.func if isinstance(node, ast.Call) else node
    if isinstance(func, ast.Attribute):
        return func.attr == "skipif"
    if isinstance(func, ast.Name):
        return func.id == "skipif"
    return False


def _unguarded_posix_calls(node):
    """Names of POSIX-only accesses in this subtree, unless hasattr guards it.

    `hasattr` ANYWHERE in the predicate is the sanctioned guard: the whole
    idiom is `not hasattr(os, "x") or os.x() != 0`, where the short-circuit
    keeps the second operand from ever being evaluated on Windows.
    """
    found = []
    guarded = False
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id == "hasattr":
            guarded = True
        if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name):
            pair = (sub.value.id, sub.attr)
            if pair in POSIX_ONLY_ATTRS or sub.value.id in POSIX_ONLY_MODULES:
                found.append("%s.%s" % pair)
    return [] if guarded else found


def offenders(root):
    """Yield 'path:line (call)' for each skipif predicate with an unguarded call."""
    hits = []
    for path in sorted(glob.glob(os.path.join(root, "**", "*.py"), recursive=True)):
        try:
            src = open(path, encoding="utf-8", errors="replace").read()
            tree = ast.parse(src, filename=path)
        except (OSError, SyntaxError) as exc:
            # A file that will not parse cannot be collected either; that is a
            # separate, louder problem. Report and keep scanning.
            print("WARN: could not parse %s: %s" % (path, exc), file=sys.stderr)
            continue
        for node in ast.walk(tree):
            for dec in getattr(node, "decorator_list", []):
                if not _is_skipif(dec):
                    continue
                for call in _unguarded_posix_calls(dec):
                    hits.append("%s:%d (%s)" % (
                        path.replace("\\", "/"), dec.lineno, call))
    return hits


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "core/scripts/tests"
    if not os.path.isdir(root):
        print("SKIP: %s not present (nothing to scan)" % root)
        return 0
    hits = offenders(root)
    if hits:
        print("FAIL: unguarded POSIX-only call in a skipif predicate "
              "(evaluated at module scope -> AttributeError at COLLECTION on "
              "Windows -> rc=2 voids the whole chunk; guard-7328): "
              + ", ".join(hits))
        return 1
    print("PASS: every skipif predicate under %s is portable" % root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
