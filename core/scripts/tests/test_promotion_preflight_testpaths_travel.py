"""Every pytest.ini testpath travels with the code it tests.

promotion-preflight.FRAMEWORK_PATHS is not only the drift-check surface: it is
also the COPY SET of framework_pull.py (read, not forked -- see
`test_framework_paths_are_read_from_preflight_not_forked`). A testpath missing
from it means a pull-adoption updates a module and leaves its tests at the
prior tag, so the downstream's post-adopt suite fails on a fixture the
adoption never delivered. Measured 2026-09-02: core/tests/gates was the missing
one -- an adoption shipped the aspiration-supply gate's `min_goals` check while
its test file stayed at the previous release, and the pinned verify run
reported one "new" failure that was pure delivery, not code.

The invariant is derived from pytest.ini rather than hardcoded so a future
testpath joins the check by being declared, with no second source of truth.
"""
from __future__ import annotations

import configparser
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "core" / "scripts"


def _preflight():
    spec = importlib.util.spec_from_file_location("_preflight_tp", SCRIPTS / "promotion-preflight.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _testpaths() -> list:
    cp = configparser.ConfigParser()
    cp.read(ROOT / "pytest.ini", encoding="utf-8")
    raw = cp.get("pytest", "testpaths", fallback="")
    return [t.strip().rstrip("/") for t in raw.split() if t.strip()]


def _covered(path: str, members: list) -> bool:
    return any(path == m or path.startswith(m.rstrip("/") + "/") for m in members)


def test_pytest_ini_declares_the_three_testpaths():
    paths = _testpaths()
    assert "core/tests/gates" in paths, paths
    assert len(paths) >= 3, paths


def test_every_declared_testpath_is_in_the_promotion_copy_set():
    members = list(_preflight().FRAMEWORK_PATHS)
    missing = [p for p in _testpaths() if not _covered(p, members)]
    assert not missing, (
        f"pytest.ini testpaths not covered by FRAMEWORK_PATHS: {missing} -- a "
        f"pull-adoption would ship code without the tests that verify it"
    )


def test_framework_pull_copy_set_carries_core_tests():
    sys.path.insert(0, str(SCRIPTS))
    import framework_pull as fp  # noqa: E402
    assert "core/tests" in fp.framework_paths(SCRIPTS)
