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

  1. Headings are minute-keyed (``## HH:MM — <label>``; measured across three
     live agent-days). Identical heading + different body therefore means two
     DIFFERENT events collapsed onto one key. Coalescing them concatenates two
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

The improvement actually available is UPSTREAM of the merge: if two boxes can
emit the same ``## HH:MM — <label>`` for different events, the key is not unique
enough, and widening it at write time (journal-append.sh) removes the class
instead of teaching the merge to paper over it. Deliberately NOT implemented
here: the reported collisions were on another deployment's journal, unreadable
from this box, so the same-minute-different-event cause is UNMEASURED. Filed
rather than fixed on a guess.
"""
import re
import sys

# Heading detector. Anchored at column 0, so 4-space-indented code blocks are
# naturally excluded (guard-526). Fenced blocks are handled separately.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
# Leading HH:MM in a heading body, used for chronological ordering.
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


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


def split_sections(text, heading_level=2):
    """Split markdown into (preamble, [(key, block), ...]).

    `block` is the heading line plus its body, verbatim including the trailing
    newline structure, so re-emission is loss-free.

    guard-526: fenced code blocks are tracked with an in_fence flag and heading
    detection is SKIPPED inside them — a journal entry quoting shell output or
    a template can legitimately contain a line starting with '## ', and treating
    that as document structure would split a section mid-body.
    """
    if not text:
        return "", []
    lines = text.splitlines(keepends=True)
    preamble = []
    sections = []
    cur_key = None
    cur_buf = []
    in_fence = False

    for line in lines:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            (cur_buf if cur_key is not None else preamble).append(line)
            continue
        m = None if in_fence else _HEADING_RE.match(line)
        if m and len(m.group(1)) == heading_level:
            if cur_key is not None:
                sections.append((cur_key, "".join(cur_buf)))
            cur_key = m.group(2).strip()
            cur_buf = [line]
        elif cur_key is not None:
            cur_buf.append(line)
        else:
            preamble.append(line)

    if cur_key is not None:
        sections.append((cur_key, "".join(cur_buf)))
    return "".join(preamble), sections


def _group(secs):
    """[(key, block), ...] -> {key: [block, ...]} preserving duplicates in order."""
    out = {}
    for key, block in secs:
        out.setdefault(key, []).append(block)
    return out


def _join(blocks):
    """Join a heading's block list back into one chunk for marker wrapping."""
    return "\n\n".join(b.rstrip("\n") for b in blocks)


def _conflict_block(ours, theirs):
    """Standard git conflict markers around two diverging chunks."""
    return (
        "<<<<<<< ours\n"
        + ours.rstrip("\n")
        + "\n=======\n"
        + theirs.rstrip("\n")
        + "\n>>>>>>> theirs\n"
    )


def _sort_index(key):
    """Chronological ordering hint from a leading HH:MM in the heading.

    Returns (0, minutes) for a parseable timestamp so timed sections sort
    ahead of untimed ones, else (1, 0) which — combined with a STABLE sort —
    leaves untimed sections in their original relative order.
    """
    m = _TIME_RE.match(key)
    if not m:
        return (1, 0)
    return (0, int(m.group(1)) * 60 + int(m.group(2)))


def merge_sections(base_text, ours_text, theirs_text):
    """Return (merged_text, conflicts). Empty conflicts == clean merge."""
    base_pre, base_secs = split_sections(base_text)
    ours_pre, ours_secs = split_sections(ours_text)
    theirs_pre, theirs_secs = split_sections(theirs_text)

    # Group by heading into a LIST of blocks, never dict(secs). A file can
    # legitimately repeat a heading (two goals closing in the same minute), and
    # dict() silently keeps only the LAST — dropping the earlier entry inside a
    # single side, which is exactly the data loss this driver exists to prevent.
    base_map = _group(base_secs)
    ours_map = _group(ours_secs)
    theirs_map = _group(theirs_secs)

    conflicts = []

    # Preamble: identical, or one side empty, else a real conflict.
    if ours_pre == theirs_pre:
        preamble = ours_pre
    elif not ours_pre.strip():
        preamble = theirs_pre
    elif not theirs_pre.strip():
        preamble = ours_pre
    else:
        preamble = _conflict_block(ours_pre, theirs_pre)
        conflicts.append("<preamble>")

    merged = {}
    # Preserve first-seen order, ours before theirs, so the stable sort below
    # keeps a deterministic result for untimed sections.
    # De-duplicate while preserving first-seen order: a heading repeated within
    # one side must be visited ONCE here (its blocks all live in the grouped
    # list), otherwise it would be emitted twice.
    order = []
    for k, _ in ours_secs:
        if k not in order:
            order.append(k)
    for k, _ in theirs_secs:
        if k not in ours_map and k not in order:
            order.append(k)

    for key in order:
        in_ours, in_theirs = key in ours_map, key in theirs_map
        if in_ours and in_theirs:
            if ours_map[key] == theirs_map[key]:
                merged[key] = ours_map[key]
            else:
                # Same heading, different body on both sides. Do NOT coalesce —
                # emit standard conflict markers so the divergence is visible in
                # the WORKING TREE, then report non-zero. Leaving %A untouched
                # instead would show the human an ours-only file with no markers,
                # and a reflexive `git add` there silently drops theirs (both
                # sides survive in index stages 2/3, but nothing tells the reader
                # to look). Git's driver contract is to leave the best-effort
                # result in %A and return non-zero.
                merged[key] = [_conflict_block(
                    _join(ours_map[key]), _join(theirs_map[key])
                )]
                conflicts.append(key)
        elif in_ours:
            # Absent from theirs. If BASE had it, theirs deleted it on purpose
            # -> honor the deletion rather than resurrecting it.
            if key in base_map and base_map[key] == ours_map[key]:
                continue
            merged[key] = ours_map[key]
        else:
            if key in base_map and base_map[key] == theirs_map[key]:
                continue
            merged[key] = theirs_map[key]

    ordered = sorted(merged.keys(), key=lambda k: _sort_index(k))
    # Normalize the JOIN only: rstrip each block's trailing blank lines and
    # rejoin with exactly one blank line between sections. Interior content is
    # untouched. Without this, a side whose file ended with no trailing blank
    # line concatenates its last body line straight onto the next '## ' heading
    # ("box-B more\n## 02:58 ..."), which some markdown renderers refuse to read
    # as a heading. Caught by the live end-to-end merge, not by unit tests.
    blocks = []
    for k in ordered:
        for blk in merged[k]:
            blocks.append(blk.rstrip("\n"))
    body = "\n\n".join(blocks)
    pre = preamble.rstrip("\n")
    if pre and body:
        out = pre + "\n\n" + body
    else:
        out = pre or body
    # Keep exactly one trailing newline when there is any content.
    if out:
        out += "\n"
    # Return the ACCUMULATED conflicts, not a literal []. This was hardcoded
    # empty while conflicts took an early `return None, conflicts`; once the
    # driver switched to always writing a best-effort result, the literal
    # silently discarded every conflict and the driver reported success on a
    # genuine divergence.
    return out, conflicts


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
