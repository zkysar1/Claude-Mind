"""Coverage for meta-imports-world-gate.py — one of the 4 pre-commit gates that
had NO test file anywhere (g-115-4399, re-measured 2026-08-29: still 4, now of 13).

WHY A GATE ABOVE ALL ELSE NEEDS A TEST. A gate's entire job is to REFUSE. An
untested gate that silently stops refusing is byte-indistinguishable from a clean
repo — the failure mode produces exactly the output success produces. g-306-105
put a real defect INTO a gate (exit code stranded in the wrong branch, so audit
mode printed a full findings report and returned 0) and it was caught only because
someone ran a positive control by hand. See rb-6205, guard-5501.

So every test here is an INDUCED FAULT: plant a violation, prove the gate SEES it.
The clean-file case alone would pass against a gate that can no longer detect
anything.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parents[1] / "meta-imports-world-gate.py"


def _load():
    spec = importlib.util.spec_from_file_location("miwg", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gate():
    return _load()


def test_the_gate_module_loads_at_all(gate):
    """Guards the cheapest way this gate could die: a syntax/import error making
    every downstream assertion vacuous."""
    assert hasattr(gate, "scan_file")


@pytest.mark.parametrize(
    "src,label",
    [
        ("from mind_api.src.world import environments\n", "from-import, package-qualified"),
        ("import mind_api.src.world.environments\n", "plain import, package-qualified"),
        ("from ..world import environments\n", "relative form used inside the package tree"),
    ],
)
def test_induced_violation_is_detected(gate, tmp_path, src, label):
    """POSITIVE CONTROL. Each is a shape the docstring claims to catch; a gate that
    silently stopped refusing fails exactly here and nowhere else."""
    f = tmp_path / "offender.py"
    f.write_text(src, encoding="utf-8")
    found = gate.scan_file(f)
    assert found, f"gate did NOT detect a violation it claims to catch: {label}"


def test_clean_file_is_not_flagged(gate, tmp_path):
    """NEGATIVE CONTROL — meaningful only beside the positive ones above."""
    f = tmp_path / "clean.py"
    f.write_text("from mind_api.src.meta import strategies\nimport json\n", encoding="utf-8")
    assert gate.scan_file(f) == []


def test_commented_and_stringified_mentions_do_not_false_positive(gate, tmp_path):
    """The docstring's stated reason for AST-scanning instead of grepping. If this
    regresses to a grep the gate becomes noisy and gets disabled — the failure mode
    that actually retires gates in practice."""
    f = tmp_path / "mentions.py"
    f.write_text(
        '# from mind_api.src.world import environments\n'
        'DOC = "from mind_api.src.world import environments"\n',
        encoding="utf-8",
    )
    assert gate.scan_file(f) == []
