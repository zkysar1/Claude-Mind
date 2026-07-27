# Mandatory Goal Selection

## Single Authority Rule

`goal-selector.sh` is the ONLY authority on goal availability. It reads live state
(aspirations.jsonl, working-memory.yaml, time gates, blockers) and returns scored candidates.

MUST NOT claim "all goals are blocked" or "no executable goals" without first running:

    Bash: goal-selector.sh

After context compression, narrative memory of what is blocked is unreliable.
The script reads ground truth. Trust its output over any recalled state.

## Both Directions Are Fabrication

The rule above reads as a guard against fabricated **absence** of work. The
mirror — fabricated **presence** — costs just as much and is easier to miss,
because resuming stated work feels like continuity rather than a decision.

MUST NOT resume executing a goal a summary says is in flight without first
confirming against live state that it is actually `pending`/`in-progress` AND
claimed by this agent:

    Bash: aspirations-query.sh --goal-field origin_signal "<goal's origin_signal>"
    Bash: team-state-read.sh --field agent_status.<agent>.in_flight --json

A post-compaction summary is a claim snapshot, not a filesystem snapshot
(`.claude/rules/verify-before-assuming.md`). It records what a prior session
*intended*, and an interrupted or already-closed goal reads identically to a
live one. Three cheap signals settle it: goal `status`, `in_flight`, and whether
`execution-diary.jsonl` holds any phase for that goal id.

Incident (bravo, 2026-07-26, compaction #1): the summary asserted g-115-3203 was
claimed at 04:17:08 with a clean coordination probe. On disk the goal was already
`completed` by bravo earlier the same night, `in_flight` was `null`, the
stranded-claim sweep found nothing, and the execution diary held **zero** phases
for it — the claim never existed. A full re-derivation ran before any of that was
checked. The re-derivation happened to be useful (it corrected a false-negative in
the first pass's reasoning), which is luck, not vindication. Note the reasoning
snapshot restored at session start already said verbatim: *"Phase 2 requires
`goal-selector.sh` — do NOT assume goal availability from memory."* The
instruction was present and in context; what was missing was treating an asserted
in-flight goal as an availability claim at all.

When the check fails, do not treat the summary as merely stale — treat the
iteration as not yet started, and run `goal-selector.sh`.

## Response to Script Output

- **Returns candidates**: execute the top-scoring one. Do not override with ad-hoc work.
- **Returns `[]`**: follow Phase 2's no-goals procedure (invoke /create-aspiration from-self, then ASAP protocol).

## Why This Convention Exists

Session 43: agent claimed "all domain goals are blocked on live systems" without running
goal-selector.sh. Working memory had zero blockers (`known_blockers: []`), and the script
returned 3 selectable goals. The agent did ad-hoc code analysis instead of executing them.
Pattern: post-autocompact narrative fabrication replacing structured goal selection.
