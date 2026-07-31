"""test_capability_gate_row_prose_bound.py — structural regression for .

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
    _load_skill_md_triggers,
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


# ── Contract 4: the SAME bound, on the OTHER entry source ────────────────────
# . `_load_capability_routing` is one of two entry producers; the
# other is `_load_skill_md_triggers`, and it has the identical exposure. A
# SKILL.md carries BOTH `triggers` (a precision surface, deliberately narrow —
# guard-1892 requires hyphenated tokens because a 2-token prose overlap is
# never demoted) and `description` (a RECALL surface, deliberately full of
# ordinary English, because it is what the runtime loads to decide whether to
# fire the skill at all). Those two want opposite optimizations, and the only
# thing that lets both live in one file is that the gate reads triggers and
# NOT description.
#
# Nothing pinned that. The tests above bound the routing-table prose; the
# skill-side prose was bounded only by what `_load_skill_md_triggers` chooses
# to copy out of the parsed front matter. `description` IS available to it —
# measured, not assumed: parse_front_matter returns keys
# ['description', 'name', 'triggers'] with the full value. So these tests pin a
# real choice (the loader declines an available field), not an accident of the
# parser never surfacing it.
#
# BE PRECISE ABOUT WHICH EDIT REGRESSES, because the obvious guess is wrong and
# this comment asserted it for ~15 minutes before a fresh-eyes probe corrected
# it. Adding a bare `"description": fm.get("description")` to the entry dict
# leaks NOTHING and these tests would still pass — correctly, because
# `_entry_tokens` reads only `skill` / `triggers` / `scripts` / `match_text` /
# `row` and has no `description` branch. The regression requires the prose to
# land in one of THOSE five: appended to `triggers`, or routed into
# `match_text` / `row` (e.g. by someone building a "richer match surface").
# Each of those leaks all six watched tokens. Measured on the descriptions that
# exist today: they are long and dense with exactly the words that collide
# ("registry", "config", "index", "audit", "state", "server"), so the blast
# radius is every skill at once — contract 2's failure class at fleet scale
# rather than per-row.
#
# guard-2142 is the behavioral half (widen a description, never triggers, when
# discovery undertriggers); these two tests are the structural half that keeps
# that advice safe to follow.
#
# Mutation-proofed at authoring time (guard-1220 / guard-1793 — an assertion is
# not a guard until something is shown to break it). Four forms run against
# `_entry_tokens` directly: description appended to `triggers`, routed into
# `match_text`, routed into `row` — all three leak ALL SIX watched tokens — and
# a bare `description` key, which leaks NONE (the negative control that
# corrected the paragraph above). The clean entry leaks none. So the assertion
# is decisive on the three real shapes rather than vacuously true of any entry.
_SKILL_MD = """---
name: widget-reconciler
description: "{desc}"
triggers:
  - "widget-reconcile"
---

# widget-reconciler
Body prose is not front matter and is never parsed into an entry.
"""

# Ordinary English chosen to overlap with real capability rows. If the
# description ever reaches the match surface, these are the tokens that would
# produce a false block, so they are the ones worth naming.
_DESC_PROSE = (
    "Use whenever a registry disagrees with the reality it indexes, or an "
    "index disagrees with a config, or a server state needs an audit. "
)


def _skill_entry(tmp_path: Path, desc: str):
    skills = tmp_path / "skills"
    (skills / "widget-reconciler").mkdir(parents=True, exist_ok=True)
    (skills / "widget-reconciler" / "SKILL.md").write_text(
        _SKILL_MD.format(desc=desc), encoding="utf-8"
    )
    entries = _load_skill_md_triggers(skills)
    assert len(entries) == 1, f"expected 1 skill entry, got {len(entries)}: {entries}"
    return entries[0]


def test_skill_description_prose_is_excluded_from_match_surface(tmp_path):
    """Contract 4a — a skill's `description` must not reach the match surface,
    while its name and `triggers` must. The recall half is asserted in the same
    test on purpose: an exclusion that also drops the triggers would pass a
    leak-only assertion while destroying the matcher."""
    entry = _skill_entry(tmp_path, _DESC_PROSE)
    toks = _entry_tokens(entry)

    leaked = {"registry", "reality", "indexes", "config", "audit", "disagrees"} & toks
    assert not leaked, (
        f"skill description prose leaked into the match surface: {sorted(leaked)}; "
        f"tokens={sorted(toks)}"
    )
    assert "widget-reconcile" in toks, (
        f"the trigger was dropped — exclusion is over-broad and the matcher is "
        f"now blind; tokens={sorted(toks)}"
    )
    # The skill NAME survives as the whole hyphenated token, not as its parts:
    # the tokenizer keeps compounds intact rather than splitting on the hyphen.
    # Asserting the bare word "widget" here FAILED on first run — a useful
    # reminder that the retention shape differs from the routing table's, where
    # cell-0 words like "State replay" are already separate words and so are
    # retained individually (see test_identifier_cell_words_are_retained).
    assert "widget-reconciler" in toks, (
        f"the skill name was dropped; tokens={sorted(toks)}"
    )


def test_growing_a_skill_description_does_not_change_match_surface(tmp_path):
    """Contract 4b — the durability property, mirroring contract 3. Descriptions
    grow (this one grew by ~700 chars in g-115-3932 to fix an undertriggering
    discovery surface). That growth must be free: byte-identical match surface,
    however long the prose gets."""
    base = _entry_tokens(_skill_entry(tmp_path / "a", _DESC_PROSE))
    grown_desc = _DESC_PROSE * 9
    grown = _entry_tokens(_skill_entry(tmp_path / "b", grown_desc))
    assert len(grown_desc) > 1000, "fixture description too short to be a real growth test"
    assert base == grown, (
        "growing the description changed the match surface — description length "
        f"is now a matcher input. added={sorted(grown - base)} "
        f"lost={sorted(base - grown)}"
    )
