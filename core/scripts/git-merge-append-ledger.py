#!/usr/bin/env python3
"""git merge driver for multi-writer APPEND LEDGERS (merge=ayoai-append-ledger).

WHY THIS EXISTS (g-115-10127). The core/config readings ledgers are evidence
series that several boxes append dated readings to. Two boxes appending between
pushes insert at the same point (end of file), which git's default text merge
cannot resolve, so with no routing (merge=unspecified) iteration-push's
integrate aborts and the box wedges. Measured 2026-09-16: cc-05 at behind=37 on
core/config/replay-instrument-readings.md, cleared by hand.

WHY NOT merge=union — MEASURED, NOT INFERRED. union keeps both sides of a
conflict only AFTER xdiff's zealous refinement has diffed the two sides against
each other, so any line the two appended blocks share (a ``` fence, a --- rule,
a formulaic `### Net` heading, a same-agent same-day header) is emitted ONCE and
the blocks are interleaved around it. Nothing errors. Mechanism, from git's own
source: ll_union_merge runs ll_xdl_merge at XDL_MERGE_ZEALOUS, and xdl_merge
refines conflicts only at XDL_MERGE_ZEALOUS or above
(https://raw.githubusercontent.com/git/git/master/merge-ll.c,
https://raw.githubusercontent.com/git/git/master/xdiff/xmerge.c). A clone set
to merge.conflictStyle=diff3 is capped at XDL_MERGE_EAGER and does not refine,
but zdiff3 loses the shared line too: measured on git 2.43.0, a union merge of
two appends that share a closing fence kept 5 of 6 fences under the default
style and under zdiff3, and all 6 under diff3. Production proof: merge
10069ea4c0 (2026-08-21T12:58Z, merge=union on both parents) left
core/config/strategic-scan-readings.md with 19 code fences where the exact
concatenation of both appends has 20, one side's block no longer contiguous; the
file's fence count has been odd ever since.

WHY NOT merge=ayoai-journal-md. That driver keys sections on `## ` headings and
compares the PREAMBLE without a base, so an append that opens no new `## `
section, or a ledger whose rows sit above its first heading, conflicts. Replaying
real history as concurrent appends: 5/5 pairs conflict on
completion-report-coverage-readings.md (a table, no `##` at all), 15/16 on
fresh-eyes-shard-readings.md, 28/30 on run-full-suite-baselines.md.

THE MODEL. The exact merge of two insertions at one point is ours, then theirs,
verbatim. A side EXTENDS a text when it is that text plus whole appended lines.
  1. Whole file: when BOTH sides extend the base, the result is ours followed
     by theirs' appended lines (identical appends, or one append that already
     starts with the other, are kept once).
  2. Otherwise `git merge-file --diff3` runs. diff3 caps xdiff below zealous
     refinement (xdl_merge caps the diff3 style at XDL_MERGE_EAGER:
     https://raw.githubusercontent.com/git/git/master/xdiff/xmerge.c), so every
     conflict hunk holds whole blocks. Never --zdiff3: measured on git 2.43.0 it
     moves the lines two appends share out of the hunk, each side's block
     arrives truncated, and the merge exits clean with a fence lost. Non-overlapping
     edits and deletions merge as in any 3-way merge, and a block both sides
     appended identically is kept once. A conflict hunk where either side
     extends the hunk's base text resolves to the other side's text followed by
     the extending side's appended lines (both extend: ours first). That covers
     "one box edited or deleted the tail, the other appended after it" without
     resurrecting the old line.
  3. A hunk where BOTH sides rewrote its base text is a genuine conflict: it is
     written back between standard 7-character markers and the driver exits 1,
     so git keeps the conflict. Never coalesced. A malformed marker sequence (a
     ledger line shaped like a marker) refuses the merge rather than guess.

Git invokes this as:  driver %O %A %B %P
  argv[1] = %O  base; EMPTY on add/add, where the longest shared line prefix of
                the two sides stands in for it
  argv[2] = %A  ours, and the OUTPUT path
  argv[3] = %B  theirs
  argv[4] = %P  path in the repo (advisory)
Exit 0 = merged, result written to %A. Exit 1 = conflict hunks remain (result
with markers written to %A), or a side was unreadable / merge-file failed (%A
left untouched). Registered per-clone by install-git-hooks.sh.
"""
import os
import subprocess
import sys
import tempfile

