# Reasoning Bank JSONL Format

Reasoning bank entries use JSONL (one JSON object per line) with script-based access:

## File Layout
- `world/reasoning-bank.jsonl` — Live reasoning bank entries

## Record Schema
Required: `id`, `title`, `type`, `category`, `content`, `created`
Defaults: `status` ("active"), `when_to_use` (""), `utilization` (zeros)
Optional: `source_goal`, `source_hypothesis`, `tags`, `related_entries`

ID format: `rb-NNN` (zero-padded 3-digit, regex: `^rb-\d{3}$`)
Valid types: `success`, `failure`, `user_provided`
Valid statuses: `active`, `retired`

## Script-Based Access (Exclusive Data Layer)
The LLM NEVER reads or edits `world/reasoning-bank.jsonl` directly. All operations go through scripts:

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
- `world/guardrails.jsonl` — Live guardrail entries

## Record Schema
Required: `id`, `rule`, `category`, `trigger_condition`, `source`, `created`
Defaults: `status` ("active"), `times_triggered` (0), `utilization` (zeros)
Optional: `tags`, `related_patterns`, `violation_history`

ID format: `guard-NNN` (zero-padded 3-digit, regex: `^guard-\d{3}$`)
Valid statuses: `active`, `retired`

## Script-Based Access (Exclusive Data Layer)
The LLM NEVER reads or edits `world/guardrails.jsonl` directly. All operations go through scripts:

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

---

# Operational Gotcha Convention

Entries representing operational friction knowledge — error patterns, environment
quirks, debugging lessons, infrastructure footguns — SHOULD include the tag
`ops-gotcha` in their `tags` array. This applies to both reasoning bank and
guardrail entries.

Examples of operational gotchas:
- "Always use `export` for env vars when scripts call Python subprocesses"
- "boto3 mock must patch at the import location, not the definition location"
- "Compact checkpoint file can exceed expected size after 50+ goals"

## Store Selection

- **Reasoning bank** for diagnostic gotchas ("when you see X, the cause is Y"):
  `type: "failure"` (self-discovered) or `type: "user_provided"` (told by user).
  Include the error pattern or symptom in `when_to_use.conditions`.
- **Guardrails** for prescriptive gotchas ("always do X" / "never do Y"):
  use `trigger_condition` to describe when the rule applies.

Both: include `"ops-gotcha"` in `tags`.

## Encoding Triggers

Operational gotchas are encoded by three paths:
1. **Phase 6.5 auto-detection** — structural keyword scan after goal execution (mandatory when signals present)
2. **`/respond` Step 7.5 OPS_GOTCHA** — when user shares operational friction knowledge
3. **Consolidation Step 0.7** — safety net sweep of session journal for unencoded error-then-fix patterns
