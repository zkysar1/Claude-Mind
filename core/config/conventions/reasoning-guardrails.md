# Reasoning Bank JSONL Format

Reasoning bank entries use JSONL (one JSON object per line) with script-based access:

## File Layout
- `mind/reasoning-bank.jsonl` — Live reasoning bank entries

## Record Schema
Required: `id`, `title`, `type`, `category`, `content`, `created`
Defaults: `status` ("active"), `when_to_use` (""), `utilization` (zeros)
Optional: `source_goal`, `source_hypothesis`, `tags`, `related_entries`

ID format: `rb-NNN` (zero-padded 3-digit, regex: `^rb-\d{3}$`)
Valid types: `success`, `failure`, `user_provided`
Valid statuses: `active`, `retired`

## Script-Based Access (Exclusive Data Layer)
The LLM NEVER reads or edits `mind/reasoning-bank.jsonl` directly. All operations go through scripts:

| Script | Purpose | Stdin |
|--------|---------|-------|
| `reasoning-bank-read.sh --active` | All active entries | — |
| `reasoning-bank-read.sh --id <id>` | Single entry by ID | — |
| `reasoning-bank-read.sh --category <cat>` | Entries by category | — |
| `reasoning-bank-read.sh --type <type>` | Entries by type | — |
| `reasoning-bank-read.sh --summary` | Compact one-liner per entry | — |
| `reasoning-bank-add.sh` | Validate + append new entry | JSON |
| `reasoning-bank-update-field.sh <id> <field> <value>` | Update single field | — |
| `reasoning-bank-increment.sh <id> <field>` | Atomic increment of utilization field | — |

All backed by `core/scripts/reasoning-bank.py` (Python 3, stdlib only).

---

# Guardrails JSONL Format

Guardrails use JSONL (one JSON object per line) with script-based access:

## File Layout
- `mind/guardrails.jsonl` — Live guardrail entries

## Record Schema
Required: `id`, `rule`, `category`, `trigger_condition`, `source`, `created`
Defaults: `status` ("active"), `times_triggered` (0), `utilization` (zeros)
Optional: `tags`, `related_patterns`, `violation_history`

ID format: `guard-NNN` (zero-padded 3-digit, regex: `^guard-\d{3}$`)
Valid statuses: `active`, `retired`

## Script-Based Access (Exclusive Data Layer)
The LLM NEVER reads or edits `mind/guardrails.jsonl` directly. All operations go through scripts:

| Script | Purpose | Stdin |
|--------|---------|-------|
| `guardrails-read.sh --active` | All active guardrails | — |
| `guardrails-read.sh --id <id>` | Single guardrail by ID | — |
| `guardrails-read.sh --category <cat>` | Guardrails by category | — |
| `guardrails-read.sh --summary` | Compact one-liner per guardrail | — |
| `guardrails-add.sh` | Validate + append new guardrail | JSON |
| `guardrails-update-field.sh <id> <field> <value>` | Update single field | — |
| `guardrails-increment.sh <id> <field>` | Atomic increment of utilization/trigger field | — |

All backed by `core/scripts/guardrails.py` (Python 3, stdlib only).

---

# Guardrail Check Script Access

Guardrail matching is implemented by `core/scripts/guardrail-check.py`. The script
deterministically matches active guardrails against context/outcome/phase filters
using keyword matching on guardrail text fields. Replaces manual LLM matching.

| Script | Purpose | Stdin |
|--------|---------|-------|
| `guardrail-check.sh --context <infrastructure\|local\|any> [--outcome <succeeded\|failed\|any>] [--phase <post-execution\|pre-selection>] [--dry-run]` | Match guardrails against filters | — |

Output: JSON with `matched` array (each entry: `id`, `rule`, `category`, `action_hint`) and `matched_count`.
`action_hint` extracts executable script commands from rule text (e.g., `domain-check.sh check --since 30`).
Side effects: increments `utilization.times_active` on matched, `times_skipped` on unmatched (unless `--dry-run`).

All backed by `core/scripts/guardrail-check.py` (Python 3, stdlib only).