# Marker width for the internal diff3 pass. Wide and odd so no ledger line can
# be mistaken for a marker (a reading may quote a standard 7-character marker).
_WIDE = 41


class SideUnreadable(Exception):
    """A side that must exist could not be read. NEVER treat this as empty."""


class MergeFileFailed(Exception):
    """git merge-file could not run or reported an error."""


def _read(path, required=False):
    """Read a side byte-faithfully (newline="" keeps CRLF as written).

    Only %O may be absent (add/add has no base). An unreadable %A or %B raises:
    reading it as empty would report a clean merge that silently drops that
    side's readings.
    """
    if not path:
        if required:
            raise SideUnreadable("no path supplied")
        return ""
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            return fh.read()
    except FileNotFoundError:
        if required:
            raise SideUnreadable("%s does not exist" % path)
        return ""
    except (OSError, UnicodeDecodeError) as exc:
        raise SideUnreadable("%s: %s" % (path, exc))


def _lines(text):
    return text.splitlines(keepends=True)


def _starts_with_lines(text, prefix):
    """True when `prefix` is a sequence of whole leading lines of `text`."""
    p = _lines(prefix)
    return _lines(text)[:len(p)] == p


def _tail(side, base):
    """The whole lines `side` appends after `base`, or None when `side` rewrote
    or removed any of base's text."""
    if not side.startswith(base):
        return None
    tail = side[len(base):]
    if base and not base.endswith("\n") and tail:
        # base's last line had no newline: side may only complete that line
        # before appending. Any other character means the line was edited.
        if tail[0] != "\n":
            return None
        tail = tail[1:]
    return tail


def _join(head, tail):
    """head then tail, never fusing head's last line onto tail's first."""
    if head and tail and not head.endswith("\n"):
        return head + "\n" + tail
    return head + tail


def _resolve(ours, base, theirs):
    """Rule 1 for one region. Returns the merged text, or None when both sides
    rewrote the base text (a genuine conflict)."""
    ours_tail, theirs_tail = _tail(ours, base), _tail(theirs, base)
    if ours_tail is not None and theirs_tail is not None:
        if _starts_with_lines(ours_tail, theirs_tail):
            return ours
        if _starts_with_lines(theirs_tail, ours_tail):
            return theirs
        return _join(ours, theirs_tail)
    if theirs_tail is not None:
        return _join(ours, theirs_tail)
    if ours_tail is not None:
        return _join(theirs, ours_tail)
    return None


def _marker_kind(line):
    """'<', '|', '=' or '>' for a diff3 marker line of our width, else None."""
    body = line.rstrip("\r\n")
    for char in "<|=>":
        if body.startswith(char * _WIDE):
            rest = body[_WIDE:]
            if rest == "" or rest.startswith(" "):
                return char
    return None


# A marker must arrive in exactly this order. Anything else means a ledger line
# matched the marker shape, and a misparse would merge silently wrong, so the
# driver refuses instead (git keeps the conflict).
_NEXT_MARKER = {"ours": "|", "base": "=", "theirs": ">"}


def _conflict_block(ours, theirs):
    ours = ours if not ours or ours.endswith("\n") else ours + "\n"
    theirs = theirs if not theirs or theirs.endswith("\n") else theirs + "\n"
    return "<<<<<<< ours\n" + ours + "=======\n" + theirs + ">>>>>>> theirs\n"


