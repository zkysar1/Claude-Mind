"""Return-Protocol exempt-list parity: verify-learning case/loop <-> return-protocol.md prose
(g-303-21, zeta allowlist audit site 7a).

The Return-Protocol "hard-exempt user-only skills" allowlist (skills that never
run inside the aspirations loop and so do NOT need a ``## Return Protocol``
section) is duplicated across THREE in-repo copies:

  1. verify-learning/SKILL.md  -- the ``case "$name" in ...) continue ;;`` arm
     that DRIVES the dynamic RP-presence grep (the authoritative enforcement
     list).
  2. verify-learning/SKILL.md  -- the ``for s in <names>; do grep ...`` loop that
     asserts each exempt name appears in return-protocol.md (the
     MISSING_IN_RP_RULE positive invariant).
  3. return-protocol.md        -- the prose bullet list under "EXCEPT for the
     hard-exempt user-only skills" (the human-facing source-of-truth).

The audit flagged 7a rot-risk: a skill added to / removed from one copy without
the others drifts silently. The existing 1295 loop check is ONE-directional
(RP-rule must contain each verify-learning name) and is itself a third hardcoded
copy -- it cannot catch a name present in the prose but absent from the
enforcing ``case`` (a skill the rule SAYS is exempt but the grep still flags).
This is the path-(ii) CI-style divergence test: all three copies must be the
SAME set, in every direction.

Pure-text parse of both files -- no subprocess, no sourcing -- so it is hermetic
and cannot hang on the Windows python->bash->python path (rb-225/rb-247).
"""
from __future__ import annotations

import re
from pathlib import Path


def _find_repo_root() -> Path:
    here = Path(__file__).resolve()
    needed = (
        (".claude", "skills", "verify-learning", "SKILL.md"),
        (".claude", "rules", "return-protocol.md"),
    )
    for anc in [here] + list(here.parents):
        if all((anc.joinpath(*p)).exists() for p in needed):
            return anc
    raise RuntimeError("repo root not found (7a parity anchors missing)")


REPO = _find_repo_root()
VL = REPO / ".claude" / "skills" / "verify-learning" / "SKILL.md"
RP = REPO / ".claude" / "rules" / "return-protocol.md"


def _vl_case_names() -> set:
    """The ``case "$name" in a|b|c) continue ;; esac`` exempt arm -- the list that
    DRIVES the dynamic RP-presence grep (authoritative enforcement)."""
    m = re.search(
        r'case\s+"\$name"\s+in\s+([a-z0-9|\-]+)\)\s*continue',
        VL.read_text(encoding="utf-8"),
    )
    assert m, 'case "$name" in ...) continue arm not found in verify-learning SKILL.md'
    return set(m.group(1).split("|"))


def _vl_loop_names() -> set:
    """The ``for s in <names>; do grep -q ...`` MISSING_IN_RP_RULE loop."""
    m = re.search(
        r'for s in ([a-z0-9 \-]+);\s*do grep -q',
        VL.read_text(encoding="utf-8"),
    )
    assert m, "for s in ...; do grep -q loop not found in verify-learning SKILL.md"
    return set(m.group(1).split())


def _rp_prose_names() -> set:
    """Backtick-wrapped skill names in the bullet block under the
    'EXCEPT for the hard-exempt user-only skills' header in return-protocol.md.

    Scoped to that bullet block so unrelated backtick tokens elsewhere in the
    rule do not leak in: collection starts at the first ``- `` bullet after the
    header and stops at the blank line that closes the block (continuation lines
    that wrap a bullet are included)."""
    names: set = set()
    started = collecting = False
    for line in RP.read_text(encoding="utf-8").splitlines():
        if "EXCEPT for the hard-exempt user-only skills" in line:
            started = True
            continue
        if not started:
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            collecting = True
            names.update(re.findall(r"`([a-z][a-z-]*)`", line))
        elif collecting:
            if stripped == "":
                break
            names.update(re.findall(r"`([a-z][a-z-]*)`", line))
    return names


def test_exempt_lists_three_way_parity():
    case_names = _vl_case_names()
    loop_names = _vl_loop_names()
    prose_names = _rp_prose_names()

    assert case_names, "parsed no names from the verify-learning case arm -- parser broke"
    assert loop_names, "parsed no names from the verify-learning loop -- parser broke"
    assert prose_names, "parsed no names from the return-protocol.md prose -- parser broke"

    # All three copies must be the SAME set. Report every pairwise drift so a
    # failure names exactly which copy diverged and in which direction
    # ( / zeta audit 7a).
    assert case_names == prose_names, (
        f"verify-learning `case` enforcement list != return-protocol.md prose. "
        f"in case not prose: {case_names - prose_names}; "
        f"in prose not case: {prose_names - case_names}. A skill exempted in one "
        f"but not the other drifts silently (allowlist rot)."
    )
    assert loop_names == prose_names, (
        f"verify-learning MISSING_IN_RP_RULE loop list != return-protocol.md prose. "
        f"in loop not prose: {loop_names - prose_names}; "
        f"in prose not loop: {prose_names - loop_names}."
    )
    # case == prose AND loop == prose => case == loop (transitive); asserted
    # explicitly so a failure points at the verify-learning-internal drift too.
    assert case_names == loop_names, (
        f"verify-learning `case` arm != its own MISSING_IN_RP_RULE loop list. "
        f"in case not loop: {case_names - loop_names}; "
        f"in loop not case: {loop_names - case_names}."
    )
