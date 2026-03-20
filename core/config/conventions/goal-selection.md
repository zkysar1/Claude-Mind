# Mandatory Goal Selection

## Single Authority Rule

`goal-selector.sh` is the ONLY authority on goal availability. It reads live state
(aspirations.jsonl, working-memory.yaml, time gates, blockers) and returns scored candidates.

MUST NOT claim "all goals are blocked" or "no executable goals" without first running:

    Bash: goal-selector.sh

After context compression, narrative memory of what is blocked is unreliable.
The script reads ground truth. Trust its output over any recalled state.

## Response to Script Output

- **Returns candidates**: execute the top-scoring one. Do not override with ad-hoc work.
- **Returns `[]`**: follow Phase 2's no-goals procedure (invoke /create-aspiration from-self, then ASAP protocol).

## Why This Convention Exists

Session 43: agent claimed "all domain goals are blocked on live systems" without running
goal-selector.sh. Working memory had zero blockers (`known_blockers: []`), and the script
returned 3 selectable goals. The agent did ad-hoc code analysis instead of executing them.
Pattern: post-autocompact narrative fabrication replacing structured goal selection.
