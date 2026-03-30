# Pattern Signatures JSONL Format

Pattern signatures use JSONL (one JSON object per line) with script-based access:

## File Layout
- `world/pattern-signatures.jsonl` — Live pattern signature entries

## Record Schema
Required: `id`, `name`, `description`, `conditions`, `expected_outcome`, `created`
Defaults: `status` ("active"), `confidence` (0.5), `outcome_history` ([]), `utilization` (zeros)
Optional: `category`, `capability_level`, `confused_with`, `tags`

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
