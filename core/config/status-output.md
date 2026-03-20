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
