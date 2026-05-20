"""meta -> world import gate (sec15 Phase-5 invariant enforcer).

AST-scans every .py file under mind_api/src/meta/ and verifies it imports
NOTHING from mind_api.src.world or any of the 7 world-domain endpoint
module names. Exit 0 = invariant holds (count == 0). Exit 1 = invariant
violated (count > 0); prints offending files and import statements.

Run:
    py -3 core/scripts/meta-imports-world-gate.py
    # or
    py -3 core/scripts/meta-imports-world-gate.py --json   # machine-readable

Wired into the per-phase gate sequence in HARDENING-preparing-to-remove-
python-cli.md sec17 (Phase 5 onward). The gate fires after every
mind_api/src/ change to keep the seam clean as new code lands.

Design notes:
  - AST scan (not grep) so commented-out imports + string-mentions don't
    false-positive.
  - Catches BOTH `import mind_api.src.world.X` AND
    `from mind_api.src.world import X` AND
    `from ..world import X` (relative form used inside the package tree).
  - Catches BOTH the package-qualified form (mind_api.src.world.*) AND
    bare endpoint module names if someone added a sys.path hack later.
  - Recursively walks meta/** so future meta endpoints inherit the gate.
  - Fail-loud-not-silent: if AST parse fails on a meta file, exits 1 with
    the parse error (a broken file IS a violation of "meta is clean").
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import List, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[2]
META_DIR = PROJECT_ROOT / "mind_api" / "src" / "meta"

# The 7 world-domain endpoint module names (per MAP in
# zeta/reports/phase5-world-meta-split-plan.md). Bare-import form is
# also forbidden to catch sys.path-injection attempts.
WORLD_MODULE_NAMES = frozenset({
    "reasoning_bank", "pipeline", "pipeline_write", "pattern_signatures",
    "tree", "tree_read", "team_state",
})


def _is_world_module(name: str) -> bool:
    """Return True if `name` references mind_api.src.world.* or a bare
    world endpoint module name."""
    if not name:
        return False
    # mind_api.src.world or mind_api.src.world.<endpoint>
    if name == "mind_api.src.world" or name.startswith("mind_api.src.world."):
        return True
    # Bare endpoint module name (e.g. `import reasoning_bank` via sys.path)
    head = name.split(".", 1)[0]
    if head in WORLD_MODULE_NAMES:
        return True
    return False


def _is_relative_world_import(node: ast.ImportFrom) -> bool:
    """Return True for `from ..world import X` or `from ..world.X import Y`."""
    # node.level: number of leading dots. node.module: the part after dots.
    # For meta/* files: level=2 (..) + module="world" or "world.X" means
    # ../../world/... which from mind_api/src/meta = mind_api/src/world.
    if node.level >= 1 and node.module:
        if node.module == "world" or node.module.startswith("world."):
            return True
    # Also catch the bare endpoint module name in a relative form (paranoid).
    if node.level >= 1 and node.module:
        head = node.module.split(".", 1)[0]
        if head in WORLD_MODULE_NAMES:
            return True
    return False


def scan_file(path: Path) -> List[Tuple[int, str]]:
    """Return list of (lineno, offending_import_text) for forbidden imports."""
    try:
        src = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [(0, f"<read failed: {exc}>")]
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as exc:
        return [(getattr(exc, "lineno", 0) or 0, f"<parse failed: {exc}>")]

    hits: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_world_module(alias.name):
                    hits.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            # Absolute: from mind_api.src.world... import X
            if node.level == 0 and node.module and _is_world_module(node.module):
                names = ", ".join(a.name for a in node.names)
                hits.append((node.lineno, f"from {node.module} import {names}"))
            # Relative: from ..world import X
            elif _is_relative_world_import(node):
                dots = "." * node.level
                names = ", ".join(a.name for a in node.names)
                hits.append((node.lineno, f"from {dots}{node.module} import {names}"))
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true",
                        help="emit JSON instead of human-readable output")
    args = parser.parse_args()

    if not META_DIR.is_dir():
        msg = f"meta dir missing: {META_DIR}"
        if args.json:
            print(json.dumps({"ok": False, "error": msg}))
        else:
            print(f"GATE ERROR: {msg}", file=sys.stderr)
        return 1

    findings: List[dict] = []
    file_count = 0
    for py in sorted(META_DIR.rglob("*.py")):
        # Skip __pycache__
        if "__pycache__" in py.parts:
            continue
        file_count += 1
        hits = scan_file(py)
        for lineno, text in hits:
            findings.append({
                "file": str(py.relative_to(PROJECT_ROOT)).replace("\\", "/"),
                "line": lineno,
                "import": text,
            })

    if args.json:
        print(json.dumps({
            "ok": len(findings) == 0,
            "scanned_files": file_count,
            "violations": findings,
        }, indent=2))
    else:
        if findings:
            print(f"GATE FAILED: meta -> world import count = {len(findings)}", file=sys.stderr)
            for f in findings:
                print(f"  {f['file']}:{f['line']}  {f['import']}", file=sys.stderr)
        else:
            print(f"GATE OK: scanned {file_count} file(s) under mind_api/src/meta/; "
                  f"meta -> world import count = 0")
    return 0 if not findings else 1


if __name__ == "__main__":
    sys.exit(main())
