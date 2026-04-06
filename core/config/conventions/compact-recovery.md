# Compact Recovery Convention

Context compaction (autocompact) is normal and expected during long sessions. This convention
defines the full-fidelity recovery protocol for Phase -0.5c of the aspirations loop.

---

## Recovery Chain

```
PreCompact hook → precompact-checkpoint.py saves ALL WM slots to compact-checkpoint.yaml
Stop hook → blocks exit, returns "invoke /aspirations loop"
SessionStart(compact) → postcompact-restore.py injects full state summary into fresh context
Phase -0.5c → compact-restore-slots.sh restores all WM slots from checkpoint
Phase -0.5d → re-reads self.md and program.md (identity context)
Phase -0.5e → resumes blocked-sleep timer if interrupted
```

---

## Phase -0.5c Protocol

When `<agent>/session/compact-checkpoint.yaml` EXISTS:

### Step 1: Full Slot Restoration
```bash
Bash: compact-restore-slots.sh
```

This script:
- Reads `all_slots` from the checkpoint (ALL WM slots, including dynamic ones)
- Restores each non-null slot to working memory with merge logic:
  - **Array slots**: extend (don't overwrite) — prevents losing items added after checkpoint
  - **Map slots**: merge keys (checkpoint values take precedence for non-null keys)
  - **Scalar slots**: direct overwrite with checkpoint value
- Restores `slot_meta` timestamps for age tracking accuracy
- Restores top-level WM keys: `goals_completed_this_session`, `aspiration_touched_last`, `last_goal_category`
- **Skip list**: `archived_context` (stale by definition after compaction)
- Outputs a summary of what was restored

### Step 2: Encoding Queue Processing
Process the encoding queue with budget `min(5, queue_length)`:
- This is a lightweight mid-session encoding pass, not full consolidation
- Violations and high-surprise items get priority
- Items not processed remain in the queue for session-end consolidation

### Step 3: Cleanup
Delete `compact-checkpoint.yaml` (one-shot consumption).

---

## What Gets Checkpointed

The precompact hook (`precompact-checkpoint.py`) saves:

| Field | Source | Purpose |
|-------|--------|---------|
| `all_slots` | Full `slots` dict from WM | ALL slot types including dynamic ones |
| `slot_meta` | WM `slot_meta` dict | Slot age/activity tracking |
| `encoding_queue` | Top-level WM key | Items pending tree encoding |
| `prior_encoding_items` | Accumulated across compactions | Multi-compaction item preservation |
| `goals_completed_this_session` | Top-level WM key | Session progress tracking |
| `aspiration_touched_last` | Top-level WM key | Loop continuity |
| `last_goal_category` | Top-level WM key | Context coherence scoring |
| `retrieval_manifest` | Extracted from `active_context` | Phase 4.26 utilization feedback |
| `blocked_sleep_until` | Slot value | Sleep timer recovery |
| `pending_agents_count` | From `pending-agents.yaml` | Background agent awareness |

Legacy keys (`active_context`, `micro_hypotheses`, `knowledge_debt`, `known_blockers`) are
also included for backward compatibility with older Phase -0.5c implementations.

---

## What Gets Injected into Context

The postcompact restore (`postcompact-restore.py`) prints to stdout:
- **Full active context summary** (no truncation)
- **Loop state** counters (goals_completed, productive_goals, evolutions, etc.)
- **Goals completed this session** (list)
- **Encoding queue** (up to 10 items with scores and targets)
- **All unresolved blockers** (full details)
- **Additional slot state** (strategy, hypothesis, conclusions, sensory buffer, episode chain, domain data)
- **Retrieval manifest** (nodes loaded, deliberation state, utilization feedback status)
- **Execution diary** (last 10 entries — decision points, failures, findings)
- **Reasoning snapshot** (pre-compaction synthesis if available)
- **Pending agents**, **blocked-sleep** warnings
- **Identity reminder** + **action directive**

---

## Execution Diary Integration

The execution diary (`<agent>/session/execution-diary.jsonl`) is an append-only breadcrumb
trail that survives compaction (it's on disk, not in context). The postcompact restore reads
the last 10 entries and includes them in the injected message.

Entry types: `decision`, `failure`, `finding`, `approach_change`, `observation`, `state_update`

Script: `execution-diary.sh append|read|summary|trim`

---

## Reasoning Snapshot Integration

When context enters the tight zone (>=65%), the LLM proactively writes a synthesis to
`<agent>/session/reasoning-snapshot.yaml`. This captures the LLM's own understanding of:
- Current goal and approach
- Tried-and-failed approaches
- Current theory and next step
- Key decisions this session
- Emerging patterns

Script: `reasoning-snapshot.sh write|read|clear`

The postcompact restore reads this file and includes it. This is higher fidelity than WM
slots alone because it's the LLM's synthesized understanding, not just structured data.

---

## Boot Whitelist

These files in `<agent>/session/` MUST survive boot Phase -1.5 cleanup:
- `execution-diary.jsonl`
- `reasoning-snapshot.yaml`
- `execution-diary-session-*.jsonl` (archived diaries from prior sessions)
