"""Shared two-layer prose filter for scanners that grep identifier patterns
from mixed prose+code documents (SKILL.md, digest .md, .sh scripts).

Extracted from signal-lifecycle-gate.py (rb-349, guard-319) so skill-structure-gate.py
and future scanners reuse the same filter without duplication.

Layer 1 (`is_prose_line`): whole-line skip for comment lines in .md and .sh.
Layer 2 (`strip_prose_refs`): inline-backtick strip for prose mentions in .md.

DO NOT extend the Layer 1 suffix set to include .py without re-reading rb-349:
Python source writes to state directly in CODE (subprocess calls), not
comments; admitting .py here would hide real writers that follow a
`#`-prefixed line.
"""
from __future__ import annotations

import re
from pathlib import Path

# Layer 2 matcher. Matches an inline-backtick run containing at least one
# non-backtick, non-newline char — so a bare ``` fence line matches nothing.
INLINE_BACKTICK_RE = re.compile(r"`[^`\n]+`")


def is_prose_line(line: str, path: Path) -> bool:
    """Layer 1: True if the line should be skipped entirely before scanning.

    Comment lines in .md and .sh files are free-text prose — their
    shell-command idioms (documentation, not executable calls) must not be
    counted by callers that scan for identifier-pattern matches. Callers
    cannot distinguish English mentions from real references, so the whole
    line is suppressed at the source.
    """
    return path.suffix in (".md", ".sh") and line.lstrip().startswith("#")


def strip_prose_refs(line: str, path: Path) -> str:
    """Layer 2: strip inline-backtick references in markdown so prose mentions
    of ``wm-set.sh KEY`` or ``bash foo.sh`` don't register as real calls.

    Applies ONLY to .md files. Shell/python source uses backticks for
    command substitution, which MUST be left alone.
    """
    if path.suffix == ".md":
        return INLINE_BACKTICK_RE.sub("", line)
    return line


# Match a line that opens / closes a triple-quoted Python docstring. Captures
# both `"""` and `'''`. Triple-quote runs INSIDE single-quoted strings on the
# same line are out of scope — they're not real docstring delimiters and the
# heuristic accepts the rare false negative to stay simple.
_TRIPLE_QUOTE_RE = re.compile(r'"""|\'\'\'')


def pyfile_docstring_lines(text: str, path: Path) -> set:
    """Return set of 1-indexed line numbers that lie inside a Python triple-
    quoted docstring (including the opening and closing delimiter lines).

    Used by scanners that grep identifier patterns in .py files: real Python
    writers never appear inside docstrings, so docstring lines are pure prose
    and admitting them produces false positives like the
    'wm-set.sh subprocess pair' regression (g-301-05). Caller invokes once
    per file and skips line numbers in the returned set.

    Returns empty set for non-.py paths.
    Heuristic: counts triple-quote occurrences per line and flips a parity
    bit. Even count keeps state; odd count toggles inside/outside. Lines
    spent inside the docstring are added. NOT a full Python parser — does
    not handle pathological cases such as a single triple-quote run nested
    inside a regular single-quoted string. Trade-off: simple, fast, no
    AST dependency.
    """
    if path.suffix != ".py":
        return set()
    inside = False
    out = set()
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        # Skip `#`-comment lines for parity tracking. A comment that
        # documents the docstring pattern (e.g. `# both """ and '''`)
        # would otherwise toggle the state with a non-delimiter token.
        # The comment line is also not "inside a docstring" so we don't
        # add it either way.
        if stripped.startswith("#"):
            if inside:
                out.add(lineno)
            continue
        matches = _TRIPLE_QUOTE_RE.findall(line)
        if not matches:
            if inside:
                out.add(lineno)
            continue
        # Line opens or closes a docstring. Always count the delimiter line
        # itself as prose — its triple-quote run sits adjacent to any
        # docstring text on the same line.
        out.add(lineno)
        if len(matches) % 2 == 1:
            inside = not inside
    return out
