# Working Memory Convention

Working memory (`<agent>/session/working-memory.yaml`) is the agent's session-scoped RAM.
All access goes through dedicated `wm-*.sh` scripts. The LLM MUST NOT read or write
the file directly — all access via scripts.

---

## Schema

```yaml
# Top-level keys (addressed directly: wm-read.sh encoding_queue)
encoding_queue: []                    # Items awaiting tree encoding
session_id: "session-N"
session_start: "YYYY-MM-DDTHH:MM:SS"
goals_completed_this_session: []      # Goal IDs completed this session
aspiration_touched_last: ""           # Last aspiration worked on

# Slot keys (addressed via slot name: wm-read.sh active_context)
slots:
  active_constraints: null            # Active execution constraints
  active_context:                     # Current goal context
    summary: "..."
    experience_refs: []
    retrieval_manifest: null
  active_hypothesis: null             # Hypothesis being evaluated
  active_strategy: null               # Current reasoning strategy
  archived_context:                   # Pointer-only prior session context
    summary: "..."
    experience_refs: []
  cross_domain_transfer: null         # Pattern transfer between domains
  domain_data: null                   # Domain-specific data for current goal
  ephemeral_observation: null         # Temporary noteworthy observation
  knowledge_debt: []                  # Tree nodes needing update
  known_blockers: []                  # Infrastructure/resource blockers
  micro_hypotheses: []                # Inline predictions
  pending_resolutions: null           # Hypotheses near deadline
  recent_violations: []               # Last expectation violations
  sensory_buffer: []                  # Pre-encoding observations
  session_goal: null                  # High-level session objective
  conclusions: []                     # Judgment calls with evidence, for audit (see negative-conclusions.md)

# Parallel metadata (auto-managed by wm.py — never edit directly)
slot_meta:
  active_context:    {updated_at: "...", accessed_at: "...", update_count: N}
  # ... one entry per slot
```

---

## Script API

All scripts in `core/scripts/`. File path is hardcoded to `<agent>/session/working-memory.yaml`.

| Script | Purpose | Side Effects |
|--------|---------|-------------|
| `wm-read.sh [slot] [--json]` | Read slot or full WM | Updates `slot_meta.{slot}.accessed_at` |
| `echo '<json>' \| wm-set.sh <slot>` | Set slot value from stdin | Updates `slot_meta.{slot}.updated_at`, increments `update_count` |
| `echo '<json>' \| wm-append.sh <slot>` | Append to array slot | Adds `_item_ts` to item, enforces array limits, updates meta |
| `wm-clear.sh <slot>` | Null scalars, empty arrays | Updates `slot_meta.{slot}.updated_at` |
| `wm-ages.sh [--json]` | Report all slot ages | Pure read, no side effects |
| `wm-prune.sh [--dry-run]` | Mid-session pruning per config | Prunes stale items, evicts stale scalars |
| `wm-init.sh` | Create from template | Reads slot_types from `core/config/memory-pipeline.yaml` |
| `wm-reset.sh` | Reset to template state | Same as init (session-end consolidation) |

### Slot Addressing

- **Slot names**: `active_context`, `known_blockers`, `micro_hypotheses`, etc.
- **Top-level keys**: `encoding_queue`, `session_id`, `session_start`, `goals_completed_this_session`, `aspiration_touched_last`
- **Dot-path subfields**: `active_context.retrieval_manifest`, `active_context.experience_refs`
- The script auto-routes: top-level keys go to `data[key]`, slot names go to `data["slots"][key]`

### Examples

```bash
# Read a slot
Bash: wm-read.sh known_blockers --json

# Set a slot
echo '{"summary":"Working on g-275-03","experience_refs":[],"retrieval_manifest":null}' | wm-set.sh active_context

# Append to array
echo '{"claim":"Service will scale","confidence":0.8,"category":"api-scaling"}' | wm-append.sh micro_hypotheses

# Read subfield
Bash: wm-read.sh active_context.retrieval_manifest --json

# Set subfield
echo 'false' | wm-set.sh active_context.retrieval_manifest.utilization_pending

# Check ages
Bash: wm-ages.sh --json

# Prune
Bash: wm-prune.sh
```

