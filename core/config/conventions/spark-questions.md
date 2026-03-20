# Spark Questions JSONL Format

Spark questions use JSONL (one JSON object per line) with script-based access:

## File Layout
- `mind/spark-questions.jsonl` — Live spark questions and candidates

## Record Schema (Questions)
Required: `id`, `text`, `category`, `type`
Defaults: `status` ("active"), `times_asked` (0), `sparks_generated` (0), `yield_rate` (0)
Optional: `last_asked`, `last_yielded`, `tags`

## Record Schema (Candidates)
Required: `id`, `text`, `category`, `type`
Defaults: `status` ("candidate")
Optional: `proposed_by`, `proposed_date`

Two record types coexist in one file:
- Questions: `sq-NNN` (zero-padded 3-digit, regex: `^sq-\d{3}$`)
- Candidates: `sq-cNN` (regex: `^sq-c\d{2}$`)
Valid statuses: `active`, `retired`, `candidate`

## Script-Based Access (Exclusive Data Layer)
The LLM NEVER reads or edits `mind/spark-questions.jsonl` directly. All operations go through scripts:

| Script | Purpose | Stdin |
|--------|---------|-------|
| `spark-questions-read.sh --active` | All active questions | — |
| `spark-questions-read.sh --candidates` | All candidate questions | — |
| `spark-questions-read.sh --id <id>` | Single question by ID | — |
| `spark-questions-read.sh --category <cat>` | Questions by category | — |
| `spark-questions-read.sh --summary` | Compact one-liner per question | — |
| `spark-questions-add.sh` | Validate + append new question or candidate | JSON |
| `spark-questions-update-field.sh <id> <field> <value>` | Update single field | — |
| `spark-questions-increment.sh <id> <field>` | Atomic increment (times_asked, sparks_generated) | — |
| `spark-questions-retire.sh <id>` | Set status to retired | — |
| `spark-questions-promote.sh <id> <new-id>` | Promote candidate to active question | — |

All backed by `core/scripts/spark-questions.py` (Python 3, stdlib only).
