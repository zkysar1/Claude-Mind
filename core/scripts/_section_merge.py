"""Section-level markdown merge — the pure algorithm, shared by two consumers.

EXTRACTED from git-merge-journal-md.py (g-115-7071, echo/cc-03, 2026-08-22).
Behavior-preserving move, not a rewrite: the functions below are byte-identical
to the ones that file carried, and its 27 regression tests still exercise them
through it.

WHY IT LIVES HERE. Two callers need this algorithm and neither can reach the
other's copy:
  1. git-merge-journal-md.py — the merge driver git invokes by path. Its
     filename is HYPHENATED, so it is not an importable module name; the only
     way in is importlib.util.spec_from_file_location.
  2. coordination_merge.merge_handler_for — dispatches the own-cloud store
     merge handlers. Putting a spec_from_file_location load inside a merge
     handler would pay a filesystem module load on a hot path, per call.
Duplicating instead was rejected outright: this routine exists to PREVENT
silent corruption (rb-3683 — line-level auto-merge on markdown can drop an
interleaved section body while keeping its heading), and two drifting copies
of that is the worst outcome available.

CONTRACT. merge_sections(base_text, ours_text, theirs_text) -> (merged_text,
conflicts). Conflicts empty == clean merge. It operates on TEXT. The
coordination_merge handler contract is (bytes, bytes) -> bytes, so that caller
owns the decode/encode and must treat a decode failure as REFUSE (return None,
letting the backend keep its safe-freeze), never as an empty side.
"""
import re

# Heading detector. Anchored at column 0, so 4-space-indented code blocks are
# naturally excluded (guard-526). Fenced blocks are handled separately.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
# Leading HH:MM in a heading body, used for chronological ordering.
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


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
            # Compare the JOINED form, not the raw block lists. split_sections
            # reattaches a trailing blank line to the block that PRECEDES it, so
            # byte-identical content differs raw depending on what follows it in
            # each file — ['## B\n2\n\n'] vs ['## B\n2\n'] for the same section.
            # Raw == therefore reported a conflict on identical content whenever
            # the two sides had different NEIGHBOURING sections, which is the
            # normal cross-box case this driver exists to merge. _join is already
            # what the conflict emitter below and the final assembly use, so
            # comparing in that form makes the decision consistent with the
            # output. Genuine divergence still conflicts ().
            if _join(ours_map[key]) == _join(theirs_map[key]):
                merged[key] = ours_map[key]
            elif key in base_map and _join(base_map[key]) == _join(theirs_map[key]):
                # Theirs is unchanged since base, so ours holds the ONLY edit --
                # take it. Without consulting base this branch conflicted on
                # every ONE-SIDED section edit, which is the normal shape for an
                # edited (rather than purely appended) section: a ledger whose
                # rows accumulate under a stable heading conflicts on every
                # cross-box append. Standard 3-way semantics ().
                merged[key] = ours_map[key]
            elif key in base_map and _join(base_map[key]) == _join(ours_map[key]):
                merged[key] = theirs_map[key]
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
            if key in base_map and _join(base_map[key]) == _join(ours_map[key]):
                continue
            merged[key] = ours_map[key]
        else:
            if key in base_map and _join(base_map[key]) == _join(theirs_map[key]):
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

