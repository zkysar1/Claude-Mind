"""Terminal goal-state allowlist parity (, zeta allowlist audit D1).

The SSOT for "which goal statuses are terminal" is
``aspirations.py:TERMINAL_GOAL_STATUSES``. Three sweep/gate scripts each
hand-copy a "no further action needed" terminal set and historically drifted
from it (audit D1, CONFIRMED active drift):

  - insight-trigger-sweep.py:TERMINAL_GOAL_STATES
  - insight-trigger-gate.py:TERMINAL_GOAL_STATES
  - unblock-parent-status-sweep.py:TERMINAL_STATES  (the mirror-source the two
    insight-trigger scripts reference; it carried the identical drift)

All three were ``{completed, skipped, superseded, archived}`` -- MISSING
``expired`` + ``decomposed`` and carrying a bogus ``archived`` (not a valid
goal status per aspirations.VALID_GOAL_STATUSES). Effect: expired/decomposed
targets were mis-classified as non-terminal and still spawned stale
Apply/Investigate/Unblock work.

This module is the PROACTIVE structural catch the audit recommends (its
reference is test_daemon_cli_mirror_parity.py, audit site 5c): it diffs each
copy's source-literal against the SSOT and FAILS on drift, so a future SSOT
change that is not mirrored -- or a re-drift of any copy -- fails a fast,
targeted test instead of silently mis-classifying goals.

Pure-ast by design (reads the source files by path; ast.literal_eval on the
module-level set literals). No import of the target modules, no daemon spawn --
so it is hermetic (needs no WORLD_DIR) and runs identically from
core/scripts/tests or mind_api/tests.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


def _find_repo_root() -> Path:
    """Walk upward for the dir holding the SSOT + all three mirror sources, so
    this test is independent of which suite directory it runs from."""
    here = Path(__file__).resolve()
    needed = (
        ("core", "scripts", "aspirations.py"),
        ("core", "scripts", "insight-trigger-sweep.py"),
        ("core", "scripts", "insight-trigger-gate.py"),
        ("core", "scripts", "unblock-parent-status-sweep.py"),
    )
    for anc in [here] + list(here.parents):
        if all((anc.joinpath(*parts)).exists() for parts in needed):
            return anc
    raise RuntimeError(
        "repo root not found (need core/scripts/aspirations.py + the three "
        "terminal-state mirror sources)"
    )


REPO_ROOT = _find_repo_root()
SCRIPTS = REPO_ROOT / "core" / "scripts"
SSOT_FILE = SCRIPTS / "aspirations.py"

# (filename, module-level constant name) for each hand-copied terminal set.
COPIES = [
    ("insight-trigger-sweep.py", "TERMINAL_GOAL_STATES"),
    ("insight-trigger-gate.py", "TERMINAL_GOAL_STATES"),
    ("unblock-parent-status-sweep.py", "TERMINAL_STATES"),
]

_RESYNC = (
    "Re-sync this copy to aspirations.TERMINAL_GOAL_STATUSES (allowlist rot "
    "class; g-303-21 / zeta audit D1)."
)


def _module_assign_literal(path: Path, name: str):
    """ast.literal_eval the value of a module-level ``name = <literal>``.

    Raises AssertionError (not a silent None) when the name is absent, so a
    rename/move of the SSOT or a copy fails loudly here rather than passing a
    parity check against nothing.
    """
    mod = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in mod.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"module-level assignment {name!r} not found in {path.name}")


def _ssot() -> set:
    return set(_module_assign_literal(SSOT_FILE, "TERMINAL_GOAL_STATUSES"))


def _valid_goal_statuses() -> set:
    return set(_module_assign_literal(SSOT_FILE, "VALID_GOAL_STATUSES"))


def test_ssot_fix_anchored():
    """Anchor the specific  fix in the SSOT itself: the two statuses
    that were missing from every copy MUST be terminal, and the bogus
    ``archived`` (never a valid goal status) MUST NOT be. Phrased as
    membership (not full-set equality) so adding a NEW terminal status later
    does not spuriously fail this anchor."""
    ssot = _ssot()
    assert "expired" in ssot, "expired must be a terminal goal status (g-303-21)"
    assert "decomposed" in ssot, "decomposed must be a terminal goal status (g-303-21)"
    assert "archived" not in ssot, "archived is not a valid goal status (audit D1)"


@pytest.mark.parametrize("filename,constname", COPIES)
def test_copy_matches_ssot(filename, constname):
    """Each hand-copied terminal set EQUALS the SSOT. Fails on any drift."""
    copy = set(_module_assign_literal(SCRIPTS / filename, constname))
    ssot = _ssot()
    assert copy == ssot, (
        f"{filename}:{constname} = {sorted(copy)} drifted from "
        f"aspirations.TERMINAL_GOAL_STATUSES = {sorted(ssot)}. {_RESYNC}"
    )


@pytest.mark.parametrize(
    "filename,constname",
    [("aspirations.py", "TERMINAL_GOAL_STATUSES")] + COPIES,
)
def test_terminal_set_has_no_invalid_status(filename, constname):
    """Every element of the SSOT and each copy is a real goal status. Catches
    the ``archived`` failure mode (a dead status that can never match
    goal['status']) dynamically -- no hardcoded value list to itself rot."""
    members = set(_module_assign_literal(SCRIPTS / filename, constname))
    valid = _valid_goal_statuses()
    invalid = members - valid
    assert not invalid, (
        f"{filename}:{constname} contains non-goal-status value(s) {sorted(invalid)}; "
        f"valid statuses are {sorted(valid)}."
    )
