"""test_capability_gate_row_prose_bound.py — structural regression for g-115-3655.

Sibling guards (test_capability_gate_table_token_noise.py, ...fence_stopwords.py)
pin SPECIFIC false-positive defers by adding the offending tokens to
`_STOPWORDS`. That defense is per-incident and buys time only until the next row
grows: rows in capability-routing.md grow monotonically because every measurement
appends provenance to the row it corrects, and at ~3200 chars of prose a row
contains enough ordinary English to collide with any defer. Two live FPs on
2026-07-28 ('never'/'registry' and 'fenced'/'since') were the second breach of
that guard.

This file pins the STRUCTURAL property instead, so the class cannot recur
regardless of which English word is next: the match surface of a row is BOUNDED
and independent of the row's prose length.

Contract asserted here:
  1. Prose-cell tokens that are identifier-shaped (containing - _ or .) ARE
     retained. Load-bearing: 11 of 20 live rows name their companion script
     ONLY in the prose cell, and that is exactly the token a real defer cites.
     Dropping the prose cell wholesale would break true-positive detection —
     the dangerous direction, and why a cell-0-only fix was measured and
     rejected.
  2. Bare English prose tokens are NOT retained.
  3. Appending arbitrary prose to a row does not change its match surface at
     all. This is the durability property: provenance can grow without bound
     and never becomes a matcher input.

Hermetic by construction — builds a synthetic convention file rather than
reading the live one, so the guard cannot drift as real rows are edited.
Placeholders are fictional per .claude/rules/domain-free-examples.md.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

from gates.capability import (  # noqa: E402
    _load_capability_routing,
    _entry_tokens,
)

_HEADER = (
    "## Agent-Provisionable Services\n"
    "\n"
    "| Capability | Notes |\n"
    "|---|---|\n"
)

# Cell 0 is the bounded capability identifier; cell 1 carries a companion
# script (identifier-shaped) embedded in ordinary English narration.
_ROW = (
    "| Widget service (port 9999) "
    "| Start it with `widget-run.sh` when the queue stalls. "
    "This never fails and the registry stays complete. |"
)

# Pure English, no hyphens/underscores/dotted abbreviations, so it contributes
# no identifier-shaped tokens. Stands in for the provenance paragraphs that
# real rows accumulate.
_PROSE = (
    "The identity differs by host so provisionable is not a property of this "
    "table. Reads from the same host succeed which means a denial here is a "
    "boundary and never an outage. Do not misread it as a dead credential. "
) * 12


def _rows_for(tmp_path: Path, row: str):
    conv = tmp_path / "conventions"
    conv.mkdir(parents=True, exist_ok=True)
    (conv / "capability-routing.md").write_text(
        _HEADER + row + "\n", encoding="utf-8"
    )
    return _load_capability_routing(tmp_path)


def test_prose_cell_script_identifier_is_retained(tmp_path):
    """Contract 1 — a companion script named ONLY in the prose cell must stay
    matchable. This is what a cell-0-only fix would have silently broken."""
    rows = _rows_for(tmp_path, _ROW)
    assert len(rows) == 1, f"expected 1 parsed row, got {len(rows)}: {rows}"
    toks = _entry_tokens(rows[0])
    assert "widget-run.sh" in toks, (
        f"prose-cell script identifier was dropped from the match surface; "
        f"tokens={sorted(toks)}"
    )


def test_bare_prose_words_are_excluded(tmp_path):
    """Contract 2 — the ordinary English that caused both live FPs must not
    reach the match surface. 'never' and 'registry' are the exact tokens that
    produced the 2026-07-28 false block."""
    rows = _rows_for(tmp_path, _ROW)
    toks = _entry_tokens(rows[0])
    leaked = {"never", "fails", "registry", "complete", "stalls"} & toks
    assert not leaked, (
        f"bare prose words leaked into the match surface: {sorted(leaked)}; "
        f"tokens={sorted(toks)}"
    )


def test_appending_prose_does_not_change_match_surface(tmp_path):
    """Contract 3 — THE durability property. A row that grows by ~1000 chars of
    provenance must have a byte-identical match surface. Without this, every
    future row-growth reopens the FP class and the only defense is another
    stopword."""
    base = _entry_tokens(_rows_for(tmp_path / "a", _ROW)[0])
    grown_row = _ROW[:-1].rstrip() + " " + _PROSE + "|"
    grown = _entry_tokens(_rows_for(tmp_path / "b", grown_row)[0])
    assert len(_PROSE) > 1000, "fixture prose too short to be a real growth test"
    assert base == grown, (
        "appending prose changed the match surface — row length is still a "
        f"matcher input. added={sorted(grown - base)} lost={sorted(base - grown)}"
    )


def test_identifier_cell_words_are_retained(tmp_path):
    """Recall control adjacent to contract 2: excluding prose must not also
    exclude cell 0's bare words, which ARE the capability's discriminators
    ('Deployments', 'State replay' carry no compound token at all)."""
    rows = _rows_for(tmp_path, "| State replay | Narration that never matters. |")
    toks = _entry_tokens(rows[0])
    assert {"state", "replay"} <= toks, (
        f"identifier-cell words were dropped; tokens={sorted(toks)}"
    )
