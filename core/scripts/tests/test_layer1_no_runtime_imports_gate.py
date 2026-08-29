"""Coverage for layer1-no-runtime-imports-gate.py — 2nd of the 4 pre-commit gates
with no test anywhere (g-115-4399; re-measured 2026-08-29: 4 uncovered of 13).

A gate's job is to REFUSE, so a gate that silently stops refusing produces exactly
what a clean repo produces. Clean-path assertions therefore pass against a totally
dead gate and prove nothing on their own. Every positive case here is an INDUCED
FAULT. See guard-5501, rb-6205, and g-306-105 (a real defect introduced INTO a gate,
caught only by a hand-run positive control).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parents[1] / "layer1-no-runtime-imports-gate.py"


@pytest.fixture(scope="module")
def gate():
    spec = importlib.util.spec_from_file_location("l1nrig", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_module_loads_and_exposes_both_scanners(gate):
    assert hasattr(gate, "scan_py") and hasattr(gate, "scan_sh")


@pytest.mark.parametrize(
    "src,label",
    [
        ("import mind_api.src.world\n", "absolute import"),
        ("from mind_api.src import world\n", "from-import"),
    ],
)
def test_py_violation_detected(gate, tmp_path, src, label):
    """POSITIVE CONTROL — clause (a) of the gate's documented contract."""
    f = tmp_path / "l1.py"
    f.write_text(src, encoding="utf-8")
    assert gate.scan_py(f), f"gate missed a violation it claims to catch: {label}"


def test_sh_source_violation_detected(gate, tmp_path):
    """POSITIVE CONTROL — clause (b): sourcing Layer-2 from a Layer-1 wrapper."""
    f = tmp_path / "l1.sh"
    f.write_text('source "$REPO/mind_api/src/helper.sh"\n', encoding="utf-8")
    assert gate.scan_sh(f)


def test_sh_python_dash_c_violation_detected(gate, tmp_path):
    """POSITIVE CONTROL — clause (c): smuggling the import through `python -c`."""
    f = tmp_path / "l1.sh"
    f.write_text('python -c "import mind_api.src.world"\n', encoding="utf-8")
    assert gate.scan_sh(f)


def test_daemon_launcher_is_whitelisted(gate, tmp_path):
    """THE DISCRIMINATION BOUNDARY, and the only case where a miss is a FALSE
    ALARM rather than a silent pass. `python -m mind_api.src` is HOW Layer 1
    starts the daemon -- not a code dependency on it. A gate that flags this
    blocks the launcher and gets disabled, which is how gates die in practice.
    Note it sits one character from clause (c): -m is allowed, -c is not."""
    f = tmp_path / "mind-api-start.sh"
    f.write_text('python -m mind_api.src\n', encoding="utf-8")
    assert gate.scan_sh(f) == []


def test_clean_layer1_files_are_not_flagged(gate, tmp_path):
    """NEGATIVE CONTROL — meaningful only beside the positives above."""
    p = tmp_path / "clean.py"
    p.write_text("import json\nfrom pathlib import Path\n", encoding="utf-8")
    s = tmp_path / "clean.sh"
    s.write_text('source "$REPO/core/scripts/_paths.sh"\n', encoding="utf-8")
    assert gate.scan_py(p) == []
    assert gate.scan_sh(s) == []
