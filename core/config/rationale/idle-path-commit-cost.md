# Rationale: What the Idle-Path Commit Costs

Referenced from `.claude/skills/aspirations-all-blocked/SKILL.md` Step B7.2, at
the `iteration-commit.sh --goal-id all-blocked` call that runs before the idle
sleep. This file holds what that commit costs on a cycle that wrote nothing
substantive, and why it is not gated on a clean tree. The text moved here
verbatim from the skill so that always-loaded file need not grow (g-375-157).

## Why it does not no-op, and why it is not gated

WHAT IT COSTS ON A CYCLE THAT WROTE NOTHING SUBSTANTIVE, stated honestly
because the obvious claim is wrong: it does NOT no-op. `world/changelog.jsonl`
is appended by the loop's OWN script-mediated reads and writes, so a truly
clean tree is rare here — three consecutive attempts to reach that branch all
found 1-2 dirty files and committed them (measured on downstream prod). Expect
a small `chore(all-blocked)` commit carrying a changelog delta on most idle
cycles. That is bounded and harmless, and it is much cheaper than the failure
it replaces (stranding a guardrail or a tree-node section through an idle
stretch). Do not "fix" the noise by gating on cleanliness without
re-measuring — the gate would rarely fire and would reintroduce the
stranding risk for the cycles that matter.

## Cross-references

- `.claude/skills/aspirations-all-blocked/SKILL.md` Step B7.2 — the commit call
- `core/scripts/iteration-commit.sh` — the script that call routes through
- rb-10474 — why justification moves off a hot-path file behind one pointer line
