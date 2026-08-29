"""test_daemon_only_module_refuses_as_script.py -- a daemon-side library run as a script
refuses loudly and names its wrapper (2026-08-29).

Measured on a downstream deployment (eight Bodies on a small local model): a Body ran
`python3 core/scripts/reasoning-bank.py add --entry ...`, got rc=0 and NO output, and
read the silence as "added" -- nothing was written. `retrieve.py --category x` had the
same shape. The CLI subcommands were deliberately removed (H2 Wave 2, 2026-05-15;
no-python-cli-fallback.md); the module must still not look like it worked. This is
a REFUSAL, not a fallback: no store is touched, rc=2, stderr names the wrapper.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "core" / "scripts"


@pytest.mark.parametrize(
    "module, args, wrapper",
    [
        ("reasoning-bank.py", ["add", "--entry", "x"], "reasoning-bank-add.sh"),
        ("retrieve.py", ["--category", "x"], "retrieve.sh"),
    ],
)
def test_the_module_run_as_a_script_refuses_and_names_the_wrapper(module, args, wrapper):
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / module), *args],
        capture_output=True,
        text=True,
        timeout=60,
        env={"STORAGE_BACKEND": "local", "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.stdout == ""
    assert wrapper in proc.stderr
    assert "not a command" in proc.stderr


def test_the_refusal_is_a_main_guard_not_an_import_side_effect():
    """Importers (guardrail-check.py, board.py, tests) must keep working: the refusal
    lives under `if __name__ == "__main__":` only."""
    for module in ("reasoning-bank.py", "retrieve.py"):
        src = (SCRIPTS / module).read_text(encoding="utf-8")
        assert 'if __name__ == "__main__":' in src, module
        # The refusal's sys.exit is inside the guard: it must come after it.
        assert src.index('if __name__ == "__main__":') < src.rindex("sys.exit(2)"), module
