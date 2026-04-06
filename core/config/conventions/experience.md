# Experience Archive JSONL Format

Experience records store full-fidelity interaction traces in JSONL with script-based access.

## File Layout
- `<agent>/experience.jsonl` — Live experience records
- `<agent>/experience-archive.jsonl` — Archived experiences (append-only)
- `<agent>/experience-meta.json` — Metadata (totals, by_type, by_category)
- `<agent>/experience/{id}.md` — Full content files (one per experience)

## Record Schema
Required: `id`, `type`, `created`, `category`, `summary`, `content_path`
Default: `goal_id` (null), `hypothesis_id` (null), `tree_nodes_related` ([]), `verbatim_anchors` ([]), `retrieval_stats` (zeros), `archived` (false), `archived_date` (null)

ID format: `exp-{source-id-or-slug}` (regex: `^exp-[a-z0-9._-]+$`)
Valid types: `goal_execution`, `hypothesis_formation`, `research`, `reflection`, `user_correction`, `user_interaction`, `execution_reflection`

## Script-Based Access (Exclusive Data Layer)
The LLM NEVER reads or edits experience JSONL files directly. All operations go through scripts:

| Script | Purpose | Stdin |
|--------|---------|-------|
| `experience-read.sh --id <id>` | Single record (live then archive) | — |
| `experience-read.sh --category <cat>` | Records by category (live only) | — |
| `experience-read.sh --goal <goal-id>` | Records by goal | — |
| `experience-read.sh --hypothesis <hyp-id>` | Records by hypothesis | — |
| `experience-read.sh --summary` | Compact one-liner per record | — |
| `experience-read.sh --type <type>` | Records by type | — |
| `experience-read.sh --most-retrieved [N]` | Top N by retrieval_count | — |
| `experience-read.sh --least-retrieved [N]` | Bottom N by retrieval_count | — |
| `experience-read.sh --archive` | Archived records | — |
| `experience-read.sh --meta` | Metadata | — |
| `experience-add.sh` | Validate + append record | JSON |
| `experience-update-field.sh <id> <field> <value>` | Update single field (dot notation for nested) | — |
| `experience-archive.sh` | Sweep old/low-utility to archive | — |
| `experience-meta-update.sh <field> <value>` | Update metadata | — |

Scripts validate JSON schema before writing. On validation failure: exit non-zero with error.
All backed by `core/scripts/experience.py` (Python 3, stdlib only).

## Temporal Credit Fields (MR-Search)

Experience records support temporal credit propagation — when a later goal succeeds because of an earlier goal's research, the earlier experience gets credit. Inspired by MR-Search's discounted temporal credit: `A_{i,n} = Σ γ^(n'-n) × r̃_{i,n'}`.

Optional fields on experience records:

```yaml
enabled_by:                          # Causal enablers from prior experiences
  - experience_id: "exp-g-003-02-research"
    relationship: "provided_foundation"  # provided_foundation | corrected_approach | revealed_constraint
    temporal_distance: 3             # Number of goals between enabler and this success
temporal_credit: 0.0                 # Accumulated backward credit from downstream successes
```

- `enabled_by`: Set by Phase 4.27 when execution succeeds and retrieved context from a prior experience was causally helpful.
- `temporal_credit`: Accumulated by Step 8.9 of state update — `credit = downstream_learning_value × 0.9^temporal_distance`.

**Note:** `source_reflection_id` is set on **reasoning bank and guardrail records** (not experience records) by Phase 6.5. Phase 4.26 reads it from those records to track which reflections produced helpful artifacts downstream.

Temporal credit informs strategy extraction (`/reflect-on-self` Patterns mode, Step 3): experiences with high temporal_credit represent "enabling strategies" that set up later success.
