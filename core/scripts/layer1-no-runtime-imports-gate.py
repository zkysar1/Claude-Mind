"""Layer-1 -> Layer-2 import gate (sec15 Phase-6 invariant enforcer).

Enforces the layer boundary documented in core/BOUNDARY.md:
    Layer 1 (Client/Agent Framework — future OSS) must NOT import or
    source anything from Layer 2 (Service/Daemon).

Specifically, no file in the Layer-1 set may:
  (a) Python-import `mind_api.src` or any submodule (absolute form), nor
  (b) `source` a path under `mind_api/src/` from a .sh wrapper, nor
  (c) `python -c` a snippet that imports `mind_api.src.X` (the daemon
      launcher `python -m mind_api.src` from mind-api-start.sh is allowed
      and explicitly whitelisted — it is HOW Layer 1 starts the daemon,
      not how Layer 1 depends on Layer 2 code).

Layer-1 set (per BOUNDARY.md):
  - All .sh wrappers under core/scripts/ (except mind-api-start.sh which
    holds the daemon-launcher whitelist case; the gate special-cases it
    rather than excluding the whole file).
  - All .py files under core/scripts/ EXCEPT the explicit Layer-2 ones:
    gates/, _fileops.py, _override_helpers.py.
  - .claude/skills/ and .claude/rules/ (rarely contain imports, but
    included for completeness).

Run:
    py -3 core/scripts/layer1-no-runtime-imports-gate.py
    py -3 core/scripts/layer1-no-runtime-imports-gate.py --json

Exit 0 = invariant holds. Exit 1 = violated (prints offending
file:line:reference).

Design notes:
  - Python: AST scan (commented-out imports + string-mentions don't
    false-positive).
  - Shell: regex scan (no real AST; rely on `source`/`. ` and
    `python -c` patterns plus a strict whitelist of `python -m mind_api.src`).
  - Doc strings + comments that contain the phrase "mind_api/src/"
    (e.g., error messages like "See mind_api/src/endpoints/ for API
    docs") are NOT violations — they are informational, not code
    coupling. The gate does NOT flag string literals or comments.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import List


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = PROJECT_ROOT / "core" / "scripts"
CLAUDE_DIR = PROJECT_ROOT / ".claude"

# Layer-2 Python modules (per BOUNDARY.md) that live INSIDE core/scripts/
# but are server-side. Exclude these from the Layer-1 scan.
LAYER2_PY_IN_SCRIPTS = frozenset({
    "_fileops.py", "_override_helpers.py",
})
# Layer-2 Python directories inside core/scripts/.
LAYER2_PY_DIRS = ("gates",)

# Source-code patterns that indicate a Layer-2 dependency in a shell file.
# `python -m mind_api.src` is the DAEMON LAUNCHER and is ALLOWED — it's how
# Layer 1 invokes the daemon, not how Layer 1 imports Layer 2 code.
_SH_RUNTIME_REFS = re.compile(
    r'^\s*(?:source|\.)\s+[^\n#]*mind_api/src/'  # source ... mind_api/src/...
    r'|^\s*python[0-9]*\s+-c\s+[^\n]*mind_api\.src'  # python -c '...mind_api.src...'
    r'|^\s*from\s+mind_api\.src'  # heredoc'd from import (in EOF blocks)
    r'|^\s*import\s+mind_api\.src',
    re.MULTILINE,
)
# The whitelisted daemon launcher: `python -m mind_api.src` or
# `python3 -m mind_api.src` or `py -3 -m mind_api.src`.
_SH_LAUNCHER_WHITELIST = re.compile(
    r'\bpython[0-9]*\s+-m\s+mind_api\.src\b'
    r'|\bpy\s+-3\s+-m\s+mind_api\.src\b',
)


def _layer1_py_files() -> List[Path]:
    """Enumerate Layer-1 Python files."""
    out: List[Path] = []
    # core/scripts/*.py minus Layer-2 ones
    for py in sorted(SCRIPTS_DIR.glob("*.py")):
        if py.name in LAYER2_PY_IN_SCRIPTS:
            continue
        out.append(py)
    # .claude/skills/**/*.py + .claude/rules/**/*.py
    if CLAUDE_DIR.is_dir():
        for sub in ("skills", "rules"):
            d = CLAUDE_DIR / sub
            if d.is_dir():
                out.extend(sorted(d.rglob("*.py")))
    return out


def _layer1_sh_files() -> List[Path]:
    """Enumerate Layer-1 shell files."""
    out: List[Path] = []
    for sh in sorted(SCRIPTS_DIR.glob("*.sh")):
        out.append(sh)
    return out


def scan_py(path: Path) -> List[dict]:
    """AST-scan a Layer-1 .py file for mind_api.src imports."""
    try:
        src = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [{"line": 0, "reference": f"<read failed: {exc}>"}]
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as exc:
        return [{"line": getattr(exc, "lineno", 0) or 0,
                 "reference": f"<parse failed: {exc}>"}]
    hits: List[dict] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "mind_api.src" or alias.name.startswith("mind_api.src."):
                    hits.append({"line": node.lineno, "reference": f"import {alias.name}"})
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module and (
                node.module == "mind_api.src" or node.module.startswith("mind_api.src.")
            ):
                names = ", ".join(a.name for a in node.names)
                hits.append({"line": node.lineno, "reference": f"from {node.module} import {names}"})
    return hits


def scan_sh(path: Path) -> List[dict]:
    """Regex-scan a Layer-1 .sh file for mind_api/src/ source / python -c imports.

    The daemon-launcher pattern (`python -m mind_api.src`) is whitelisted.
    """
    try:
        src = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [{"line": 0, "reference": f"<read failed: {exc}>"}]
    hits: List[dict] = []
    for m in _SH_RUNTIME_REFS.finditer(src):
        line_text = m.group(0)
        # Whitelist `python -m mind_api.src` (daemon launcher).
        if _SH_LAUNCHER_WHITELIST.search(line_text):
            continue
        lineno = src.count("\n", 0, m.start()) + 1
        hits.append({"line": lineno, "reference": line_text.strip()})
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true",
                        help="emit JSON instead of human-readable output")
    args = parser.parse_args()

    findings: List[dict] = []
    py_files = _layer1_py_files()
    sh_files = _layer1_sh_files()

    for py in py_files:
        for hit in scan_py(py):
            findings.append({
                "file": str(py.relative_to(PROJECT_ROOT)).replace("\\", "/"),
                "kind": "python",
                **hit,
            })
    for sh in sh_files:
        for hit in scan_sh(sh):
            findings.append({
                "file": str(sh.relative_to(PROJECT_ROOT)).replace("\\", "/"),
                "kind": "shell",
                **hit,
            })

    if args.json:
        print(json.dumps({
            "ok": len(findings) == 0,
            "scanned_py": len(py_files),
            "scanned_sh": len(sh_files),
            "violations": findings,
        }, indent=2))
    else:
        if findings:
            print(f"GATE FAILED: Layer-1 -> Layer-2 reference count = {len(findings)}",
                  file=sys.stderr)
            for f in findings:
                print(f"  {f['file']}:{f['line']} [{f['kind']}]  {f['reference']}",
                      file=sys.stderr)
        else:
            print(f"GATE OK: scanned {len(py_files)} Layer-1 .py + "
                  f"{len(sh_files)} Layer-1 .sh; Layer-1 -> Layer-2 references = 0")
    return 0 if not findings else 1


if __name__ == "__main__":
    sys.exit(main())
