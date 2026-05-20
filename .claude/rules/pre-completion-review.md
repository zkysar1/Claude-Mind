# Pre-Completion Review

## Principle

Before marking a goal as completed, review your own work against the goal's
verification criteria. Catch your own mistakes before the verification phase
catches them for you. Self-critique is faster and cheaper than re-execution.

## When To Apply

After Phase 4 execution and before Phase 5 verification, for any goal that
produced file changes, code changes, or knowledge tree modifications.

## Rules

1. **Re-read what you wrote**: After making changes, read the modified files.
   Do not rely on your memory of what you intended to write — verify what
   actually landed on disk.
2. **Check against verification criteria**: Compare the current state to each
   item in `verification.outcomes` and `verification.checks`. If any outcome
   is not met, fix it before proceeding to Phase 5.
3. **Check for unintended side effects**: Did you modify files outside the
   goal's scope? Did you break an existing convention? Did you introduce a
   dependency that did not exist before?
4. **Reduce before completing**: Can any part of your change be removed while
   still satisfying the verification criteria? Remove it. Smaller diffs are
   easier to verify, easier to revert, and less likely to have side effects.

## Anti-patterns

- Declaring completion based on "I wrote the code, so it should work"
- Skipping re-read because the change was "simple" or "obvious"
- Proceeding to Phase 5 with known gaps, hoping the verification will pass anyway
- Adding "bonus" improvements during the review step (that is scope creep —
  Rule 6 of implementation-discipline.md)
