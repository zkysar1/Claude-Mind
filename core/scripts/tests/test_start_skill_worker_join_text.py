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

The CROSS-box branch had the same cut (2026-09-23, alpha-next on zc-01): the acquire
step read "ACQUIRE_RC=4 otherwise ->" and then nothing, followed by the join message AND
the `--reducer-only` refusal with its options. A 35B met rc=4 on a bare `/start alpha`,
printed the refusal and ended the turn: 34 iterations, 76 minutes, no worker. The
verdict script that maps the rc (runner-claim-acquire-verdict.sh, 2026-09-10) existed
and had no call site.

Checks, all on the file as shipped:
  1. STRUCTURAL -- the `fresh` paragraph names the Worker Body and tells the reader to
     proceed to the activation sequence BEFORE the alternatives are listed; the
     cross-box acquire step acts on the verdict script's word, sends `join-as-worker`
     to the CW sequence, and keeps the refusal under `refuse`.
  2. FRAGMENTS -- no prose line ends in a dangling function word or arrow right before
     a blank line (outside fences, tables, headings). Measured across all 78 skills the same
     day: this heuristic hit exactly the nine trimmed fragments plus two legitimate
     lines in OTHER skills, so it is scoped to this file, where the ceiling pressure
     that produced the cuts is highest.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parents[3] / ".claude" / "skills" / "start" / "SKILL.md"
sys.path.insert(0, str(SKILL.parents[3] / "core" / "scripts"))

import runner_claim_acquire as rca  # noqa: E402

_DANGLING = re.compile(
    r"[ (](the|of|is|a|an|and|or|to|for|with|second|live|either|write|mode|half|this|"
    r"that|in|on|at|by|from|which|when|before|after|not|be|has|have|are|was|→)$"
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


def _acquire_step(text: str) -> str:
    start = text.index("runner-claim.sh acquire --agent <agent-name>")
    return text[start : text.index("**CW-pre", start)]


def test_cross_box_acquire_joins_as_a_worker_and_refuses_only_under_reducer_only() -> None:
    step = _acquire_step(SKILL.read_text(encoding="utf-8"))
    assert "runner-claim-acquire-verdict.sh" in step, "the rc is read by hand again"
    # The words the skill branches on are the ones the script prints.
    for verdict in (rca.PROCEED, rca.JOIN_AS_WORKER, rca.REFUSE):
        assert f"`{verdict}`" in step, f"no branch for the verdict {verdict!r}"
    join = step.index(f"`{rca.JOIN_AS_WORKER}`")
    refuse = step.index(f"`{rca.REFUSE}`")
    assert "run CW-pre..CW3" in step[join:refuse]
    assert "NOT a refusal" in step[join:refuse]
    assert refuse < step.index("Cannot start"), "the refusal must sit under `refuse`"


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
