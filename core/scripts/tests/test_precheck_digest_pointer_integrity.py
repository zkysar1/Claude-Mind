"""Every extracted-digest pointer must resolve, and every section must be pointed at.

g-115-6583 moved the PROSE of aspirations-precheck's 19 deferrable-tier phases
into core/config/aspirations-precheck-digest.md, leaving a one-line pointer

    ▸ Body in `core/config/aspirations-precheck-digest.md` (§ Phase 0.5b.17) — ...

The glyphs are load-bearing: POINTER below matches ▸ and § literally, so an
ASCII-ised example here would document a shape the parser rejects (probed:
POINTER.search() on the old '>' / 'SS' form returned False), and the vacuity
guard would not catch one reworded pointer -- it trips below 16 against 23.

in SKILL.md where each body used to be. That created a NEW invariant with no
guard: the pointer and the digest heading are two strings in two files that
must agree, and NOTHING enforces it.

WHY A TEST AND NOT A verify-learning GREP: the failure is SILENT in the worst
way. Rename a digest heading, or reword a pointer, and the phase body simply
becomes unreachable -- the loop reads SKILL.md, sees a phase header and a drop
branch, follows a pointer to a section that no longer exists, and executes the
phase with no body. No error, no rc, no missing file. Identical in shape to the
defect the sibling test_precheck_phase_chain.py exists for (a drop branch
pointing at a phase the parser cannot see), which sat RED for four days while
reading as a defect in an innocent phase.

MAPPING, NOT BIJECTION. Several pointers may share one digest section (today
three lanes point at one 'Phases 0.5b.19-0.5b.21' body), so the pointer side is
deliberately many-to-one. The HEADING side is not: two sections sharing an id
would collapse in the set() below and go invisible while both checks stayed
green, so it gets its own assertion.

DIRECTION OF THE TWO ASSERTIONS. An unresolved pointer is a body the loop
cannot reach -- always a defect. An orphaned section is a body nothing points
at -- also unreachable, but the likelier cause is a pointer that was deleted
rather than a section that was added, so both are failures and both name the
offender.

Deliberately NOT generalised to every SKILL.md: this pointer convention is
currently used by exactly one extraction. Widen it when a second one adopts it,
not before (implementation-discipline: no single-use abstraction, and a test
that scans files using no such pointers passes vacuously forever).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SKILL = ROOT / ".claude" / "skills" / "aspirations-precheck" / "SKILL.md"
DIGEST = ROOT / "core" / "config" / "aspirations-precheck-digest.md"

# `> Body in `<path>` (SS <section>) - ...`  -- capture the section name only.
POINTER = re.compile(r"▸ Body[^\n]*?\(§ ([^)]+)\)")
HEADING = re.compile(r"^##\s+(.+?)\s*$", re.M)


def _pointers() -> list[str]:
    return [m.strip() for m in POINTER.findall(SKILL.read_text(encoding="utf-8"))]


def _headings() -> list[str]:
    return [h.strip() for h in HEADING.findall(DIGEST.read_text(encoding="utf-8"))]


def _ids(headings: list[str]) -> set[str]:
    """A pointer names the heading's id part -- the text before the first ':'."""
    return {h.split(":")[0].strip() for h in headings}


def test_the_parser_is_not_vacuous():
    """Both regexes must see live material.

    Every assertion below is 'no offenders', which a regex matching NOTHING
    satisfies forever. If the pointer glyph or the heading level ever changes,
    fail HERE with a clear cause instead of turning the real checks green and
    hollow. (Same guard, same reason, as test_precheck_phase_chain.py's.)
    """
    assert DIGEST.exists(), f"digest missing: {DIGEST}"
    assert SKILL.exists(), f"skill missing: {SKILL}"
    pointers, headings = _pointers(), _headings()
    assert len(pointers) > 15, f"pointer parser found only {len(pointers)}"
    assert len(headings) > 15, f"heading parser found only {len(headings)}"


def test_every_pointer_resolves_to_a_digest_section():
    """A pointer naming no section is a phase body the loop cannot reach."""
    ids = _ids(_headings())
    missing = sorted({p for p in _pointers() if p not in ids})
    assert not missing, (
        "SKILL.md points at digest sections that do not exist -- the phase body "
        "is unreachable and the loop will execute the phase with no body, "
        "silently:\n  " + "\n  ".join(missing)
    )


def test_no_digest_section_is_orphaned():
    """A section nothing points at is unreachable from SKILL.md."""
    pointed = set(_pointers())
    orphans = sorted(i for i in _ids(_headings()) if i not in pointed)
    assert not orphans, (
        "digest sections with no pointer in SKILL.md -- a reader following the "
        "chain can never reach them:\n  " + "\n  ".join(orphans)
    )


def test_no_duplicate_digest_section_ids():
    """Two sections sharing an id collapse in _ids() and go invisible.

    Both checks above compare SETS, so a duplicated `## Phase X: ...` heading
    satisfies them -- every pointer still resolves and nothing reads orphaned --
    while a reader following the pointer finds two bodies and cannot tell which
    is authoritative. Multiplicity has to be asserted before the collapse.
    """
    ids = [h.split(":")[0].strip() for h in _headings()]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert not dupes, (
        "digest sections share an id -- a pointer to one of these resolves "
        "ambiguously and the set() comparisons above cannot see it:\n  "
        + "\n  ".join(dupes)
    )
