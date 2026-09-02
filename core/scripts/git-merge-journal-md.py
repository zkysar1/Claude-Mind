#!/usr/bin/env python3
# domain-leak-exempt: the path family this driver routes
# (agents/<name>/journal/<yyyy>/<mm>/<yyyy-mm-dd>.md) is a real repo path, not
# an illustrative example.
"""git merge driver for the NARRATIVE daily journal (merge=ayoai-journal-md).

Resolves the cross-box add/add conflict on
``agents/<agent>/journal/<yyyy>/<mm>/<yyyy-mm-dd>.md`` by SECTION-LEVEL union
instead of aborting iteration-push.sh's integrate step.

WHY THIS EXISTS (g-115-3425, from bravo's g-115-3422 root-cause isolation).
``.gitattributes`` routes ``agents/*/journal.jsonl`` (the INDEX) to
merge=ayoai-ledger, but nothing matched the NARRATIVE daily ``.md``.
journal-append.sh creates that file on the day's first write, so when one agent
identity runs on two boxes BOTH create the same path independently with no
common ancestor — a textbook add/add that recurs EVERY calendar day for EVERY
multi-box agent, and which blocks the whole push (the merge aborts, so
unrelated ledger work strands with it).

WHY NOT merge=union. ``.gitattributes`` states the union scope is
EVIDENCE-GATED: only files with ZERO historical deleted lines qualify, because
union resurrects pruned/edited lines. Probing this path family over 6185
commits found 1980 deleted content lines, so union is unsafe by the project's
own stated rule. This driver unions by SECTION rather than by line, mirroring
how git-merge-ayoai-ledger.py unions by record rather than by line.

THE MODEL. The file is a sequence of ``## <heading>`` sections (in practice
``## HH:MM — Goal: ...``) under an optional preamble. Sections are keyed by
their heading line. The merge unions the key sets and emits chronologically.
A section present on only one side is kept. A section whose heading appears on
BOTH sides with IDENTICAL body is kept once. A section whose heading appears on
both sides with DIFFERING bodies is a genuine semantic conflict and the driver
REFUSES (exit 1) rather than silently coalescing — the verification criterion
"a same-heading divergence on both sides still surfaces as a real conflict".

Git invokes this as:  driver %O %A %B %P
  argv[1] = %O  ancestor/base file. Usually EMPTY here: the common case is
                add/add, which has no base. Read when present and used only to
                distinguish a DELETED section from a never-present one.
  argv[2] = %A  "ours" file  — ALSO the OUTPUT path (driver writes merged here)
  argv[3] = %B  "theirs" file
  argv[4] = %P  pathname in the repo (advisory; this driver is path-agnostic)
Exit 0 = merged cleanly (result written to %A).
Exit 1 = conflicts remain. Per git's driver contract the best-effort result is
         STILL written to %A, with standard <<<<<<< / ======= / >>>>>>> markers
         around ONLY the diverging sections — every other section is still
         auto-unioned, so the human resolves just the real disagreement.
         Writing markers is load-bearing: leaving %A untouched would show an
         ours-only file with no markers, and a reflexive `git add` there drops
         theirs silently (both sides do survive in index stages 2/3, but
         nothing in the working tree tells the reader to look).

DELETION SAFETY. Because union-by-section would otherwise resurrect a section
one side deliberately removed, a section present in BASE and absent from one
side is treated as an intentional deletion and is NOT resurrected. With no base
(the add/add case) every section is an addition, so nothing can be resurrected.
This is the section-level analogue of the union evidence gate.

SAME-HEADING UNION: RE-EXAMINED AND REFUSED AGAIN (g-115-3980 outcome 5, closed
by g-115-4253 2026-07-31). The open question was whether a same-heading
divergence is safe to auto-union rather than conflict. It is not, and the
REFUSE above stands, on three grounds:

  1. Headings are USUALLY minute-keyed (``## HH:MM — <label>``), so identical
     heading + different body means two DIFFERENT events collapsed onto one
     key. "Three live agent-days" understated the sample and the shape:
     re-measured 2026-08-31 (g-115-4259) over every reachable agent-day on
     cc-08, 1,533 of 20,941 headings (7.3%) are NOT minute-keyed, and 159 carry
     NO minute component at all — ``## Consolidation — <date>`` (31),
     ``## Hippocampal Replay — <date> (g-N-N)``, ``## What landed``,
     ``## Changes Made``. Those come from aspirations-consolidate, replay and
     boot SKILL.md, NOT from journal-append.sh (whose :143 heading is
     minute-AND-goal-id keyed by construction). So the genuine same-heading
     collision class is real but lives in those three writers; widening
     journal-append.sh's key would not touch it. Coalescing them concatenates two
     unrelated narratives under one timestamp, and the result is thereafter
     indistinguishable from a single entry — the loss is silent and permanent.
  2. The cost of refusing is already bounded. Per the exit-1 contract above,
     markers wrap ONLY the diverging section and every other section still
     auto-unions, so a reader resolves one section, not a file.
  3. It is NOT the class that wedged cc-06, though g-115-4253 bundled the two.
     There, an unknown basename left the path UNMERGED and aborted the entire
     integrate (54 commits, 6.2h). Here the driver always writes a resolvable
     annotated result. Same symptom word ("keeps conflicting"), different
     failure shape, different remedy — do not import the fallback from
     git-merge-ayoai-ledger.py into this driver.

THE REPORTED omni COLLISION WAS NOT A SAME-HEADING COLLISION (g-115-4259,
2026-08-31). Still do NOT widen journal-append.sh's key on its account.

The cited evidence was agents/omni/journal/2026/07/2026-07-31.md diverging with
entries timed 03:12 and 03:20. journal-append.sh:143 emits
``## $(date +%H:%M) — Goal: <summary> (<goal-id>)`` unconditionally, so entries
eight minutes apart carry DIFFERENT headings by construction and this driver
unions them cleanly. omni's own file is unreadable from cc-08 too (no agents/omni
on the filesystem; zds-mind declares no peer_world_path, and peer_retrieve's
store list covers board/tree/conventions/JSONL — never agents/*/journal/), so
the verdict rests on the writer's shape plus the reproduction below, NOT on
omni's two literal lines, which remain unread.

REPRODUCED both ways in a throwaway repo — two boxes adding this same path with
those two timestamps, no common ancestor:
  - driver NOT registered -> ``CONFLICT (add/add)``, rc=1, BOTH entries wrapped
    in ONE ``<<<<<<<``/``>>>>>>>`` block. Reads exactly like "the same heading
    diverged". git emits NO warning that the attribute names an absent driver.
  - driver registered      -> rc=0, clean chronological section-union.
So the reported symptom is fully explained WITHOUT identical headings. The
driver landed here 2026-07-27 and the conflict was 2026-07-31 on a deployment
two promotion stages downstream; registration is per-clone in .git/config (NOT
version-controlled), written by install-git-hooks.sh via a fail-open ``|| true``
at sessionstart-orchestrator.sh:103. check-merge-driver-registered.sh
(g-306-333, 2026-08-20) was built for precisely this failure mode three weeks
later. Diagnose an add/add journal conflict by running that check FIRST.
"""
import re
import sys

