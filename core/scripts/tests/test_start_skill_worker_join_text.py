"""The start skill's RUNNING+autonomous branch must SAY that a second terminal joins a
live reducer as a Worker Body -- and no sentence in the file may end mid-clause.

Why this exists (2026-08-29, alpha, coach on zc-03): a size trim of start/SKILL.md
(g-115-7706, 89,106 -> 63,468 B) cut nine sentences mid-clause. The one that mattered
was line 245: "live reducer runner detected (scenario 1). This second" -- and then
nothing. What followed were the two ALTERNATIVE commands a human may type (take over /
observer window), so a model reading literally printed those options and ended the
turn. A 27B model refused the worker join 2 of 3 times on the same input; the one
success guessed the missing continuation. The intent (bare `/start <agent>` while a
reducer runs = join as worker, commits 14acd1663 / 548b65661) was never in the text.

Two checks, both on the file as shipped:
  1. STRUCTURAL -- the `fresh` paragraph names the Worker Body and tells the reader to
     proceed to the activation sequence BEFORE the alternatives are listed.
  2. FRAGMENTS -- no prose line ends in a dangling function word right before a blank
     line (outside fences, tables, headings). Measured across all 78 skills the same
     day: this heuristic hit exactly the nine trimmed fragments plus two legitimate
     lines in OTHER skills, so it is scoped to this file, where the ceiling pressure
     that produced the cuts is highest.
"""

from __future__ import annotations

import re
from pathlib import Path

SKILL = Path(__file__).resolve().parents[3] / ".claude" / "skills" / "start" / "SKILL.md"

_DANGLING = re.compile(
    r"[ (](the|of|is|a|an|and|or|to|for|with|second|live|either|write|mode|half|this|"
    r"that|in|on|at|by|from|which|when|before|after|not|be|has|have|are|was)$"
)


def _fresh_paragraph(text: str) -> str:
    start = text.index("**IF output is `fresh`**")
    end = text.index("**To take over the reducer role instead**", start)
    return text[start:end]


def test_fresh_branch_tells_the_second_terminal_to_join_as_a_worker() -> None:
    para = _fresh_paragraph(SKILL.read_text(encoding="utf-8"))
    assert "Worker Body" in para, "the join is not stated before the alternatives"
    assert "Proceed to the **Worker Body Activation" in para
    assert "do NOT print a refusal" in para
    assert "DO NOT auto-recover" in para


def test_no_sentence_is_cut_mid_clause() -> None:
    lines = SKILL.read_text(encoding="utf-8").splitlines()
    in_fence = False
    prev = ""
    offenders: list[str] = []
    for n, line in enumerate(lines, start=1):
        if re.match(r"^ *```", line):
            in_fence = not in_fence
        if (
            not in_fence
            and prev
            and line == ""
            and not prev.startswith("#")
            and not re.match(r"^ *\|", prev)
            and _DANGLING.search(prev)
        ):
            offenders.append(f"{n - 1}: {prev.strip()}")
        prev = line
    assert not offenders, "sentence fragments before a blank line:\n" + "\n".join(offenders)
