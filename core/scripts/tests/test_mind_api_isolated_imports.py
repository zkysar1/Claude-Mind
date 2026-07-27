"""test_mind_api_isolated_imports.py — regression guard for rb-3868 / .

Import-order class: a mind_api/src/** module that imports a core/scripts
sibling module (_gate_log, _fileops, _team_state, ...) BEFORE the
sys.path-installing relative import (`from .. import file_locks` — the
installer lives at mind_api/src/file_locks.py:33-34) raises
ModuleNotFoundError on any ISOLATED import — pytest collecting the module,
importlib in a test, a fresh REPL. The running daemon MASKS the bug by
load-order luck (another module loads file_locks first), making it a latent
production-bug class invisible until something imports the module alone.

Incidents: g-115-2480 (strategy_apply.py), g-115-2517 sweep (team_state.py +
team_state_write.py — both fixed in the same change that added this test).

Mechanism — one subprocess batch, per-module isolation: a single fresh
interpreter enumerates every mind_api/src/**/*.py module and imports each
after (a) resetting sys.path to the pristine snapshot and (b) purging every
repo-local cached module (name starts with "mind_api" or __file__ under the
repo root). Third-party deps stay cached for speed — the regression class
only needs a pristine sys.path and no cached repo modules. Measured ~0.1s
for 60 modules, so the guard is effectively free.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

_DRIVER = r"""
import importlib, json, sys
from pathlib import Path
root = Path.cwd()
sys.path.insert(0, str(root))
pristine_path = list(sys.path)
pristine_modules = set(sys.modules.keys())
root_str = str(root)

def purge_repo_local():
    sys.path[:] = list(pristine_path)
    for name in list(sys.modules.keys()):
        if name in pristine_modules:
            continue
        mod = sys.modules.get(name)
        f = getattr(mod, "__file__", None) or ""
        if name.startswith("mind_api") or (f and f.startswith(root_str)):
            del sys.modules[name]

src = root / "mind_api" / "src"
mods = []
for p in sorted(src.rglob("*.py")):
    if "__pycache__" in p.parts:
        continue
    rel = p.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    mods.append(".".join(parts))

failures = []
for m in mods:
    purge_repo_local()
    try:
        importlib.import_module(m)
    except Exception as e:
        failures.append({"module": m, "error": f"{type(e).__name__}: {e}"})
print(json.dumps({"total": len(mods), "failures": failures}))
"""


def test_every_mind_api_module_imports_in_isolation():
    """Each mind_api/src module must import with no cached repo modules.

    A failure here almost always means a core/scripts import placed BEFORE
    the `from .. import file_locks` relative import — move it after (see
    strategy_apply.py's import block for the canonical pattern).
    """
    # Full env inherit + explicit backend pin: the pin alone satisfies
    # guard-955 (import side effects must never touch the production store),
    # while inheritance keeps Windows subprocesses viable (SystemRoot/PATH —
    # a POSIX-only minimal env broke them; fresh-eyes finding
    # zeta-fec-minimal-env-windows-hostile-202607172150).
    env = {**os.environ, "STORAGE_BACKEND": "local"}
    proc = subprocess.run(
        [sys.executable, "-c", _DRIVER],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"isolated-import driver crashed (rc={proc.returncode}):\n"
        f"stdout={proc.stdout[-2000:]}\nstderr={proc.stderr[-2000:]}"
    )
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    # Sanity floor: the sweep must actually enumerate the package (60 modules
    # at time of writing) — a near-zero total means the glob broke, not that
    # the package shrank.
    assert result["total"] >= 30, (
        f"suspiciously few modules enumerated ({result['total']}) — "
        "driver glob likely broken (empty-is-not-evidence, guard-1079)"
    )
    assert result["failures"] == [], (
        "isolated-import regressions (rb-3868 class — core/scripts import "
        "before the sys.path-installing `from .. import file_locks`):\n"
        + json.dumps(result["failures"], indent=2)
    )
