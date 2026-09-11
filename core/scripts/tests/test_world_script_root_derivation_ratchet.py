"""Predicate controls for world-script-root-derivation-ratchet.py ().

guard-574: a check's regex must be validated against real line shapes before it
ships — regexes written from memory frequently false-FAIL, and a false-FAIL then
fires on every run for a non-regression. guard-329: a permanently-red check is
worse than no check.

The originally-proposed predicate for this check ("grep for BASH_SOURCE or
rev-parse --show-toplevel under the world scripts dir") matched 174 files on the
live corpus and would have shipped permanently red. These controls pin the
NARROWED predicate so a future edit cannot silently widen it back.

The negative controls are the load-bearing half: a bare relative
`source core/scripts/_paths.sh` is the SANCTIONED form (the documented calling
convention is `source core/scripts/_paths.sh && bash "$WORLD_PATH/..."` from
PROJECT_ROOT), and `SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"`
is a script legitimately locating ITSELF. Neither is the defect.
"""
import importlib.util
from pathlib import Path

import pytest

_RATCHET = (Path(__file__).resolve().parents[1]
            / "world-script-root-derivation-ratchet.py")


def _load():
    spec = importlib.util.spec_from_file_location("wsrd_ratchet", _RATCHET)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


def _is_violation(mod, line):
    """Mirror the four-condition predicate in _scan()."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return False
    return bool(
        mod._PATHS_REF.search(stripped)
        and mod._SOURCE_CMD.search(stripped)
        and not mod._HONOURS_ROOT.search(stripped)
        and mod._SELF_LOCATED.search(stripped)
    )


# Every one of these is a real shape observed in the live corpus, plus the exact
# pre-fix form from mind-seed-freshness-check.sh (the  instance).
VIOLATIONS = [
    'source "$SCRIPT_DIR/../../core/scripts/_paths.sh" 2>/dev/null',
    '  source "$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)/core/scripts/_paths.sh"',
    '. "$(dirname "$0")/../../core/scripts/_paths.sh" 2>/dev/null || true',
    'source "$ABC_DIR/../../core/scripts/_paths.sh" 2>/dev/null || true',
    '  || source "$(cd "$FOO_DIR/../.." && pwd)/core/scripts/_paths.sh"',
    '  . "$SCRIPT_DIR/../../../core/scripts/_paths.sh" 2>/dev/null || true',
]

CLEAN = [
    # honours an inherited PROJECT_ROOT — the guard-5419 prescription
    'source "$PROJECT_ROOT/core/scripts/_paths.sh"',
    'source "${PROJECT_ROOT:-$(pwd)}/core/scripts/_paths.sh"',
    # PROJECT_ROOT first, self-location only as fallback: still compliant
    'source "${PROJECT_ROOT:-$(dirname "$0")/../..}/core/scripts/_paths.sh"',
    # bare relative — the SANCTIONED calling convention, not a defect
    'source core/scripts/_paths.sh',
    'source core/scripts/_paths.sh 2>/dev/null || true',
    # comments documenting the correct invocation
    '#   source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/x.sh"',
    '# `source /opt/ayoai-mind/core/scripts/_paths.sh` - a path that exists on',
    # a script locating ITSELF — legitimate, and the reason a bare
    # BASH_SOURCE grep returns 174 files
    'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
    # sourcing something that is not the framework-root loader
    'source "$CORE_ROOT/scripts/_platform.sh"',
    # prose mentioning the file
    'echo "run: source core/scripts/_paths.sh first" >&2',
]


@pytest.mark.parametrize("line", VIOLATIONS)
def test_violations_are_detected(mod, line):
    assert _is_violation(mod, line), f"predicate MISSED a real defect: {line}"


@pytest.mark.parametrize("line", CLEAN)
def test_compliant_lines_are_not_flagged(mod, line):
    assert not _is_violation(mod, line), f"predicate FALSE-POSITIVED on: {line}"


def test_blind_run_writes_no_baseline(mod, tmp_path, monkeypatch, capsys):
    """guard-1947: BLIND is not clean, and must not seed a 0.

    A baseline of 0 seeded from a box that cannot see the corpus would make
    every box that CAN see it read REGRESSED forever.
    """
    monkeypatch.setattr(mod, "WORLD_DIR", tmp_path / "does-not-exist")

    def _explode(*a, **k):  # pragma: no cover - must never be reached
        raise AssertionError("BLIND run attempted a baseline write")

    monkeypatch.setattr(mod, "locked_modify_yaml", _explode)
    monkeypatch.setattr("sys.argv", ["ratchet"])
    assert mod.main() == 0
    assert "BLIND" in capsys.readouterr().out


def test_skip_excludes_backups_and_pycache(mod, tmp_path):
    assert mod._skip(tmp_path / "x.sh.bak-g-115-8459")
    assert mod._skip(tmp_path / "__pycache__" / "x.sh")
    assert not mod._skip(tmp_path / "tests" / "x.sh")
