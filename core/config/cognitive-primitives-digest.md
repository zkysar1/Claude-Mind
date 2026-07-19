# Cognitive Primitives — Goal Creation from Observations

Extracted from `.claude/skills/aspirations-execute/SKILL.md`. Loaded on-demand via
`load-cognitive-primitives.sh` when the agent needs to file an Investigate, Idea,
Maintain, or Cross-Agent Insight goal. `Unblock` goes through `CREATE_BLOCKER`
(its own digest — `load-create-blocker-protocol.sh`), never here.

During ANY phase — goal execution, error handling, reflection, spark checks —
the agent can create goals from things it notices. These are NOT mutually
exclusive. A single event can spawn all five:
  Unblock + Investigate + Idea + Maintain + Cross-Agent Insight.

## Unblocking Goals — "Something is stuck"

Created exclusively by the CREATE_BLOCKER protocol (see
`create-blocker-protocol-digest.md`). Never create these manually — always go
through CREATE_BLOCKER so the blocker-create-gate.py structural checks and the
capability-gate.py participant validation fire.

## Investigation Goals — "Something seems off"

```
goal = {
    "title": "Investigate: {observation (50 chars)}",
    "status": "pending",
    "priority": "MEDIUM",
    "skill": null,
    "participants": ["agent"],
    "category": "{relevant category}",
    "description": "Observed during {goal.id}: {observation}\n\nContext: {what prompted this}",
    "verification": {
        "outcomes": ["Root cause understood and documented"],
        "checks": []
    },
    "blocked_by": [],
    "origin_signal": "investigate:{goal.id}"   # REQUIRED by origin-signal-gate — cites triggering goal
}
echo '<goal-json>' | bash core/scripts/aspirations-add-goal.sh --source {source} <aspiration_id>
```

## Idea Goals — "What if we tried...?"

```
goal = {
    "title": "Idea: {creative insight (50 chars)}",
    "status": "pending",
    "priority": "MEDIUM",
    "skill": null,
    "participants": ["agent"],
    "category": "{relevant category}",
    "description": "Idea from {goal.id}: {full description}\n\nExpected benefit: {why this matters}",
    "verification": {
        "outcomes": ["Idea evaluated — implemented, formed hypothesis, or retired"],
        "checks": []
    },
    "blocked_by": [],
    "origin_signal": "idea:{goal.id}"   # REQUIRED by origin-signal-gate — cites triggering goal
}
echo '<goal-json>' | bash core/scripts/aspirations-add-goal.sh --source {source} <aspiration_id>
```

## Maintain Goals — "I just fixed the framework itself"

For in-flight framework corrections the agent performs AS it notices them —
reverting a false auto-clear, patching a script, encoding a new guardrail,
cleaning up stale state. Unlike Investigate (work to be done), Maintain
represents work the agent JUST DID inline and needs proper encoding for.
Without a Maintain goal, the work lives only in ephemeral context — the
experience archive, spark, and tree-encoding pipeline never fire on it.

**Shared-surface collision check (BEFORE starting the inline fix — g-115-2616)**:
when the fix touches a SHARED framework surface (`core/`, `.claude/`,
`mind_api/` — anything sibling agents also patch), the filing-time duplication
gate cannot protect you: completed-Maintain filings are dedup-exempt by design
(g-115-836), and a concurrent partner fix is invisible inside the cross-box
sync horizon (pending-queue mirror lag, unpushed partner commits, laggy
team-state — all three gate anchor stores are box-local views; rb-3296). The
2026-07-18 double-implementation (g-115-2609/2610 vs g-115-2611: same rollback
fix built twice in a 5-minute window, 5-file merge conflict) happened entirely
inside that horizon. The interception point must come BEFORE the work, on the
one partition-surviving channel:

1. Read the coordination board for a matching in-flight fix:
   `board-read.sh --channel coordination --since 90m` — scan for
   `inline-fix-start` (or claim/status) posts naming the same file or symptom.
2. On match: coordinate instead of duplicating — reply to the post, split the
   work, or stand down.
3. On no-match: post the one-liner BEFORE editing:
   `echo "inline-fix-start: <file-or-surface> — <symptom>" | board-post.sh
   --channel coordination --type status --tags "inline-fix-start,<surface-slug>"`

Cost is ~2 board calls. Box-local fixes (own agent dir, non-shared data) skip
this check.

File the Maintain goal BEFORE moving on, with `status: completed` already
set and `started` / `completed_date` timestamps matching the inline work.
The standard post-execution pipeline then encodes the learning properly.

```
goal = {
    "title": "Maintain: {what was fixed (50 chars)}",
    "status": "completed",        # already done inline
    "started": "{ISO now}",
    "completed_date": "{ISO now}",
    "priority": "MEDIUM",
    "skill": null,
    "participants": ["agent"],
    "category": "framework-maintenance",
    "description": "Inline framework correction during {triggering_context}: {what was wrong}\n\nFix applied: {what was changed}\n\nLesson encoded: {guard-ID / rb-ID / rule-path}",
    "verification": {
        "outcomes": ["Framework issue corrected; lesson encoded as guardrail or rule"],
        "checks": []
    },
    "blocked_by": [],
    "origin_signal": "maintain:{triggering_goal_id_or_asp_id}"   # REQUIRED by origin-signal-gate
}
echo '<goal-json>' | bash core/scripts/aspirations-add-goal.sh --source {source} <aspiration_id>
```

Place in `asp-001` (agent self-maintenance) or whichever aspiration houses
framework-maintenance work in your domain. The next iteration's state-update
will fire experience archival, spark, and tree encoding as if the goal had
been executed normally — because it was, just inline rather than claimed.
See `guard-148`.

## Cross-Agent Insight Goals — "This changes something for the other agent"

When execution reveals something that invalidates, constrains, or enables another
agent's work, post an insight trigger to the findings board. This is the reactive
influence channel — discoveries during execution reshape the other agent's strategy.

```
# Post insight trigger finding (see core/config/conventions/board.md
# "Insight Trigger Payload" for the full tag schema)
echo "Description of what was discovered and why it matters" | \
  bash core/scripts/board-post.sh --channel findings --type finding \
    --tags "insight_trigger,severity:<invalidates|constrains|enables|informs>,affects:<goal-id>,requires_action_by:<agent>,action_type:<re-scope|re-prioritize|investigate>,<category>"
```

**Severity guide:**
- `invalidates`: An assumption the other agent relies on is provably wrong
- `constrains`: The other agent's approach needs modification (but isn't wrong)
- `enables`: Something unblocked or became possible that the other agent should know about
- `informs`: Interesting finding — no immediate action needed

## Dedup rule (all types)

Before creating any primitive, check for existing pending/in-progress goals with
similar titles. Duplicates clog the queue and distort the scoring signals that
drive goal selection. Place in the RIGHT aspiration (read active aspirations,
pick best fit — asp-001 is the default agent self-maintenance home).
