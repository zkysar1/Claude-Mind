# Plan Completion: Clear the Plan, Answer the Question

## Principle

A plan is instrumental. The user did not ask for a plan; they asked a question
or for an outcome, and the plan was the agent's chosen means of getting there.
When plan execution finishes, the agent owes the ANSWER — the conclusion or
verdict to the original request, informed by what the plan produced. "Plan
finished" is a status about the scaffolding, not a deliverable. Ending there
hands the user a completed plan and an unanswered question (observed
2026-09-05 as the #1 interactive quirk: agent ends on "plan finished", plan
left in place, no conclusion).

Applies whenever plan mode is used (interactive / assistant lane). The
autonomous loop's analog is goal verification (`return-protocol.md`).

## Rules

1. **Anchor the original request in the plan file.** The plan file (named in
   the plan-mode system message) MUST open with a `## Original request`
   section quoting the user's ask verbatim. This is the durable record the
   verdict is derived from — it survives context compression, so the answer
   never depends on remembering a question that has scrolled away.
2. **On completion, clear the plan.** When every step of the approved plan has
   executed, clear the plan file (delete it, or mark it complete per the
   harness's plan-mode conventions) so no stale plan lingers as active work.
3. **Then answer the original request.** Re-read the `## Original request`
   section and deliver the conclusion/verdict that directly answers it, now
   that the plan is done. Lead with the answer; the plan's steps are
   supporting detail, not the headline. Assert on the evidence execution
   produced (`communication-clarity.md` rule 6).
4. **Never end on a bare status.** "Plan finished", "done", "completed the
   plan", or a step recap with no conclusion is a failed handoff — the work
   was done but the deliverable was never delivered.

## Anti-patterns

- Ending with "plan finished" and nothing else
- Leaving the plan file in place after execution so it reads as still-active
- Recapping the steps taken without stating what they concluded
- Answering the question the plan drifted toward instead of the one the user
  originally asked (re-read the anchor)
- Omitting the anchor section, then being unable to reconstruct the original
  ask after compression

## Cross-references

- `communication-clarity.md` rule 6 — assert on observed evidence in the verdict
- `pre-completion-review.md` — re-read your own WORK before declaring done;
  this rule adds: re-read the ORIGINAL ASK
- `return-protocol.md` — terminal-call contract for the autonomous loop; this
  rule governs the interactive plan lane
