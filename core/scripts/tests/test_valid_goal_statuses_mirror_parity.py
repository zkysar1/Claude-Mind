""" (B1) — VALID_GOAL_STATUSES lives in THREE files, and two are refusal gates.

WHY THIS TEST EXISTS. ``aspirations_write.py`` carried the comment "Mirror upstream when
these change; a parity test could enforce" from the day it was written. Nobody built it, so
for the whole life of that comment the only thing keeping three copies in sync was a reader
noticing the sentence. The sibling ``test_terminal_goal_states_parity.py`` guards the
TERMINAL set across its own mirrors; nothing guarded the VALID set.

THE FAILURE MODE IS ASYMMETRIC, which is what makes a drift here expensive. The SSOT is
permissive and the two daemon copies are REFUSAL GATES
(``aspirations_write.py`` rejects the write, ``aspirations_query.py`` rejects
``--goal-status <value>``). So a value present upstream but missing in a mirror does not
degrade gracefully — the daemon refuses the operation outright while the CLI accepts it, and
the two disagree about what a legal goal is. Measured before the B1 landing:
``aspirations-query.sh --goal-status candidate`` returned
``{"error": "invalid_goal_status"}`` while the SSOT had already been extended.

⚠ DO NOT add ``candidate`` to ``test_terminal_goal_states_parity.py``. That file guards the
TERMINAL mirrors; putting ``candidate`` there would make it terminal in three sweeps and
invert invariant I7 of ``world/conventions/goal-intake-management.md`` §2 — **with the suite
staying green**, because the terminal set would then be a subset of the valid set and every
existing assertion would still hold. That is the trap the g-353-62 adversarial review
measured, and it is why this is a separate file.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# __file__ is core/scripts/tests/<this>.py — four parents to the repo root.
REPO = Path(__file__).resolve().parents[3]
SSOT_FILE = REPO / "core" / "scripts" / "aspirations.py"

# (path, constant name) for each mirror that must equal the SSOT exactly.
COPIES = [
    (REPO / "mind_api" / "src" / "endpoints" / "aspirations_write.py", "_VALID_GOAL_STATUSES"),
    (REPO / "mind_api" / "src" / "endpoints" / "aspirations_query.py", "VALID_GOAL_STATUSES"),
]


def _module_assign_literal(path: Path, name: str):
    """Read a module-level set/list literal WITHOUT importing the module.

    Import is not an option here: the two mirrors live under ``mind_api.src`` and pull the
    daemon's package graph. AST also means comments inside the literal are invisible, so the
    per-member annotations in each copy cannot affect parity.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"module-level assignment {name!r} not found in {path.name}")


def _ssot() -> set:
    return set(_module_assign_literal(SSOT_FILE, "VALID_GOAL_STATUSES"))


@pytest.mark.parametrize("path,constname", COPIES, ids=lambda v: getattr(v, "name", v))
def test_mirror_matches_ssot(path: Path, constname: str):
    """Full-set equality, both directions.

    Not membership: a mirror carrying an EXTRA value is equally broken — it would accept a
    write the SSOT rejects, so the store could receive a status no CLI consumer validates.
    """
    mirror = set(_module_assign_literal(path, constname))
    ssot = _ssot()
    assert mirror == ssot, (
        f"{path.name}::{constname} has drifted from aspirations.py::VALID_GOAL_STATUSES.\n"
        f"  missing from mirror (SSOT accepts, daemon would REFUSE): {sorted(ssot - mirror)}\n"
        f"  extra in mirror (daemon would ACCEPT, CLI rejects):      {sorted(mirror - ssot)}"
    )


def test_candidate_is_valid_everywhere():
    """Anchor the specific B1 landing in all three files at once."""
    assert "candidate" in _ssot(), "candidate must be a valid goal status (g-353-63)"
    for path, constname in COPIES:
        assert "candidate" in set(_module_assign_literal(path, constname)), (
            f"{path.name}::{constname} is a refusal gate and does not carry `candidate` — "
            "the daemon will reject candidate writes/queries (g-353-63)"
        )


def test_candidate_is_not_terminal():
    """The invariant the whole increment rests on (§2: candidate is NOT terminal).

    A candidate is filed-but-not-groomed: it must count as REMAINING work in every
    non-terminal tally, so making it terminal would silently mark intake as resolved.
    """
    terminal = set(_module_assign_literal(SSOT_FILE, "TERMINAL_GOAL_STATUSES"))
    assert "candidate" not in terminal, (
        "candidate must NOT be terminal — see goal-intake-management.md §2 invariant I7 "
        "and the *** DO NOT add candidate to test_terminal_goal_states_parity.py *** warning"
    )
    # And it must genuinely be in the valid set, or the assertion above is vacuous.
    assert "candidate" in _ssot()


def test_terminal_is_a_subset_of_valid():
    """Guards the direction that would otherwise make this whole file vacuous.

    If a status were terminal without being valid, every membership assertion here could
    pass while the engine held an unreachable archival state.
    """
    terminal = set(_module_assign_literal(SSOT_FILE, "TERMINAL_GOAL_STATUSES"))
    assert terminal <= _ssot(), f"terminal statuses not in VALID: {sorted(terminal - _ssot())}"
