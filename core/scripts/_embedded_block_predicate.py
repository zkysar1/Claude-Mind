#!/usr/bin/env python3
"""Detect the hand-rolled embedded-block-extraction shape. SSOT for two consumers.

WHY THIS EXISTS (g-115-6228, closing guard-2222 at the TOOL layer).

guard-2222 says: do not hand-roll `grep -n` for an opening marker plus
`awk`/`sed` line arithmetic to pull an embedded code block out of a host file --
use core/scripts/extract-embedded-block.sh. The rule is correct, retrievable and
heavily retrieved (times_active 1796, retrieval_count 69) and it STILL did not
reach the moment of use three times, across three different agents, in 13 days:

  foxtrot 2026-08-01 (g-005-31)   grep -n opener + awk closer + sed slice
  bravo   2026-08-09 (g-335-1035) sed slice on bounds from a pre-edit read
  echo    2026-08-14 (g-326-220)  grep -nF opener + awk closer + sed slice

The rule's own text pre-commits the escalation: "Treat a further recurrence as
evidence for closing this at the TOOL layer (make the hand-rolled path harder or
the helper the obvious default), not for restating the rule again." This module
is that closure. It is deliberately NOT a fourth prose restatement.

WHY THE SHAPE IS THE RIGHT THING TO MATCH. All three incidents share one
invariant: a line number is obtained for a file, and then that file is sliced by
line number. The failure is never loud -- a wrong byte range yields output that
reads exactly like a fact about the file (a false apostrophe violation, a false
IndentationError, a false SyntaxError naming an innocent script). So the trigger
must be the OPERATION, not any particular error.

TWO FORMS, because requiring both halves in one command would miss bravo's:

  A. DERIVE+SLICE -- a line-number source (grep -n, awk print NR) AND a
     line-range slice (sed -n 'a,bp', awk NR>=a, head|tail) in the same command.
     Catches foxtrot and echo.
  B. SLICE+INTERPRET -- a line-range slice piped into an interpreter or syntax
     check (python3, bash -n, py_compile, ast.parse). Catches bravo, whose
     bounds came from an EARLIER read so no grep -n appears in the command at
     all. This is the form that manufactures the most convincing false evidence,
     because the interpreter's complaint names a real file and a real line.

SKIP, NEVER SWALLOW (guard-2655). On anything short of an affirmative match this
returns None. A structural scanner that runs forward on a partial match emits a
line-numbered accusation against a CORRECT file, which is worse than a miss: the
blamed author has nothing to fix and the cost repeats on every box, every run.
Both consumers are report-only for the same reason.

Pure and side-effect free: no I/O, no imports beyond `re`, so both consumers and
the tests exercise identical logic. Mirrors _swakeup_predicate.py, which is the
framework's existing gate+detective SSOT precedent.
"""

import re

HELPER = "core/scripts/extract-embedded-block.sh"
SKILL = "/extract-and-run-embedded-block"

# Already doing the right thing -- never advise against the helper itself.
_USES_HELPER = re.compile(r"extract[-_]embedded[-_]block")

# --- half 1: a line NUMBER is derived from a file -----------------------------
_LINE_NUMBER_SOURCE = (
    # grep -n / -nF / -Fn / --line-number  (short flags may be bundled)
    re.compile(r"\bgrep\b[^|;&\n]*(?:\s-\w*n\w*|\s--line-number)"),
    # awk '/pat/{print NR}'  -- asking awk which line matched
    re.compile(r"\bawk\b[^|;&\n]*\bprint\s+NR\b"),
)

# --- half 2: a file is SLICED by line range -----------------------------------
_LINE_RANGE_SLICE = (
    # sed -n '12,40p'  /  sed -n "$a,$b p"  /  sed -n "${a},${b}p"
    # A BOUND is a literal integer or a shell variable in either form. The
    # braced form is why this is spelled out rather than a loose [\w\}]+ class:
    # `${start}` opens with a brace, and a character class that accepts `}` but
    # not `{` silently fails to match the single most common way a script
    # carries computed bounds. Caught by this module's own positive control on
    # first run () -- the earlier tests passed only because their
    # slice half was matching the awk alternative instead.
    re.compile(r"\bsed\b[^|;&\n]*-n[^|;&\n]*?"
               r"(?:\$\{?\w+\}?|\d+)\s*,\s*(?:\$\{?\w+\}?|\d+)\s*p"),
    # awk 'NR>=12 && NR<=40'
    re.compile(r"\bawk\b[^|;&\n]*\bNR\s*(?:>=|>|==)"),
    # head -n 40 ... | tail -n 12
    re.compile(r"\bhead\b[^|;&\n]*-n[^|;&\n]*\|[^|;&\n]*\btail\b"),
)

