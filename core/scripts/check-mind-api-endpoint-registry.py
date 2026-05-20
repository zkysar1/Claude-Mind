#!/usr/bin/env python3
"""check-mind-api-endpoint-registry.py — Layer B pre-commit gate.

Verifies that mind_api/src/endpoints/__init__.py load_all imports resolve
to existing modules in the post-commit tree state. Prevents the
ImportError class observed 2026-05-15 where renamed endpoint modules
landed in commits without an updated __init__.py.

Modes:
  precommit (default): staged __init__.py against staged index
  --audit: working-tree __init__.py against working tree

Exit:
  0 — all imports resolve (or non-fatal skip)
  1 — at least one import target missing
  2 — usage error

See g-115-802 design doc: zeta/reports/g-115-802-design.md
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterable

TARGET_RELPATH = "mind_api/src/endpoints/__init__.py"
TARGET_PKG = "mind_api.src.endpoints"


def _git(*args: str) -> tuple[int, str]:
    r = subprocess.run(("git",) + args, capture_output=True, text=True)
    return r.returncode, r.stdout


def _repo_root() -> Path | None:
    rc, out = _git("rev-parse", "--show-toplevel")
    return Path(out.strip()) if rc == 0 else None


def _staged_content(rel_path: str) -> str | None:
    rc, out = _git("show", f":{rel_path}")
    return out if rc == 0 else None


def _staged_path_exists(rel_path: str) -> bool:
    rc, _ = _git("cat-file", "-e", f":{rel_path}")
    return rc == 0


def _resolve_targets(
    node: ast.ImportFrom, parent_pkg: str
) -> Iterable[tuple[str, str]]:
    """Yield (display, rel_path) for each module that must exist.

    Skip absolute imports (level == 0).

    Semantics (per g-115-802 design Section 3):
      from . import X         → parent_pkg/X.py
      from .X import Y        → parent_pkg/X/Y.py
      from .. import X        → parent_of(parent_pkg)/X.py
      from ..pkg import X     → parent_of(parent_pkg)/pkg/X.py

    Always yields one entry per alias (each X is a submodule file inside
    its package). Known limitation: if X is a SYMBOL exported by
    pkg/__init__.py (not a submodule file), the gate false-positives. The
    current registry uses only submodule imports — extend to also accept
    pkg/__init__.py if a future maintainer adds symbol imports.
    """
    if node.level == 0:
        return

    parts = parent_pkg.split(".")
    # level=1 stays in parent_pkg; level=2 goes up one; level=N up N-1.
    base = parts[: len(parts) - (node.level - 1)]
    if node.module:
        base = base + node.module.split(".")

    # Every alias is treated as a submodule file inside `base/`.
    # `from . import X` and `from ..pkg import X` are symmetric: X is the
    # module file, base names the directory it lives in.
    prefix = node.module + "." if node.module else ""
    for alias in node.names:
        rel_path = "/".join(base + [alias.name]) + ".py"
        display = f"{'.' * node.level}{prefix}{alias.name}"
        yield display, rel_path


def _check(content: str, exists: Callable[[str], bool]) -> list[tuple[str, str]]:
    """Parse content, return list of missing (display, rel_path)."""
    tree = ast.parse(content)
    missing: list[tuple[str, str]] = []
    seen: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        for display, rel_path in _resolve_targets(node, TARGET_PKG):
            if rel_path in seen:
                continue
            seen.add(rel_path)
            if not exists(rel_path):
                missing.append((display, rel_path))
    return missing


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] != "--audit":
        print(f"usage: {sys.argv[0]} [--audit]", file=sys.stderr)
        return 2
    audit_mode = bool(args)

    repo_root = _repo_root()
    if repo_root is None:
        return 0  # not in a git tree — fail-open (mirrors sibling gate)
    os.chdir(repo_root)

    if audit_mode:
        p = repo_root / TARGET_RELPATH
        if not p.is_file():
            print(
                f"[check-mind-api-endpoint-registry] WARN: {TARGET_RELPATH} missing",
                file=sys.stderr,
            )
            return 0
        content = p.read_text(encoding="utf-8")

        def exists_fn(rel_path: str) -> bool:
            return (repo_root / rel_path).is_file()
    else:
        # precommit: read staged __init__.py; if not staged, read HEAD;
        # if neither, skip (out of scope).
        content = _staged_content(TARGET_RELPATH)
        if content is None:
            p = repo_root / TARGET_RELPATH
            if not p.is_file():
                return 0
            content = p.read_text(encoding="utf-8")
        exists_fn = _staged_path_exists

    try:
        missing = _check(content, exists_fn)
    except SyntaxError as e:
        print(
            f"[check-mind-api-endpoint-registry] BLOCKED: {TARGET_RELPATH} "
            f"has syntax error: {e}",
            file=sys.stderr,
        )
        return 1

    if missing:
        print(
            "[check-mind-api-endpoint-registry] BLOCKED: load_all imports "
            "reference modules missing from the post-commit tree:",
            file=sys.stderr,
        )
        for display, rel_path in missing:
            print(f"  {display}  ->  {rel_path}", file=sys.stderr)
        print("", file=sys.stderr)
        print(
            "The daemon will fail to start with ImportError on these imports.",
            file=sys.stderr,
        )
        print(
            f"Either restore the missing modules or update {TARGET_RELPATH} "
            "to match.",
            file=sys.stderr,
        )
        print("", file=sys.stderr)
        print(
            "(g-115-802: prevents ImportError class observed 2026-05-15)",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