def _diff3_merge(base, ours, theirs):
    """Rules 2-3: returns (merged_text, unresolved_hunk_count)."""
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for name, text in (("ours", ours), ("base", base), ("theirs", theirs)):
            path = os.path.join(tmp, name)
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
            paths.append(path)
        try:
            # Bytes, decoded below: text mode would translate CRLF on the way in.
            proc = subprocess.run(
                ["git", "merge-file", "-p", "--diff3", "--marker-size=%d" % _WIDE,
                 "-L", "ours", "-L", "base", "-L", "theirs", *paths],
                capture_output=True,
            )
        except OSError as exc:
            raise MergeFileFailed("could not run git merge-file: %s" % exc)
    # merge-file exits with the conflict count (capped at 127), or negative on
    # error, which the OS reports as >= 128.
    if proc.returncode >= 128 or proc.returncode < 0:
        raise MergeFileFailed("git merge-file rc=%s: %s" % (
            proc.returncode, proc.stderr.decode("utf-8", "replace").strip()))
    try:
        out = proc.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MergeFileFailed("undecodable merge-file output: %s" % exc)
    if proc.returncode == 0:
        return out, 0

    merged, unresolved = [], 0
    lines = _lines(out)
    state, parts = None, None
    for index, line in enumerate(lines):
        kind = _marker_kind(line)
        if state is None:
            if kind is None:
                merged.append(line)
            elif kind == "<":
                state, parts = "ours", {"ours": [], "base": [], "theirs": []}
            else:
                raise MergeFileFailed("'%s' marker outside a conflict at output line %d" % (kind, index + 1))
            continue
        if kind is None:
            parts[state].append(line)
            continue
        if kind != _NEXT_MARKER[state]:
            raise MergeFileFailed("'%s' marker in the %s section at output line %d" % (kind, state, index + 1))
        if kind == "|":
            state = "base"
        elif kind == "=":
            state = "theirs"
        else:
            hunk = ["".join(parts[k]) for k in ("ours", "base", "theirs")]
            resolved = _resolve(*hunk)
            if resolved is None:
                unresolved += 1
                merged.append(_conflict_block(hunk[0], hunk[2]))
            else:
                if resolved and not resolved.endswith("\n") and index + 1 < len(lines):
                    resolved += "\n"
                merged.append(resolved)
            state, parts = None, None
    if state is not None:
        raise MergeFileFailed("conflict left unterminated in the %s section" % state)
    return "".join(merged), unresolved


def _shared_line_prefix(a, b):
    n = 0
    for x, y in zip(_lines(a), _lines(b)):
        if x != y:
            break
        n += len(x)
    return a[:n]


def merge_append(base, ours, theirs):
    """Return (merged_text, unresolved_hunk_count). 0 == clean merge."""
    if not base and ours and theirs:
        base = _shared_line_prefix(ours, theirs)
    # Shortcut only when BOTH sides purely appended. When one side also rewrote
    # text, diff3 is more precise: it merges a block both sides appended once.
    if _tail(ours, base) is not None and _tail(theirs, base) is not None:
        return _resolve(ours, base, theirs), 0
    return _diff3_merge(base, ours, theirs)


def main(argv):
    if len(argv) < 4:
        print("usage: git-merge-append-ledger.py %O %A %B [%P]", file=sys.stderr)
        return 1
    base_path, ours_path, theirs_path = argv[1], argv[2], argv[3]
    label = argv[4] if len(argv) > 4 else ours_path
    try:
        base = _read(base_path)
        ours = _read(ours_path, required=True)
        theirs = _read(theirs_path, required=True)
        merged, unresolved = merge_append(base, ours, theirs)
    except (SideUnreadable, MergeFileFailed) as exc:
        print("[git-merge-append-ledger] REFUSING %s: %s. git keeps the conflict "
              "for manual resolution." % (label, exc), file=sys.stderr)
        return 1
    try:
        with open(ours_path, "w", encoding="utf-8", newline="") as fh:
            fh.write(merged)
    except OSError as exc:
        print("[git-merge-append-ledger] write failed for %s: %s" % (label, exc),
              file=sys.stderr)
        return 1
    if unresolved:
        print("[git-merge-append-ledger] %s: %d hunk(s) rewritten on BOTH sides, "
              "marked for manual resolution" % (label, unresolved), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