# Section-merge algorithm lives in _section_merge.py so coordination_merge.py
# can import it too (g-115-7071). This file's name is hyphenated and therefore
# not importable, so the shared module is the only way both callers get ONE copy.
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _section_merge import (  # noqa: E402
    _HEADING_RE, _TIME_RE, _FENCE_RE,
    split_sections, _group, _join, _conflict_block, _sort_index, merge_sections,
)


class SideUnreadable(Exception):
    """A side that must exist could not be read. NEVER treat this as empty."""


def _read(path, required=False):
    """Read a side.

    A MISSING file is legitimate only for %O — the common case here is add/add,
    which has no base — so absence maps to "" .

    An UNREADABLE file (permissions, IO error, undecodable bytes) is NOT the
    same as an empty one. Collapsing it to "" made the driver treat `theirs` as
    contributing nothing and report a CLEAN rc=0 merge, silently discarding the
    other box's entire day of entries. `required=True` (used for %A and %B)
    raises instead, so main() refuses and git keeps the conflict — refusing is
    never data loss, silently succeeding is.
    """
    if not path:
        if required:
            raise SideUnreadable("no path supplied")
        return ""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        if required:
            raise SideUnreadable("%s does not exist" % path)
        return ""
    except (OSError, UnicodeDecodeError) as exc:
        raise SideUnreadable("%s: %s" % (path, exc))



def main(argv):
    if len(argv) < 4:
        print(
            "usage: git-merge-journal-md.py %O %A %B [%P]",
            file=sys.stderr,
        )
        return 1
    base_path, ours_path, theirs_path = argv[1], argv[2], argv[3]
    path_label = argv[4] if len(argv) > 4 else ours_path

    try:
        # %O may legitimately be absent (add/add has no base). %A and %B must be
        # readable — treating an unreadable side as empty silently discards that
        # box's entire day of entries and still reports a clean merge.
        base_text = _read(base_path)
        ours_text = _read(ours_path, required=True)
        theirs_text = _read(theirs_path, required=True)
    except SideUnreadable as exc:
        print(
            "[git-merge-journal-md] REFUSING %s: a merge side is unreadable (%s). "
            "Not treating it as empty — that would silently drop the other side's "
            "entries. git will keep the conflict for manual resolution."
            % (path_label, exc),
            file=sys.stderr,
        )
        return 1

    merged, conflicts = merge_sections(base_text, ours_text, theirs_text)
    # Write in BOTH cases: git's driver contract is to leave the best-effort
    # result in %A and signal the outcome via the exit code. On conflict the
    # result carries standard markers around only the diverging sections, so
    # every non-diverging section is still auto-unioned and the human resolves
    # just the real disagreement.
    try:
        with open(ours_path, "w", encoding="utf-8", newline="") as fh:
            fh.write(merged)
    except OSError as exc:
        print(
            "[git-merge-journal-md] write failed for %s: %s" % (path_label, exc),
            file=sys.stderr,
        )
        return 1

    if conflicts:
        print(
            "[git-merge-journal-md] %s: %d section(s) diverge under the same "
            "heading, marked for manual resolution: %s"
            % (path_label, len(conflicts), ", ".join(conflicts[:5])),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
