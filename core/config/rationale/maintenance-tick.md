# Rationale: Phase 8.7 Tree-Debt Backstop

Referenced from `core/config/aspirations-loop-digest.md` Phase 8.7. The digest
holds the runtime check; this file explains why there's a redundant path.

## Why the g-115-81 backstop exists

The digest runs `/tree maintain --backlog` when `tree debt > threshold * 3`.
Before g-115-81, this was the only path — and it could be abbreviated or
skipped when the LLM was under context pressure, letting the backlog grow
unchecked.

The backstop: `iteration-close.sh --phase learning-gate` ALSO writes the
`force_tree_maintain` WM signal when debt exceeds `threshold * 3`. The next
iteration's `aspirations-precheck` Phase 0-pre reads that signal and invokes
`/tree maintain --backlog` automatically, independent of whether the LLM
remembered to run it during Phase 8.7.

So even if Phase 8.7 is abbreviated, the next-iteration precheck catches up.
Defense in depth: one LLM-side path, one script-side fallback.

## Cross-reference

- Guard: `g-115-81`
- Implementation: `core/scripts/iteration-close.sh` (learning-gate phase),
  `.claude/skills/aspirations-precheck/SKILL.md` (Phase 0-pre)
