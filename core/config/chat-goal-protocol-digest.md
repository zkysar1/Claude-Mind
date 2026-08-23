# Chat-Goal Protocol Digest

Referenced from `.claude/skills/respond/SKILL.md` Step 5.6. Loaded on demand —
never in the hot path.

The autonomous loop runs every unit of work through
retrieve → execute → verify → encode, anchored on a **goal record**. Assistant
mode runs the same kind of work with none of that scaffolding, so chat work is
invisible to the learning loop unless someone remembers to run
`/encode-session`. This digest is how a substantive chat request gets the loop's
disciplines **without entering the loop** — the mode boundary is deliberate user
control and is not crossed here.

Owner ask that motivated it (2026-08-21, verbatim intent): *"is there a way to
force an agent to get into one aspiration loop, and take that end to end for
each user goal?"*

## The classifier — conservative by design

Fire the lane ONLY when the request is a **substantive work request**: it is
multi-step AND it produces durable artifacts (code, config, framework files, a
knowledge-tree change, a deployment).

Do NOT fire on:

- Questions, explanations, status checks, "what does X do", "why did Y happen"
- Single mechanical edits (a typo, one config value, a rename)
- Anything already routed by a Step 5 directive row (self update, new
  aspiration, priority review, remember-fact, …) — those have their own lanes
  and a second goal record would double-count
- Work the user frames as exploratory ("just poke at", "have a look")

**Over-firing is the failure mode to fear, not under-firing.** The owner likes
chat fast; turning a two-minute answer into goal ceremony is a regression even
when the bookkeeping is correct. When genuinely uncertain, DO NOT fire — the
existing `/encode-session` and Maintain lanes still catch the learning, just
later. One skipped goal record costs a little attribution; one unnecessary
ceremony costs the user's patience on every turn afterwards.

## The four legs

**1 — FILE the goal record, before executing.**

```
Bash: cat <<'JSON' | bash core/scripts/aspirations-add-goal.sh <asp-id> --source <world|agent>
{
  "title": "<the work, as the user framed it>",
  "priority": "MEDIUM",
  "participants": ["agent"],
  "goal_source": "chat-originated",
  "origin_signal": "user_directive",
  "status": "in-progress",
  "description": "<the user's request, verbatim>. Filed by the chat-goal lane (respond Step 5.6).",
  "verification": {"outcomes": ["<stated UP FRONT, before any work>"]}
}
JSON
```

`origin_signal: user_directive` is mandatory and is what the origin-signal gate
checks — `chat-originated` is the `goal_source`, a different field answering a
different question (WHO asked vs WHERE it came from). Scope-route the goal the
same way any other goal is routed: framework/product work to `world`,
agent-private work to `agent`.

**Verification criteria are stated BEFORE execution, not after.** This is the
whole difference between a goal record and a receipt. Criteria written after the
work are a description of what happened; criteria written first can fail.

**2 — EXECUTE under the disciplines that already exist.** No new machinery: the
pre-apply consultation (two queries, subject and mechanism), Tier 2.5 retrieval
escalation, and the full-suite recommender all apply exactly as they do in the
loop. Chat mode has never been exempt from them; it has only lacked the record
that makes skipping them visible.

**3 — CLOSE with evidence.** Write the `outcome_note` with what was actually
measured, then set the status. A chat goal that ends without an `outcome_note`
has produced a record with no evidence in it, which is worse than no record —
it reads as verified work to every later consumer.

**4 — ENCODING FOLLOWS AUTOMATICALLY.** Nothing further to invoke. Once a closed
goal record exists, the standard pipeline, completion reports, and utilization
feedback all see the work, because every one of them is keyed on goal records
rather than on transcripts.

## Prior art — this is a generalization, not a new idea

The **Maintain** primitive already does exactly this for inline fixes: file the
goal with `status: completed` so the standard encoding pipeline fires over work
that was already done. The chat-goal lane is the same move extended from
after-the-fact to in-progress, which is what makes leg 1's up-front criteria
possible.

The companion **retrieve** leg shipped separately on 2026-08-21 as the
`UserPromptSubmit` auto-retrieval pre-pass (`user-prompt-retrieval-inject.sh`),
which covers retrieval per turn. This digest adds execute / verify / encode.

## Measuring adoption

`goal_source` is the census key. A count of `chat-originated` goals flowing
through encoding and completion reporting is the adoption measure — and its
shape matters more than its level: a count that climbs steadily on a chat-heavy
day is the lane working, while a count that fires on nearly every turn is the
classifier over-firing and should be tightened, not celebrated.

## Cross-references

- `.claude/skills/respond/SKILL.md` Step 5.6 — the pointer that reaches this file
- `.claude/skills/encode-session/SKILL.md` — the manual lane this reduces reliance on
- `core/config/conventions/hot-path-size-budget.md` — why this content lives here
  and not in `respond/SKILL.md`
- `core/config/conventions/learning-routing.md` — where each kind of learning goes
- CLAUDE.md § Cognitive Primitives — the Maintain primitive this generalizes
