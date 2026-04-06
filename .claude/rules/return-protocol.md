# Return Protocol

Every sub-skill called from the aspirations loop MUST end its execution with
a Bash tool call, not text output. Text-only output as the last action kills
the autonomous session — the turn ends, and the loop dies.

## Rule

Your LAST action before returning MUST be a Bash tool call, not text output.
NEVER end with text like "Returning to orchestrator" or "Phase complete."
If your last substantive action was text output, make a final Bash call:
  `Bash: echo "DONE"`
The caller continues immediately after this skill returns.

## Applies To

All sub-skills invoked via `Skill()` or `invoke` during the aspirations loop:
aspirations-verify, aspirations-spark, aspirations-state-update,
aspirations-evolve, reflect, review-hypotheses, and any forged skills
called from the loop.