---

## Dynamic (Ad-Hoc) Slots

Skills can create slots not in the template by calling `wm-set.sh <slot_name>` with any
name not in `TOP_LEVEL_KEYS`. The script routes unknown names to `slots:` and creates both
the slot and its `slot_meta` entry on first write. These slots won't exist after
`wm-init.sh` or `wm-reset.sh` — they're session-scoped unless re-created.

Domain-specific slots (e.g. `infrastructure_recovery_directive`) are created this way at
runtime by domain knowledge articles or forged skills.

---

## Auto-Timestamps

Every `wm-set.sh` and `wm-append.sh` call automatically updates `slot_meta.{slot}.updated_at`.
Every `wm-read.sh <slot>` call updates `slot_meta.{slot}.accessed_at`.
Items appended via `wm-append.sh` get `_item_ts` field with the current local ISO timestamp.

Items without `_item_ts` are treated as old (pre-migration or manually added).

---

## Pruning

Configured in `core/config/memory-pipeline.yaml` under `working_memory_pruning`:

- **Stale threshold** (30 min): Slots not updated in 30 minutes are flagged
- **Evict threshold** (120 min): Non-protected scalar slots auto-nulled after 2 hours
- **Array limits**: Per-slot max items (oldest evicted first by `_item_ts`)
- **Item staleness**: Per-slot age thresholds for array items
- **Protected slots**: `known_blockers` (only prune resolved), `knowledge_debt` (only prune resolved)

Pruning runs in Phase 11 of the aspirations loop via `Bash: wm-prune.sh`.

---

## Lifecycle

1. **Init** (aspirations Phase -1): `Bash: wm-init.sh` creates template, then seed from handoff
2. **Per-goal updates**: Skills use `wm-set.sh`, `wm-append.sh`, `wm-read.sh`
3. **Compact checkpoint**: `precompact-checkpoint.py` reads WM, saves key slots
4. **Checkpoint restore** (Phase -0.5c): Reads checkpoint, restores via `wm-set.sh`
5. **Maintenance** (Phase 11): `wm-ages.sh` + `wm-prune.sh`
6. **Session-end** (consolidation Step 5): `Bash: wm-reset.sh`

---

## Cross-Session Persistence

Only `known_blockers` and `knowledge_debt` survive sessions (via `handoff.yaml`).
Everything else resets. `archived_context` provides compressed pointer to prior session.

---

## Array Slot Schemas

### knowledge_debt items
```yaml
- node_key: "topic-name"
  reason: "Why this node needs updating"
  source_goal: "g-NNN-NN"
  priority: HIGH | MEDIUM | LOW
  created: "YYYY-MM-DD"
  sessions_deferred: 0
  _item_ts: "YYYY-MM-DDTHH:MM:SS"   # Auto-added by wm-append.sh
```

### known_blockers items
See `core/config/conventions/handoff-working-memory.md` for full schema.

### micro_hypotheses items
```yaml
- claim: "Short prediction"
  confidence: 0.0-1.0
  formed: "HH:MM:SS"
  outcome: null | confirmed | corrected
  surprise: 0-10
  category: "category-slug"
  _item_ts: "YYYY-MM-DDTHH:MM:SS"
```

### encoding_queue items
```yaml
- source_goal: "g-NNN-NN"
  observation: "What was learned"
  encoding_score: 0.0-1.0
  scores: {novelty, outcome_impact, surprise, goal_relevance, repetition_strength}
  target_article: "world/knowledge/tree/{path}.md"
  replay_priority: "violations | high_surprise | routine_observations"
  _item_ts: "YYYY-MM-DDTHH:MM:SS"
```
