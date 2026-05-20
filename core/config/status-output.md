# Status Output Protocol

When agent-state is RUNNING, emit clear status lines at transition points so the user can follow along. These are plain text output between tool calls — not tool calls themselves.

Keep all status output brief. Never explain what you're about to do in prose — the markers speak for themselves.

## Session Boundary

At the start of each session (boot):

```
━━━ SESSION {N} ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

## Goal Start

When picking up a new goal:

```
── GOAL: {title} ─────────────────────────────
   {skill} | {priority} | effort: {full/standard}
```

## Context Retrieval

When loading context via intelligent retrieval (Phase 4):

```
▸ Intelligent retrieval: scanning knowledge tree...
▸ Tree nodes: {node-key-1, node-key-2} ({N} nodes loaded)
▸ Supplementary: {N} reasoning, {N} guardrails, {N} patterns, {N} experiences
```

If follow-up nodes needed: `▸ Follow-up: loaded {N} additional nodes for context`
If nothing relevant found: `▸ No relevant tree nodes found — supplementary stores only.`

After retrieval manifest written:
```
▸ Retrieval manifest: {N} nodes, {A} active, {S} skipped items written
▸ Retrieval influence: {how loaded context informs this goal's execution}
```

## Skill Handoff

When one skill invokes another:

```
▸ Invoking {/skill-name}...
```

## Hypothesis Events

When forming or resolving:

```
▸ Hypothesis formed: {title} [{horizon}]
▸ Hypothesis resolved: {title} → {CONFIRMED/CORRECTED}
```

## Goal Complete

When a goal finishes:

```
✓ DONE: {title}
   {one-line outcome summary}
```

If a spark fired: append `| spark: {what it generated}`

After utilization feedback: `▸ Utilization feedback: {H} helpful, {N} noise, {S} skipped`

## Aspiration Events

When an aspiration completes or a new one is created:

```
★ ASPIRATION COMPLETE: {title}
★ NEW ASPIRATION: {title}
```

## Evolution Events

When strategy changes or capability level changes:

```
▲ CAPABILITY: {category} → {new level}
▲ EVOLUTION: {what changed}
```

## Errors and Blocks

When something fails or a goal is blocked:

```
✗ BLOCKED: {goal title} — {reason}
✗ ERROR: {what went wrong}
```

## Partner Status (live cross-agent snapshot)

Surfaced once per iteration at the precheck top (the canonical write site is
`aspirations-precheck` Phase 0-pre.0). Source: `team-state-read.sh --json`,
field `agent_status.<partner>.in_flight`. The board claim posts are the audit
trail; this is the live snapshot — see
`core/config/conventions/coordination.md` "in_flight Field".

```
▸ Partner ({partner-name}): in_flight {goal_id} '{short title}' phase={N} ({Nm/h} ago)
```

When the partner is between goals (no in_flight, but last_active is recent):

```
▸ Partner ({partner-name}): no in_flight | last_active {Nm/h ago}
```

When concluding partner-silent (per `.claude/rules/check-team-state-before-silent.md`),
the inverse must always be derived from this same field — never from "I haven't
heard anything." If `last_active` is within 6h, the partner is NOT silent.
