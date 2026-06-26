"""budget-meter sweep_tier() <-> aspirations-precheck SKILL.md tier-table parity
(g-303-21, zeta allowlist audit site 6a).

aspirations-precheck-budget-meter.sh:sweep_tier() classifies each precheck sweep
into always-run / medium / deferrable; the authoritative human-readable list is
the tier table in aspirations-precheck/SKILL.md Step 0a. The audit flagged 6a
rot-risk MED-HIGH: a sweep added to the SKILL.md table without a sweep_tier()
case hits the WARN-on-unknown default (medium) instead of its real tier -- and
the pre-existing verify-learning PB checks only SPOT-CHECK a handful of specific
sweeps (each with its own mini-hardcoded list), never the full set. This is the
full-parity enforcement (path ii): the two sites' (sweep -> tier) maps must be
identical, in both directions.

Pure-text parse of both files -- no subprocess, no sourcing -- so it is hermetic
and cannot hang on the Windows python->bash->python path (rb-225/rb-247).
"""
from __future__ import annotations

import re
from pathlib import Path


def _find_repo_root() -> Path:
    here = Path(__file__).resolve()
    needed = (
        ("core", "scripts", "aspirations-precheck-budget-meter.sh"),
        (".claude", "skills", "aspirations-precheck", "SKILL.md"),
    )
    for anc in [here] + list(here.parents):
        if all((anc.joinpath(*p)).exists() for p in needed):
            return anc
    raise RuntimeError("repo root not found (6a parity anchors missing)")


REPO = _find_repo_root()
METER_SH = REPO / "core" / "scripts" / "aspirations-precheck-budget-meter.sh"
SKILL = REPO / ".claude" / "skills" / "aspirations-precheck" / "SKILL.md"
_TIERS = {"always-run", "medium", "deferrable"}


def _sweep_tier_map() -> dict:
    """Parse sweep_tier()'s case statement: each ``name|name|...)`` arm and the
    ``echo "<tier>"`` that follows. Skips the ``*)`` default arm."""
    text = METER_SH.read_text(encoding="utf-8")
    m = re.search(r"sweep_tier\(\)\s*\{(.*?)\n\}", text, re.DOTALL)
    assert m, "sweep_tier() function body not found in budget-meter.sh"
    body = m.group(1)
    out: dict = {}
    pending: list = []
    for line in body.splitlines():
        s = line.strip()
        cm = re.match(r"^([a-z0-9][a-z0-9|\-]*)\)\s*$", s)
        if cm and cm.group(1) != "*":
            pending = cm.group(1).split("|")
            continue
        em = re.match(r'^echo\s+"([a-z-]+)"', s)
        if em and pending:
            tier = em.group(1)
            if tier in _TIERS:
                for name in pending:
                    out[name] = tier
            pending = []
    return out


def _skill_table_map() -> dict:
    """Parse the markdown tier-table rows: ``| <phase> | <sweep> | <tier> |``.
    The tier-valued 3rd column (always-run|medium|deferrable) is what isolates
    THIS table from any other 3-column markdown table in the SKILL.md."""
    out: dict = {}
    row = re.compile(
        r"^\|[^|]*\|\s*([a-z0-9][a-z0-9\-]*)\s*\|\s*(always-run|medium|deferrable)\s*\|"
    )
    for line in SKILL.read_text(encoding="utf-8").splitlines():
        m = row.match(line.strip())
        if m:
            out[m.group(1)] = m.group(2)
    return out


def test_sweep_tier_matches_skill_table():
    meter = _sweep_tier_map()
    table = _skill_table_map()
    assert meter, "parsed no sweeps from sweep_tier() -- the .sh parser broke"
    assert table, "parsed no rows from the SKILL.md tier table -- the md parser broke"

    # Forward: every SKILL.md table sweep is classified by sweep_tier() with the
    # SAME tier (a table sweep missing from sweep_tier would hit WARN-default).
    table_unmatched = {k: v for k, v in table.items() if meter.get(k) != v}
    assert not table_unmatched, (
        f"SKILL.md tier-table sweep(s) not matched by sweep_tier() "
        f"(would hit the WARN-default medium): {table_unmatched}. Add/fix the "
        f"case arm (allowlist rot class; g-303-21 / zeta audit 6a)."
    )

    # Reverse: no sweep_tier() arm is absent from the table (the other drift dir).
    meter_extra = {k: v for k, v in meter.items() if k not in table}
    assert not meter_extra, (
        f"sweep_tier() classifies sweep(s) absent from the SKILL.md tier table: "
        f"{meter_extra}. Add the table row or remove the stale case "
        f"(g-303-21 / zeta audit 6a)."
    )