# --- form B tail: the slice is handed to an interpreter or syntax check --------
_INTERPRETS = re.compile(
    r"\|\s*(?:python3?|py\s+-3|bash\s+-n|sh\s+-n)\b"
    r"|\bpy_compile\b|\bast\.parse\b|\bbash\s+-n\b"
)


# A heredoc BODY is data being written, not commands being executed. Measured
# live (, first substantive firing after wiring): the gate fired on a
# `cat > record.md <<EOF` whose PROSE described the hand-rolled shape -- an
# experience record about this very defect. Documentation, guardrail text and
# commit messages all discuss the shape by quoting it, so without this the gate
# accuses every document that explains it, and an advisory that cries wolf is
# ignored inside a day (guard-2222 already carries times_noise 9).
#
# This is the gate-side counterpart of the prose filtering guard-319 mandates
# for corpus scanners; the detective got _prose_filter and the gate originally
# got nothing.
#
# EXCEPTION: a heredoc fed to an INTERPRETER is executed, so its body is real.
# `bash <<EOF` / `python3 - <<EOF` keep their bodies.
#
# DIRECTION OF ERROR, chosen deliberately: when an opener has no closing
# delimiter, strip to END rather than stripping nothing. guard-2655 says a
# scanner must SKIP rather than SWALLOW, but that rule governs ACCUSING -- here
# the swallow direction produces FEWER accusations. For an advisory a false
# negative is a missed reminder; a false positive is noise that retires the
# whole gate. Strip generously.
_HEREDOC_OPEN = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
# SHELL only. A `python3 - <<EOF` body is PYTHON, where a shell-shaped string is
# a literal, not an execution -- the same distinction the detective draws for .py
# files. Keeping python bodies made the gate fire on its own test harness.
_HEREDOC_TO_INTERPRETER = re.compile(r"\b(?:bash|sh)\b[^\n]*<<-?\s*['\"]?[A-Za-z_]")


def _strip_heredoc_bodies(text):
    """Remove heredoc bodies that are WRITTEN rather than EXECUTED."""
    lines = text.split("\n")
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        m = _HEREDOC_OPEN.search(line)
        if not m:
            i += 1
            continue
        if _HEREDOC_TO_INTERPRETER.search(line):
            i += 1          # executed body -- keep it verbatim
            continue
        delim = m.group(2)
        i += 1
        while i < len(lines) and lines[i].strip() != delim:
            i += 1          # drop the body
        if i < len(lines):
            out.append(lines[i])   # keep the closing delimiter line
            i += 1
    return "\n".join(out)


def _matches(patterns, text):
    for p in patterns:
        m = p.search(text)
        if m:
            return m.group(0).strip()
    return None


def detect(text):
    """Return a finding dict when `text` hand-rolls embedded-block extraction.

    None means NO affirmative match -- callers must treat None as "say nothing",
    never as "inconclusive, warn anyway" (guard-2655).
    """
    if not text or not isinstance(text, str):
        return None
    text = _strip_heredoc_bodies(text)
    if _USES_HELPER.search(text):
        return None

    derive = _matches(_LINE_NUMBER_SOURCE, text)
    slice_ = _matches(_LINE_RANGE_SLICE, text)
    if not slice_:
        return None

    if derive:
        return {
            "form": "derive+slice",
            "line_number_source": derive,
            "line_range_slice": slice_,
            "helper": HELPER,
            "skill": SKILL,
            "guard": "guard-2222",
        }

    interp = _INTERPRETS.search(text)
    if interp:
        return {
            "form": "slice+interpret",
            "line_number_source": None,
            "line_range_slice": slice_,
            "interpreter": interp.group(0).strip(),
            "helper": HELPER,
            "skill": SKILL,
            "guard": "guard-2222",
        }
    return None


def advisory_text(finding):
    """One-paragraph advisory naming the helper. Shared by both consumers."""
    if finding["form"] == "derive+slice":
        seen = (f"deriving a line number ({finding['line_number_source']}) and "
                f"then slicing by it ({finding['line_range_slice']})")
    else:
        seen = (f"slicing a file by line range ({finding['line_range_slice']}) and "
                f"handing the result to {finding['interpreter']}")
    return (
        f"[embedded-block-extraction] ADVISORY (guard-2222): this command is "
        f"{seen} -- the hand-rolled embedded-block extraction shape. Use "
        f"`bash {HELPER} --grammar shell --file <host> --open-marker \"<opener>\" "
        f"[--close-line <exact>]` instead (or the {SKILL} skill). It takes "
        f"--open-marker as a SUBSTRING, so the `grep` regex trap cannot arise "
        f"through it, and it uses a real quote-state scanner rather than the "
        f"parity test guard-1989 forbids. This path has failed silently three "
        f"times across three agents -- it does not error, it manufactures a "
        f"wrong byte range whose output reads like a real defect in the file. "
        f"ADVISORY ONLY: your command is running."
    )
