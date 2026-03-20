---
name: prime
description: "Context priming — load domain knowledge, guardrails, and reasoning into active context"
user-invocable: false
triggers:
  - "/prime"
execution_history:
  total_invocations: 0
  outcome_tracking:
    successful: 0
    unsuccessful: 0
    success_rate: 0.0
  last_invocation: null
  known_pitfalls: []
  reconsolidation_trigger: "After 10 invocations with declining success rate, trigger skill review"
conventions: [tree-retrieval, reasoning-guardrails, pattern-signatures]
---

# /prime — Context Priming Engine

Loads the agent's accumulated knowledge into active context so that conversations
and goal execution start with domain awareness rather than amnesia.

**Internal skill**: called by boot (RUNNING state) and `/enterPersona` (any state).
Not user-invocable — users enter persona to prime automatically.

**Key design**: Boot loads the MAP (indexes, summaries). Prime loads the TERRITORY
(actual tree node content, reasoning bank entries, guardrail details). Together they
give the agent full domain awareness.

## Sub-commands

```
/prime                    — Auto-detect context and prime broadly
/prime --category <cat>   — Prime a single category at deep depth
```

## Phase 0: Load Conventions

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Phase 1: Detect Context & Build Category List

```
1. Bash: `session-state-get.sh` → determine IDLE vs RUNNING vs UNINITIALIZED
   - UNINITIALIZED: output "Nothing to prime — run /start first." → STOP
   - IDLE or RUNNING: PROCEED

2. IF --category <cat> argument provided:
     Set categories = [{name: <cat>, depth: "deep"}]
     SKIP to Phase 2

3. Read mind/self.md → extract domain identity (for display in Phase 4)
   IF missing: self_summary = "Not configured"

4. Read mind/profile.yaml → check focus field
   IF focus is set and non-null: add focus domain as Tier 1 category

5. Determine categories from aspirations and pipeline:
   Bash: load-aspirations-compact.sh → IF path returned: Read it
     (compact data has IDs, titles, statuses, priorities, categories — no descriptions/verification)
     Extract unique goal categories:
     - In-progress goal categories → Tier 1 (depth: medium)
     - HIGH priority pending goal categories → Tier 2 (depth: shallow)
     - Remaining goal categories → Tier 3 (skip)
   Bash: pipeline-read.sh --stage active → extract active hypothesis categories
     - Active hypothesis categories → Tier 2 (depth: shallow)

6. Deduplicate: if a category appears in multiple tiers, use the highest tier

7. Apply budget caps:
   - IDLE broad: max 3 categories at medium depth
   - RUNNING full: max 3 categories at medium + 2 at shallow
   - Targeted (--category): 1 category at deep (no cap)
```

## Phase 2: Load Domain-Agnostic Stores (Always)

These are small, always relevant, and not category-specific. Load unconditionally.

```
1. Read mind/self.md → full content
   Display:
   ═══ SELF ══════════════════════════════════════
   {mind/self.md body content after YAML front matter}

2. Bash: guardrails-read.sh --active → ALL active guardrails
   IF count > 30: note overflow but still load all (guardrails are safety-critical)

3. Bash: reasoning-bank-read.sh --active → ALL active reasoning bank entries
   IF count > 30: note overflow but still load all

4. Read mind/knowledge/beliefs.yaml → filter status in (active, weakened)
   IF file missing: beliefs = [] (skip silently)
```

## Phase 3: Load Category-Specific Knowledge

For each category from Phase 1 (in tier order, respecting budget):

```
1. Bash: retrieve.sh --category {cat} --depth {tier_depth}
   → Returns JSON with: tree_nodes, reasoning_bank, guardrails,
     pattern_signatures, experiences, beliefs, experiential_index

   NOTE: retrieve.sh increments retrieval counters. This is intentional.
   Primed knowledge IS retrieved knowledge — the spaced repetition
   signal is accurate. Knowledge that gets primed is knowledge the
   agent needs.

2. From the result, extract and display:
   - Tree nodes loaded (count + capability levels)
   - Pattern signatures matched (count)
   - Experiences matched (count)

3. IF no categories were identified (empty list):
   Output: "No category-specific context to load."
   (Domain-agnostic stores from Phase 2 are still loaded)
```

## Phase 4: Output Priming Summary

```
═══ PRIMED ════════════════════════════════════
Self: {one-line Self summary from mind/self.md}
Focus: {focus directive from mind/profile.yaml, or "none set"}
State: {IDLE | RUNNING}
Domains loaded:
  - {category}: {N} nodes at {depth}, capability: {level}
  - {category}: {N} nodes at {depth}, capability: {level}
Guardrails: {count} active
Reasoning: {count} entries
Patterns: {count} signatures
Beliefs: {count} active
═══════════════════════════════════════════════

IF IDLE state:
  "Context loaded. Ask me anything about {comma-separated domain list}."

IF RUNNING state:
  (no additional output — boot continues to next step)

IF no mind/ data exists (fresh install, no aspirations):
  "Primed with empty state. Run /start to begin building knowledge."
```

## Invocation Rules

- Does NOT require a session snapshot — reads data stores directly via scripts
- Does NOT modify agent-state, working-memory, handoff, or any state files
- When called from boot: runs after Step 2.5 (snapshot exists for navigation)
- When called from enterPersona: runs without snapshot (direct reads only)
- For auto-continuation, boot passes `--category {goal_category}`

## Chaining

- **Called by**: `/boot` (Step 2.7 full, Step 8.5 continuation), `/enterPersona` (after enabling persona)
- **Calls**: `retrieve.sh`, `guardrails-read.sh`, `reasoning-bank-read.sh`, `aspirations-read.sh` (read-only), `pipeline-read.sh` (read-only)
- **Does NOT call**: `/boot`, `/aspirations`, `/respond`, or any other skill
- **Does NOT modify**: agent-state, working-memory, handoff, or any state files
