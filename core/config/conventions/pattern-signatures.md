# Pattern Signatures JSONL Format

Pattern signatures use JSONL (one JSON object per line) with script-based access:

## File Layout
- `world/pattern-signatures.jsonl` — Live pattern signature entries

## Record Schema

**Canonical field list lives in `core/config/schema-registry.yaml`** (single source of
truth). The registry is what `schema-drift-sweep.py` validates against every week; edit
it when the live record schema changes, and update pseudocode in SKILL.md files in the
same change. The sweep catches anyone who forgets.

Quick reference (see registry for the full list):
- Core: `id`, `name`, `description`, `conditions`, `expected_outcome`, `created`, `status`
- Outcome tracking: `outcome_stats.{confirmed, total, accuracy}` — nested counters
- Utilization: `utilization.{retrieval_count, last_retrieved}`
- Optional: `category`, `capability_level`, `confused_with`, `tags`, `validation_status`

Legacy field names (`hit_rate`, `times_triggered`, `false_positive_rate`, `false_positives`)
were replaced by `outcome_stats.*` and are actively tracked for drift in the registry's
`stale_fields` map. DO NOT use these names in new pseudocode.

ID format: `sig-NNN` (zero-padded 3-digit, regex: `^sig-\d{3}$`)
Valid statuses: `active`, `retired`, `contradicted`

## Script-Based Access (Exclusive Data Layer)
The LLM NEVER reads or edits `world/pattern-signatures.jsonl` directly. All operations go through scripts:

| Script | Purpose | Stdin |
|--------|---------|-------|
| `pattern-signatures-read.sh --active` | All active signatures | — |
| `pattern-signatures-read.sh --id <id>` | Single signature by ID | — |
| `pattern-signatures-read.sh --category <cat>` | Signatures by category | — |
| `pattern-signatures-read.sh --summary` | Compact one-liner per signature | — |
| `pattern-signatures-add.sh` | Validate + append new signature | JSON |
| `pattern-signatures-update.sh <id>` | Validate + replace signature | JSON |
| `pattern-signatures-update-field.sh <id> <field> <value>` | Update single field | — |
| `pattern-signatures-record-outcome.sh <id> <outcome>` | Append outcome + recalculate confidence | — |
| `pattern-signatures-set-status.sh <id> <status>` | Change signature status | — |

All backed by `core/scripts/pattern-signatures.py` (Python 3, stdlib only).
