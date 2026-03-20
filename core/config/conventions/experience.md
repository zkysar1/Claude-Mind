# Experience Archive JSONL Format

Experience records store full-fidelity interaction traces in JSONL with script-based access.

## File Layout
- `mind/experience.jsonl` — Live experience records
- `mind/experience-archive.jsonl` — Archived experiences (append-only)
- `mind/experience-meta.json` — Metadata (totals, by_type, by_category)
- `mind/experience/{id}.md` — Full content files (one per experience)

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
