"""Every precheck phase's budget-drop branch must target its IMMEDIATE successor.

THE DEFECT (found 2026-07-29 by fresh-eyes review, four instances fixed;
this file is the regression guard, core/scripts/tests/test_precheck_phase_chain.py):

    IF decision == "drop": SKIP this phase; continue to Phase 0.5c

read literally -- and an LLM executing pseudocode reads it literally -- that
jumps to 0.5c. When the phase is 0.5b.5 and the file has 0.5b.6 ... 0.5b.14
in between, ONE budget-meter drop silently skips NINE phases.

Phases 0.5b.5/.6/.7/.8 all carried that line while 0.5b.9/.10/.11/.12 chained
correctly to their successors. That asymmetry is the signature of the cause:
each author appending a phase updates the pointer of the phase IMMEDIATELY
BEFORE theirs and leaves every earlier one behind. So the defect is not a typo
that happened four times -- it is the default outcome of the normal editing
motion, and it will recur on the next append unless something checks.

WHY IT MATTERED: 0.5b.5 is the pending-questions sweep -- lane Q of
`.claude/rules/reclaim-routed-work.md`. Lanes B and P are 0.5b.13 and 0.5b.14.
A single drop at lane Q therefore skipped the other two reclaim lanes, which
is exactly the invisible-drop failure that rule's rule 6 warns about: no
error, no signal, just a duty that quietly stops running.

The check is deliberately narrow -- ONLY the literal drop-branch line. Prose
mentions of other phases are not control flow and are none of its business.

THE DIGEST TWIN (g-115-5551, 2026-10-06): on 2026-08-30 (0eb9c4be43) the
deferrable phase BODIES moved out of SKILL.md into
core/config/aspirations-precheck-digest.md, and with them the construct this
file was filed to watch -- the silent clean-case continue,
`continue silently to Phase Y`, which a CLEAN run (not a budget drop) follows.
SKILL.md carries none of them any more; every one lives in the digest. The
same editing motion that left the four original drop-branches mis-pointed --
each author updates the phase immediately before their insertion and leaves
earlier pointers behind -- has since mis-pointed a silent line: 0.5b.23 was
inserted 2026-09-05 (f311a9c4c3), its SKILL.md drop branch was repointed, and
the digest line owned by the phase immediately before it was left targeting
0.5c. A clean run of the hardcoded-scope lane therefore skips the
abandoned-claim detector entirely, with no error and no signal.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

SKILL = (Path(__file__).resolve().parents[3]
         / ".claude" / "skills" / "aspirations-precheck" / "SKILL.md")

# The deferrable BODIES (and every silent-continue below) live here since the
# 2026-08-30 digest extraction (0eb9c4be43) moved them out of SKILL.md.
DIGEST = (Path(__file__).resolve().parents[3]
          / "core" / "config" / "aspirations-precheck-digest.md")

PHASE_HDR = re.compile(r"^##\s+Phase\s+([0-9][0-9A-Za-z.]*?)\s*[:\s]")
DROP_BRANCH = re.compile(
    r'IF\s+decision\s*==\s*"drop".*?continue\s+to\s+Phase\s+([0-9][0-9A-Za-z.]*)')

# THE SILENT TWIN of DROP_BRANCH: the clean-case continue a phase follows when
# its detector finds nothing. It is control flow an executing model reads
# literally, exactly like the drop branch -- so it needs the same guard. The
# digest writes its target with or without the word "Phase" and with or
# without a trailing ".", so the id capture tolerates both.
SILENT_CONTINUE = re.compile(
    r'continue\s+silently\s+to\s+(?:Phase\s+)?([0-9][0-9A-Za-z.]*?)\.?\s*(?:$|#)')

# In the digest, a section header names ONE phase -- BARE, no colon or title
# ("## Phase 0.5b.10", unlike SKILL.md's "## Phase 0.5b.10: Title", which the
# shared PHASE_HDR requires) -- or a COMPOSITE of lanes ("## Phases
# 0.5b.19-0.5b.21: Three Detective Lanes"), whose two endpoints bound the
# range of lane ids it owns. Inside a composite, each lane opens with a
# comment marker ("# 0.5b.19 ...") that attributes the lines that follow to
# that lane -- the same file-order rule _phases() uses.
DIGEST_PHASE_HDR = re.compile(
    r"^##\s+Phase\s+([0-9][0-9A-Za-z.]*?)(?:\s*:\s*.*|\s*$)")
COMPOSITE_HDR = re.compile(
    r"^##\s+Phases\s+([0-9][0-9A-Za-z.]*)\s*-\s*([0-9][0-9A-Za-z.]*)")
# A lane marker is a LINE-START comment naming exactly one phase id,
# followed by a space and its lane name. The negative lookahead rejects
# RANGE prose such as "# 0.5b.3-0.5b.15 all re-probe an EXTERNAL condition"
# (the id is immediately followed by "-<id>"), which is a cross-reference,
# not a lane opening -- treating it as one would close the composite
# early and orphan the lane's silent lines.
LANE_MARKER = re.compile(
    r"^#\s+([0-9][0-9A-Za-z.]+)(?![\w.-])\s+\S")

# Known-open exceptions: phase -> (target, tracking goal).
# An entry here means "this is WRONG and tracked", not "this is fine". Each
# must name a goal, so an exception cannot outlive the work that resolves it.
# EMPTY, and that is the desired state. The one entry it ever held ("0.5g" ->
# "1", tracked by ) was removed 2026-07-29 when that goal resolved.
#
# Worth recording WHY it was deferred, because the stated reason turned out to
# be false: the deferral assumed repointing 0.5g at 0.5h "could activate tiered
# reverts on a path where they have not been running." Measured before fixing --
# `health_regression.mode` is `full`, all five agents carry a `.calibrated`
# marker, and every agent's `.last-check` sat 0-5 iterations behind its ledger's
# current iteration against an interval of 10. So 0.5h was already running
# everywhere and revert authority was already live; the fix activated nothing.
# The gate simply had never tripped (zero health-regression Investigate goals
# ever filed), which is indistinguishable from "dormant" unless you look for a
# per-run artifact. Generalizable: an exemption's rationale is a HYPOTHESIS, and
# it ages exactly like the code it exempts.
ALLOWED_SKIPS = {}


def _phases():
    """[(phase_id, start_line, end_line)] in FILE ORDER.

    File order, never lexical: the chain contains 0.5b.8, 0.5b.8.5, 0.5b.9,
    where any string sort puts 0.5b.8.5 after 0.5b.9 and a naive numeric parse
    chokes on the three-component id. Order of appearance is the real order and
    needs no parsing at all.
    """
    lines = SKILL.read_text(encoding="utf-8", errors="replace").splitlines()
    hdrs = [(m.group(1), i) for i, l in enumerate(lines) if (m := PHASE_HDR.match(l))]
    out = []
    for idx, (pid, start) in enumerate(hdrs):
        end = hdrs[idx + 1][1] if idx + 1 < len(hdrs) else len(lines)
        out.append((pid, start, end))
    return out, lines


def _drop_targets():
    """[(phase_id, target_phase_id, lineno)] for every literal drop branch."""
    phases, lines = _phases()
    out = []
    for pid, start, end in phases:
        for i in range(start, end):
            m = DROP_BRANCH.search(lines[i])
            if m:
                out.append((pid, m.group(1), i + 1))
    return out


def _digest_silent_targets(path=None):
    """[(owner_phase_id, target_phase_id, lineno)] for every silent-continue
    line in the precheck digest, in file order.

    The digest carries the BODIES SKILL.md lost in the 2026-08-30 extraction,
    and with them every `continue silently to Phase Y` line. Its section
    headers name a phase the same way SKILL.md's do -- so the owner of a line
    is the SKILL.md-chain phase whose section (or, in a composite section,
    whose lane marker) owns that line, and the immediate-successor rule is
    evaluated against the SKILL.md chain, which stays the single source of
    order.
    """
    lines = (path or DIGEST).read_text(
        encoding="utf-8", errors="replace").splitlines()
    out = []
    section = None    # phase id of the single-phase section, or None
    composite = None  # (range_start, range_end) phase ids of the composite
    lane = None       # lane id inside a composite, or None
    for i, l in enumerate(lines):
        single = DIGEST_PHASE_HDR.match(l)
        if single:
            section, composite, lane = single.group(1), None, None
            continue
        comp = COMPOSITE_HDR.match(l)
        if comp:
            section = None
            composite = (comp.group(1), comp.group(2))
            lane = None
            continue
        if composite is not None:
            m = LANE_MARKER.match(l)
            if m:
                lo, hi = composite
                if lo <= m.group(1) <= hi:
                    lane = m.group(1)
                else:
                    # A lane id OUTSIDE the named range: the composite is
                    # over. (Headers reset it too; the marker is the tell
                    # for content that outruns the header.)
                    section, composite, lane = None, None, None
                if lane is None:
                    continue
        m = SILENT_CONTINUE.search(l)
        if m and ((composite is None and section is None)
                  or (composite is not None and lane is None)):
            # A silent line whose owner cannot be attributed is exactly how
            # the original defect hides -- fail loudly instead of skipping
            # it (an unattributed line would read as "clean"). "Inside a
            # composite" sets section to None ON PURPOSE, so that state is
            # judged by lane, not by section.
            raise AssertionError(
                f"silent-continue at digest line {i + 1} is not inside a "
                f"phase section or a named lane: {l.strip()!r}")
        if m:
            owner = lane if composite is not None else section
            out.append((owner, m.group(1), i + 1))
    return out


def _silent_offenders(silent_targets=None, skill_targets=None):
    """Shared successor check: every target must be the phase's IMMEDIATE
    successor in the SKILL.md chain (or outside the chain from the final
    phase only). Used by the live guard AND by the mutation-proof test, so
    the proof exercises the identical code path."""
    phases, _ = _phases()
    order = [p for p, _, _ in phases]
    pos = {p: i for i, p in enumerate(order)}
    offenders = []
    for pid, target, lineno, source in silent_targets:
        if pid not in pos:
            continue
        idx = pos[pid]
        is_last = idx == len(order) - 1
        successor = None if is_last else order[idx + 1]
        if target == successor:
            continue
        if target not in pos:
            if is_last:
                continue
        if ALLOWED_SKIPS.get(pid, (None, None))[0] == target:
            continue
        skipped = (order[idx + 1: pos[target]] if target in pos
                   else order[idx + 1:])
        offenders.append(
            f"  {source}:{lineno} Phase {pid} silent-continue -> Phase {target}; "
            f"expected Phase {successor}. Skips {len(skipped)}: "
            f"{', '.join(skipped) or '(rest of chain)'}")
    return offenders


def test_the_parser_is_not_vacuous():
    """Every assertion below is 'no offenders found', which a parser matching
    NOTHING satisfies forever. Pin that both regexes still see live material,
    so a heading-format change fails HERE with a clear cause instead of turning
    the real check green and hollow."""
    phases, _ = _phases()
    assert len(phases) > 20, f"phase parser found only {len(phases)} headers"
    targets = _drop_targets()
    assert len(targets) > 10, f"drop-branch parser found only {len(targets)} branches"
    ids = [p for p, _, _ in phases]
    for expected in ("0.5b.5", "0.5b.8.5", "0.5b.13", "0.5b.14", "0.5c", "0.5c.1"):
        assert expected in ids, f"phase {expected} vanished from the chain"


def test_phase_ids_are_unique():
    """Two phases sharing an id would make 'immediate successor' ambiguous and
    silently weaken every check below."""
    phases, _ = _phases()
    ids = [p for p, _, _ in phases]
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, f"duplicate phase ids: {sorted(dupes)}"


def test_drop_branch_targets_the_immediate_successor():
    """THE guard. A drop must skip ONE phase, never a span of them."""
    phases, _ = _phases()
    order = [p for p, _, _ in phases]
    pos = {p: i for i, p in enumerate(order)}
    offenders = []

    for pid, target, lineno in _drop_targets():
        if pid not in pos:
            continue
        idx = pos[pid]
        is_last = idx == len(order) - 1
        successor = None if is_last else order[idx + 1]

        if target == successor:
            continue
        if target not in pos:
            # Target lives outside this file (e.g. "Phase 1", in the
            # orchestrator). Legitimate ONLY from the final phase -- from any
            # earlier one it leaves the rest of the chain unreachable.
            if is_last:
                continue
        if ALLOWED_SKIPS.get(pid, (None, None))[0] == target:
            continue

        skipped = (order[idx + 1: pos[target]] if target in pos
                   else order[idx + 1:])
        offenders.append(
            f"  SKILL.md:{lineno} Phase {pid} drop-branch -> Phase {target}; "
            f"expected Phase {successor}. Skips {len(skipped)}: "
            f"{', '.join(skipped) or '(rest of chain)'}")

    assert not offenders, (
        "A budget-meter drop must skip ONE phase, not a span. Each of these "
        "silently disables every phase listed:\n" + "\n".join(offenders) +
        "\n\nWhen appending a phase, repoint the drop branch of the phase now "
        "immediately before it -- and check the EARLIER ones too. Fixing only "
        "your own neighbour is what produced four instances of this."
    )


def test_silent_continues_target_the_immediate_successor():
    """THE DIGEST TWIN guard: a silent clean-case continue must also land on
    the IMMEDIATE successor in the SKILL.md chain, never a span past it.

    A drop-branch mis-point costs ONE budget-meter drop; a silent mis-point
    costs the CLEAN path -- the common case that runs on most boxes -- which
    is the worse of the two. Found live by g-115-5551: 0.5b.23 inserted into
    the chain (2026-09-05), the SKILL.md drop branch repointed, and the
    digest's silent line for the phase immediately before it left targeting
    0.5c, so a clean hardcoded-scope run skips the abandoned-claim detector
    with no signal."""
    raw = _digest_silent_targets()
    assert len(raw) >= 10, (
        f"silent-continue parser found only {len(raw)} lines in the digest; "
        f"if the construct was renamed or the digest restructured, this check "
        f"is watching nothing (g-115-5551)")
    offenders = _silent_offenders(
        [(pid, t, n, DIGEST.name) for pid, t, n in raw])
    assert not offenders, (
        "A silent clean-case continue must land on the IMMEDIATE successor, "
        "not a span past it -- the clean path is the one that runs, so a "
        "mis-point here disables every phase listed on the common case:\n"
        + "\n".join(offenders)
        + "\n\nWhen appending a phase, repoint the SILENT target of the "
        "phase now immediately before it in the digest too -- and check the "
        "EARLIER ones. The drop branch and its silent twin are two lines of "
        "the same pointer; fixing only one leaves the other path broken.")


def test_silent_guard_fails_on_a_deliberately_mispointed_branch():
    """PROOF ( verification outcome 2): the extractor must FAIL on
    a branch that was deliberately mis-pointed, before it is trusted. A
    guard whose only evidence is 'zero offenders on live files' is
    indistinguishable from a parser that matches nothing -- the non-vacuity
    test below pins the parser, THIS pins the verdict. The mutation: 0.5b.10
    (single-phase section, so no lane-marker ambiguity) silently continues to
    0.5b.12 instead of 0.5b.11 -- skipping 0.5b.11, exactly the original
    defect shape."""
    mutated = Path(tempfile.gettempdir()) / "test_silent_guard_mutated.md"
    text = DIGEST.read_text(encoding="utf-8")
    good = "continue silently to Phase 0.5b.11"
    bad = "continue silently to Phase 0.5b.12"
    assert text.count(good) == 1, "mutation anchor drifted"
    mutated.write_text(text.replace(good, bad), encoding="utf-8")
    try:
        raw = _digest_silent_targets(mutated)
        offenders = _silent_offenders(
            [(pid, t, n, "MUTATED digest") for pid, t, n in raw])
        flagged = [o for o in offenders
                   if "Phase 0.5b.10" in o and "0.5b.12" in o]
        assert flagged, (
            "the deliberately mis-pointed silent branch (0.5b.10 -> 0.5b.12) "
            "was NOT flagged by the guard's own extractor; the guard cannot "
            "be trusted until it fails on a known bad input. All offenders: "
            + ("\n" + "\n".join(offenders) if offenders else "(none)"))
        for o in flagged:
            assert "expected Phase 0.5b.11" in o, (
                f"flag cites the wrong expected successor: {o}")
    finally:
        mutated.unlink(missing_ok=True)


def test_allowlist_entries_name_a_live_tracking_goal():
    """An exception with no goal attached becomes permanent by neglect. Require
    a goal id in the entry, and require the SKILL.md to still exhibit the skip
    -- so a fixed case cannot leave a stale exemption behind that would mask a
    genuine future regression at the same phase."""
    # An exemption must still be EXHIBITED by some live branch -- drop or
    # silent -- or it is stale and would mask a future regression here.
    targets = ({(p, t) for p, t, _ in _drop_targets()}
               | {(p, t) for p, t, _ in _digest_silent_targets()})
    for phase, (target, goal) in ALLOWED_SKIPS.items():
        assert re.fullmatch(r"g-\d+-\d+", goal), (
            f"ALLOWED_SKIPS[{phase!r}] must name a tracking goal, got {goal!r}")
        assert (phase, target) in targets, (
            f"ALLOWED_SKIPS[{phase!r}] -> {target!r} no longer occurs in "
            f"SKILL.md. If {goal} resolved it, DELETE the entry: a stale "
            "exemption would silently permit a future regression here.")


if __name__ == "__main__":
    import sys
    import traceback
    failures = 0
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError:
            failures += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{'FAILED' if failures else 'OK'}: {failures} failure(s)")
    sys.exit(1 if failures else 0)
