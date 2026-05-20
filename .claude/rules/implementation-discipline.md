# Implementation Discipline

## Principle

When executing a goal, change only what the goal requires. The aspiration loop
decides WHAT to improve; this rule constrains HOW each improvement is implemented.
Broad improvement is the loop's job. Surgical implementation is the goal's job.

## Scope

Applies during Phase 4 (goal execution) and any code/config changes made to
fulfill a specific goal. Does NOT constrain goal selection, aspiration generation,
spark questions, or evolution — those systems are designed to pursue broad improvement.

## Rules

1. **Touch only what the goal requires**: If the goal says "fix the retry logic
   in deploy.sh," do not also refactor the logging format, rename variables for
   clarity, or add error handling to adjacent functions.
2. **No speculative features**: Do not add capabilities "in case we need them
   later." If the goal does not require it, do not build it. The loop will
   create a goal for it when the need is real.
3. **No single-use abstractions**: Do not extract a helper, utility, or wrapper
   unless it will be called from at least two call sites that exist today.
   "Might be useful later" is not a second call site.
4. **Match existing style**: When modifying a file, follow the conventions
   already present in that file — naming, indentation, comment style, structure.
   Do not introduce a new convention as part of an unrelated goal.
5. **Clean up only your own mess**: If this goal created dead code, unused imports,
   or orphaned files, remove them. If they existed before this goal, leave them
   for a dedicated cleanup goal.
6. **Scope creep is a new goal**: When you notice adjacent improvements during
   execution, do not inline them. Create an Idea goal via aspirations-add-goal.sh
   and continue with the current goal.

## Anti-patterns

- Refactoring a file "while you're in there" for a goal that only needed a one-line change
- Adding error handling for scenarios that have never occurred and are not part of the goal
- Extracting a function used exactly once "for readability"
- Changing variable names or formatting in lines you did not need to touch
- Building a general solution when a specific one satisfies the goal's verification criteria
