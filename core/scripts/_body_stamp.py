#!/usr/bin/env python3
"""_body_stamp.py — the line a writer adds to sign what a worker Body wrote ().

WHY THE WRITER SIGNS, NOT THE BODY. A worker Body used to sign what it filed and
noted ("<agent> worker Body <sid>, <host>") from memory, and nothing ever handed
it its host. A census of 22 such signatures on 2026-10-02 found 8 naming the wrong
box. The wrong names were most often the reducer's box, or the right digits under
another box class's prefix. The writer, by contrast, runs inside the Body's own
Bash call, so it knows all three facts for certain: bash-agent-inject.py exports
MIND_AGENT and MIND_SID into every Bash call and BODY_ROLE=worker into a worker
Body's only, and the host is this process's own. So the writer signs, and
worker-loop tells the Body never to type its host or sid.

WHO GETS A LINE. A worker Body only. The reducer, an observer, a Claude Code
session and a test get none, and their text is stored exactly as passed. So does
a worker whose MIND_AGENT or MIND_SID is missing: no line beats a wrong one.

TWO LINES, BECAUSE A DESCRIPTION IS READ AS SCOPE AND A NOTE IS NOT.
  note      "Auto-signed: <agent> worker Body <sid8>, hostname <host>."
            goal-field-append.py (a progress_note or outcome_note block),
            closure-evidence-write.sh (the closure narrative that becomes
            outcome_note) and aspirations-update-goal.sh (a progress_note or
            outcome_note value it replaces, and an outcome_note riding a status
            write; g-375-115). The first two pass MIND_NOTE_SIGNED=1 when they
            write through the third, so no note is signed twice.
  filing    "Auto-signed: filed by <agent> worker Body on hostname <host>."
            aspirations-add-goal.sh (a filed goal's description).
A description carries no sid, so goal-field-append.py appends to one unsigned.
The close-risk tier counts each distinct 8-hex token in a goal's title,
description and source message as a named entity, and three of them make its
close tier 2. The duplication gate is not a reason. Measured 2026-10-02: two
unrelated prose filings from one Body sharing a sid drew only an advisory from
its pending-queue check, while the same pair sharing a directory-qualified path
was blocked. The host was already in every worker filing, since worker-loop
used to ask the Body to type "filed by <agent> worker Body on <hostname>". So
the filing line says that, now mechanically. Neither line names a script: the
gate reads a file name in a description as a file path, and its
recent-completions check takes a shared path as duplicate evidence.

CLI, for the bash callers:
  _body_stamp.py line          print the note line, or nothing
  _body_stamp.py description   a goal JSON object on stdin; prints it with the
                               filing line ending its description. Any input it
                               cannot sign passes through byte-identical. Input
                               that is not UTF-8 makes it exit 1, and the caller
                               then files the original.
"""
from __future__ import annotations

import json
import os
import platform
import sys


def _identity() -> "tuple[str, str, str] | None":
    """(agent, sid8, host) when a worker Body's own Bash call is running this, else None."""
    if os.environ.get("BODY_ROLE") != "worker":
        return None
    agent = (os.environ.get("MIND_AGENT") or "").strip()
    sid = (os.environ.get("MIND_SID") or "").strip()[:8]
    host = platform.node()
    if not (agent and sid and host):
        return None
    return agent, sid, host


def stamp_line() -> "str | None":
    """The line that signs a worker Body's note, else None.

    Shaped to read as a signature to a person and to the census that measured the
    defect: the worker keyword, the 8-hex sid, then the host. ASCII only, since
    closure-evidence-write.sh counts a stored note's length in bytes.
    """
    ident = _identity()
    if ident is None:
        return None
    agent, sid, host = ident
    return f"Auto-signed: {agent} worker Body {sid}, hostname {host}."


def filing_line() -> "str | None":
    """The line that signs a worker Body's filing, else None. No sid: see the module doc."""
    ident = _identity()
    if ident is None:
        return None
    agent, _sid, host = ident
    return f"Auto-signed: filed by {agent} worker Body on hostname {host}."


def stamp_description(raw: str) -> str:
    """`raw` (a goal as JSON) with the filing line ending its description.

    Anything this cannot sign passes through byte-identical: a caller that is not
    a worker Body, text that is not a JSON object, or a description that is not
    text. The add-goal endpoint then judges the body in its own words, exactly as
    it would have without this step.
    """
    line = filing_line()
    if line is None:
        return raw
    try:
        goal = json.loads(raw)
    except ValueError:
        return raw
    if not isinstance(goal, dict):
        return raw
    desc = goal.get("description")
    if desc is not None and not isinstance(desc, str):
        return raw
    desc = (desc or "").rstrip()
    goal["description"] = f"{desc}\n\n{line}" if desc else line
    return json.dumps(goal, ensure_ascii=False)


def main(argv: "list[str]") -> int:
    if len(argv) != 2 or argv[1] not in ("line", "description"):
        print("usage: _body_stamp.py line|description", file=sys.stderr)
        return 2
    # UTF-8 and a bare "\n", whatever the platform's default. A Windows text stream
    # reads and writes cp1252 (the mojibake source _fileops.py's surrogate gate
    # names) and writes "\n" as "\r\n", and bash's $(...) strips the "\n" but keeps
    # the "\r". Strict errors, unlike the "replace" this codebase uses for output a
    # person reads: a body that cannot be read or written exactly fails here, and
    # the caller then files the original unchanged.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    if argv[1] == "line":
        line = stamp_line()
        if line:
            print(line)
        return 0
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", newline="")
    sys.stdout.write(stamp_description(sys.stdin.read()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
