"""Every precheck phase's budget-drop branch must target its IMMEDIATE successor.

THE DEFECT (found 2026-07-29 by fresh-eyes review, four instances fixed):

    IF decision == "drop": SKIP this phase; continue to Phase 0.5c

read literally -- and an LLM executing pseudocode reads it literally -- that
jumps to 0.5c. When the phase is 0.5b.5 and the file has 0.5b.6 ... 0.5b.14
in between, ONE budget-meter drop silently skips NINE phases.

Phases 0.5b.5/.6/.7/.8 all carried that line while 0.5b.9/.10/.11/.12 chained
correctly to their successors. That asymmetry is the signature of the cause:
each author appending a phase updates the pointer of the phase IMMEDIATELY
BEFORE theirs and leaves every earlier one behind. So the defect is not a typo
that happened four times -- it is the default outcome of the normal editing
motion, and it will recur on the next append unless something checks.

WHY IT MATTERED: 0.5b.5 is the pending-questions sweep -- lane Q of
`.claude/rules/reclaim-routed-work.md`. Lanes B and P are 0.5b.13 and 0.5b.14.
A single drop at lane Q therefore skipped the other two reclaim lanes, which
is exactly the invisible-drop failure that rule's rule 6 warns about: no
error, no signal, just a duty that quietly stops running.

The check is deliberately narrow -- ONLY the literal drop-branch line. Prose
mentions of other phases are not control flow and are none of its business.
"""

from __future__ import annotations

import re
from pathlib import Path

SKILL = (Path(__file__).resolve().parents[3]
         / ".claude" / "skills" / "aspirations-precheck" / "SKILL.md")

PHASE_HDR = re.compile(r"^##\s+Phase\s+([0-9][0-9A-Za-z.]*?)\s*[:\s]")
DROP_BRANCH = re.compile(
    r'IF\s+decision\s*==\s*"drop".*?continue\s+to\s+Phase\s+([0-9][0-9A-Za-z.]*)')

# Known-open exceptions: phase -> (target, tracking goal).
# An entry here means "this is WRONG and tracked", not "this is fine". Each
# must name a goal, so an exception cannot outlive the work that resolves it.
ALLOWED_SKIPS = {
    # 0.5h is a Health-Regression Detection + REVERT sweep. Repointing 0.5g at
    # it could activate tiered reverts on a path where they have not been
    # running, so it was deliberately NOT fixed alongside the other four.
    "0.5g": ("1", "g-115-3830"),
}


def _phases():
    """[(phase_id, start_line, end_line)] in FILE ORDER.

    File order, never lexical: the chain contains 0.5b.8, 0.5b.8.5, 0.5b.9,
    where any string sort puts 0.5b.8.5 after 0.5b.9 and a naive numeric parse
    chokes on the three-component id. Order of appearance is the real order and
    needs no parsing at all.
    """
    lines = SKILL.read_text(encoding="utf-8", errors="replace").splitlines()
    hdrs = [(m.group(1), i) for i, l in enumerate(lines) if (m := PHASE_HDR.match(l))]
    out = []
    for idx, (pid, start) in enumerate(hdrs):
        end = hdrs[idx + 1][1] if idx + 1 < len(hdrs) else len(lines)
        out.append((pid, start, end))
    return out, lines


def _drop_targets():
    """[(phase_id, target_phase_id, lineno)] for every literal drop branch."""
    phases, lines = _phases()
    out = []
    for pid, start, end in phases:
        for i in range(start, end):
            m = DROP_BRANCH.search(lines[i])
            if m:
                out.append((pid, m.group(1), i + 1))
    return out


def test_the_parser_is_not_vacuous():
    """Every assertion below is 'no offenders found', which a parser matching
    NOTHING satisfies forever. Pin that both regexes still see live material,
    so a heading-format change fails HERE with a clear cause instead of turning
    the real check green and hollow."""
    phases, _ = _phases()
    assert len(phases) > 20, f"phase parser found only {len(phases)} headers"
    targets = _drop_targets()
    assert len(targets) > 10, f"drop-branch parser found only {len(targets)} branches"
    ids = [p for p, _, _ in phases]
    for expected in ("0.5b.5", "0.5b.8.5", "0.5b.13", "0.5b.14", "0.5c"):
        assert expected in ids, f"phase {expected} vanished from the chain"


def test_phase_ids_are_unique():
    """Two phases sharing an id would make 'immediate successor' ambiguous and
    silently weaken every check below."""
    phases, _ = _phases()
    ids = [p for p, _, _ in phases]
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, f"duplicate phase ids: {sorted(dupes)}"


def test_drop_branch_targets_the_immediate_successor():
    """THE guard. A drop must skip ONE phase, never a span of them."""
    phases, _ = _phases()
    order = [p for p, _, _ in phases]
    pos = {p: i for i, p in enumerate(order)}
    offenders = []

    for pid, target, lineno in _drop_targets():
        if pid not in pos:
            continue
        idx = pos[pid]
        is_last = idx == len(order) - 1
        successor = None if is_last else order[idx + 1]

        if target == successor:
            continue
        if target not in pos:
            # Target lives outside this file (e.g. "Phase 1", in the
            # orchestrator). Legitimate ONLY from the final phase -- from any
            # earlier one it leaves the rest of the chain unreachable.
            if is_last:
                continue
        if ALLOWED_SKIPS.get(pid, (None, None))[0] == target:
            continue

        skipped = (order[idx + 1: pos[target]] if target in pos
                   else order[idx + 1:])
        offenders.append(
            f"  SKILL.md:{lineno} Phase {pid} drop-branch -> Phase {target}; "
            f"expected Phase {successor}. Skips {len(skipped)}: "
            f"{', '.join(skipped) or '(rest of chain)'}")

    assert not offenders, (
        "A budget-meter drop must skip ONE phase, not a span. Each of these "
        "silently disables every phase listed:\n" + "\n".join(offenders) +
        "\n\nWhen appending a phase, repoint the drop branch of the phase now "
        "immediately before it -- and check the EARLIER ones too. Fixing only "
        "your own neighbour is what produced four instances of this."
    )


def test_allowlist_entries_name_a_live_tracking_goal():
    """An exception with no goal attached becomes permanent by neglect. Require
    a goal id in the entry, and require the SKILL.md to still exhibit the skip
    -- so a fixed case cannot leave a stale exemption behind that would mask a
    genuine future regression at the same phase."""
    targets = {(p, t) for p, t, _ in _drop_targets()}
    for phase, (target, goal) in ALLOWED_SKIPS.items():
        assert re.fullmatch(r"g-\d+-\d+", goal), (
            f"ALLOWED_SKIPS[{phase!r}] must name a tracking goal, got {goal!r}")
        assert (phase, target) in targets, (
            f"ALLOWED_SKIPS[{phase!r}] -> {target!r} no longer occurs in "
            f"SKILL.md. If {goal} resolved it, DELETE the entry: a stale "
            "exemption would silently permit a future regression here.")


if __name__ == "__main__":
    import sys
    import traceback
    failures = 0
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError:
            failures += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{'FAILED' if failures else 'OK'}: {failures} failure(s)")
    sys.exit(1 if failures else 0)
