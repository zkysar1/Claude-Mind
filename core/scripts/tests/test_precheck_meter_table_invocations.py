"""test_precheck_meter_table_invocations.py —  regression pin.

The aspirations-precheck meter table is the compact thing agents read before
running the sweep battery, and until 2026-08-14 it carried only sweep NAMES.
Four of ~30 names did not resolve to a runnable command (subcommand required,
sweep-name ≠ script-name for both reclaim lanes, .py-only script), and every
such failure prints to stderr and exits non-zero — which a batched
`2>/dev/null` loop renders as empty stdout, indistinguishable from a clean
lane. The fix added an Invocation column sourced from the phase bodies.

This test keeps that column honest: every core/scripts path a row names must
EXIST on disk, every row must carry a non-empty invocation, and the five
previously-broken rows must keep their specific fix markers. Static checks
only — running the sweeps belongs to the loop, not the suite.
"""

from __future__ import annotations

import re
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
SKILL_MD = PROJECT_ROOT / ".claude" / "skills" / "aspirations-precheck" / "SKILL.md"

HEADER = "| Phase | Sweep name (for `meter check`) | Tier | Invocation (exact) |"


def _table_rows():
    lines = SKILL_MD.read_text(encoding="utf-8").splitlines()
    try:
        start = lines.index(HEADER)
    except ValueError:
        raise AssertionError(
            "meter-table header with Invocation column not found in "
            f"{SKILL_MD} — the g-115-6207 column was removed or renamed")
    rows = []
    for line in lines[start + 2:]:  # skip separator row
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 4:
            rows.append(cells)
    return rows


def test_table_present_with_expected_population():
    rows = _table_rows()
    # 39 rows at fix time; future additions are fine, deletions are not.
    assert len(rows) >= 39, f"meter table shrank to {len(rows)} rows"
    names = [r[1] for r in rows]
    for required in ("tree-debt-gate", "precheck-eval", "pending-questions-sweep",
                     "reclaim-defer-audit", "reclaim-user-participant-audit",
                     "recurring-precondition-sweep", "evolution-cadence"):
        assert required in names, f"expected sweep name missing: {required}"


def test_every_row_has_nonempty_invocation():
    for cells in _table_rows():
        assert cells[3], f"row {cells[1]} has an empty Invocation cell"


def test_every_referenced_script_exists():
    """Every core/scripts/<x> path named in an Invocation cell must exist —
    this is the check whose absence let 'reclaim-defer-audit' (no such
    script) sit in the table as if runnable."""
    pat = re.compile(r"core/scripts/[A-Za-z0-9._-]+\.(?:sh|py)")
    checked = 0
    for cells in _table_rows():
        for ref in pat.findall(cells[3]):
            assert (PROJECT_ROOT / ref).is_file(), (
                f"row {cells[1]} names {ref}, which does not exist")
            checked += 1
    assert checked >= 25, f"only {checked} script refs found — table gutted?"


def test_every_row_is_script_or_battery():
    """A row must either name a concrete script or declare battery dispatch —
    a bare name with neither is exactly the pre-fix defect."""
    pat = re.compile(r"core/scripts/[A-Za-z0-9._-]+\.(?:sh|py)")
    for cells in _table_rows():
        inv = cells[3]
        assert pat.search(inv) or "battery" in inv.lower(), (
            f"row {cells[1]} invocation neither names a script nor a battery: "
            f"{inv[:80]}")


def test_previously_broken_rows_keep_their_fix_markers():
    """The five measured mis-invocations () — each row must keep
    the specific detail whose absence broke it."""
    rows = {r[1]: r[3] for r in _table_rows()}
    assert "run-all" in rows["precheck-eval"], "precheck-eval lost its subcommand"
    assert re.search(r"pending-questions-sweep\.sh sweep\b",
                     rows["pending-questions-sweep"]), \
        "pending-questions-sweep lost its REQUIRED 'sweep' subcommand"
    assert "audit-deferred-defers.sh" in rows["reclaim-defer-audit"], \
        "reclaim-defer-audit no longer maps to its real script"
    assert "audit-user-to-agent.sh" in rows["reclaim-user-participant-audit"], \
        "reclaim-user-participant-audit no longer maps to its real script"
    assert "recurring-precondition-sweep.py" in rows["recurring-precondition-sweep"], \
        "recurring-precondition-sweep must point at the .py (no .sh exists)"
    # And the .sh really must not exist — if someone adds it, this row's
    # parenthetical becomes false and should be updated (loud, cheap).
    assert not (CORE_SCRIPTS / "recurring-precondition-sweep.sh").exists(), \
        ".sh wrapper now exists — update the table row's '(.py ONLY)' note"


def test_battery_scripts_exist():
    for battery in ("precheck-sentinel-battery.sh", "precheck-cadence-battery.sh"):
        assert (CORE_SCRIPTS / battery).is_file(), f"{battery} missing"
