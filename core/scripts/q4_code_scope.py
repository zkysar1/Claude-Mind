#!/usr/bin/env python3
"""Q4 code-artifact scope: a code file is sampled on its comments and docstrings only ().

DISPOSITION, chosen between the two the goal named: the sampler never sends a
CODE line to the claim detector. A code artifact is reduced to the text its
author wrote as prose about the code before clusters are drawn, so every Q4 FAIL
on such a file has a remedy that leaves the executable code alone: put a source
token inside the flagged comment, reword it, or read the cited source. The
rejected alternative, an ADJUDICATED close (record each finding beside
q4_verdict=fail and close completed), is an audited override under another name,
and the one time it was exercised by hand it passed a true positive. Measurements
and reasons: core/config/rationale/verify-check-unevaluatable.md § "Q4 on code
artifacts".

WHY CODE LINES WERE THE PROBLEM. The detector asks "entity signal AND assertion
signal AND no source token" (ground_truth_citation). In code that matches string
constants mirroring a hook's output, assert f-strings, date literals, and shell
redirects whose "dev/null" reads as a tree-node key. None is a claim the author
made, and the only way to clear one is to contort correct code, which is the one
remedy a check must never prescribe.

WHAT IS KEPT, by syntax family (the suffix table below is CLOSED; any other
suffix is not code to this module and keeps its whole-text behaviour byte for byte):
  python  COMMENT tokens (tokenize) and module/class/function docstrings (ast).
          Every other string literal, triple-quoted fixtures included, is code.
  shell   full-line # comments, never the shebang line and never a heredoc body
          (a heredoc is a string literal). A comment trailing code is dropped.
  c-like  full-line // comments and /* */ blocks (java, javascript, typescript).
  lua     full-line -- comments and --[[ ]] blocks (lua dialects).
Only python keeps trailing comments, because only its tokenizer is exact. The
other families UNDER-FLAG on purpose: ground_truth_citation prefers a miss to a
false alarm, and a hand-rolled lexer that guessed at quotes would break that.

A READER'S TRAP, measured 2026-10-13 with the analyzer's own functions:
ground_truth_citation reads a line opening "# " as a markdown heading and so
never forms a fact line from it. Of the comment syntaxes here only // and -- and
block comments and docstrings can draw a finding. The # lines are kept anyway so
this module's scope is defined by syntax, not by that side effect.

LINE NUMBERS ARE PRESERVED (g-115-11480). Dropped text is blanked, never deleted, for the
reason scope_text_to_lines gives: every finding carries start_line/end_line and
a renumbered file would point the reviewer at the wrong place.
"""
from __future__ import annotations

import ast
import re
import tokenize
from pathlib import Path
from typing import NamedTuple, Optional


class CodeScope(NamedTuple):
    language: str       # the syntax family, not the suffix
    text: str           # same line count as the input, code lines blanked
    kept_lines: int     # lines that still carry comment or docstring text
    total_lines: int


_FAMILY_BY_SUFFIX = {
    ".py": "python",
    ".sh": "shell", ".bash": "shell",
    ".java": "c-like", ".js": "c-like", ".mjs": "c-like", ".cjs": "c-like",
    ".ts": "c-like", ".tsx": "c-like", ".jsx": "c-like",
    ".lua": "lua", ".luau": "lua",
}

# str.splitlines() breaks on these as well as on newline and carriage return; the
# tokenizer and ast do not, so a file holding one would carry two different line
# numberings. Such a file is left UNSCOPED (the previous behaviour) rather than
# mis-scoped. Built from code points, not typed, so this file itself holds none
# of them (a literal one made the module refuse to scope its own source).
_EXOTIC_EOL = re.compile("[" + "".join(map(chr, (
    0x0B, 0x0C, 0x1C, 0x1D, 0x1E, 0x85, 0x2028, 0x2029))) + "]")

_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_]\w*)\1")
_LUA_BLOCK = re.compile(r"--\[(=*)\[")


def _python(text: str, lines: list) -> list:
    kept = [""] * len(lines)
    feed = iter(text.splitlines(keepends=True))
    try:
        for tok in tokenize.generate_tokens(lambda: next(feed, "")):
            if tok.type == tokenize.COMMENT and tok.start[0] <= len(lines):
                kept[tok.start[0] - 1] = tok.string
    except (tokenize.TokenError, SyntaxError):    # IndentationError is a SyntaxError
        pass                                       # keep what was read: under-flag, never raise
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return kept
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)) or not node.body:
            continue
        first = node.body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            for n in range(first.lineno, (first.end_lineno or first.lineno) + 1):
                if n <= len(lines):
                    kept[n - 1] = lines[n - 1]
    return kept


def _shell(_text: str, lines: list) -> list:
    kept = [""] * len(lines)
    terminator, strip_tabs = None, False
    for i, line in enumerate(lines):
        if terminator is not None:                 # inside a heredoc body: a string literal
            if (line.lstrip("\t") if strip_tabs else line) == terminator:
                terminator = None
            continue
        bare = line.lstrip()
        if bare.startswith("#") and not (i == 0 and bare.startswith("#!")):
            kept[i] = bare
            continue
        opener = _HEREDOC.search(line)
        if opener and "<<<" not in line:           # a here-string is not a heredoc
            terminator, strip_tabs = opener.group(2), "<<-" in line
    return kept


def _c_like(_text: str, lines: list) -> list:
    kept = [""] * len(lines)
    in_block = False
    for i, line in enumerate(lines):
        if in_block:
            end = line.find("*/")
            kept[i] = line if end == -1 else line[:end + 2]
            in_block = end == -1
            continue
        bare = line.lstrip()
        if bare.startswith("//"):
            kept[i] = bare
        elif bare.startswith("/*"):
            end = bare.find("*/", 2)
            kept[i] = bare if end == -1 else bare[:end + 2]
            in_block = end == -1
    return kept


def _lua(_text: str, lines: list) -> list:
    kept = [""] * len(lines)
    closer = None
    for i, line in enumerate(lines):
        if closer is not None:
            end = line.find(closer)
            kept[i] = line if end == -1 else line[:end + len(closer)]
            closer = closer if end == -1 else None
            continue
        bare = line.lstrip()
        opener = _LUA_BLOCK.match(bare)
        if opener:
            level_close = "]" + opener.group(1) + "]"
            end = bare.find(level_close, opener.end())
            kept[i] = bare if end == -1 else bare[:end + len(level_close)]
            closer = level_close if end == -1 else None
        elif bare.startswith("--"):
            kept[i] = bare
    return kept


_SCANNERS = {"python": _python, "shell": _shell, "c-like": _c_like, "lua": _lua}


def scope_code(path, text: str) -> Optional[CodeScope]:
    """``text`` reduced to its comment and docstring lines, or None when ``path``
    is not a code artifact (or cannot be scoped safely) and the caller should keep
    sampling the whole text exactly as before.
    """
    family = _FAMILY_BY_SUFFIX.get(Path(str(path)).suffix.lower())
    if family is None or _EXOTIC_EOL.search(text or ""):
        return None
    lines = (text or "").splitlines()
    kept = _SCANNERS[family](text or "", lines)
    return CodeScope(family, "\n".join(kept),
                     sum(1 for k in kept if k.strip()), len(lines))
