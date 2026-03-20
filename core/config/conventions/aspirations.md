# Aspiration JSONL Format

Aspirations use JSONL (one JSON object per line) with script-based access:

## File Layout
- `mind/aspirations.jsonl` — Live active/pending aspirations
- `mind/aspirations-archive.jsonl` — Completed/retired (append-only)
- `mind/aspirations-meta.json` — Metadata (session_count, readiness_gates, last_updated, last_evolution)
- `mind/evolution-log.jsonl` — Evolution events (append-only, was evolution_log in aspirations.yaml)
- `core/config/aspirations-initial.jsonl` — Bootstrap aspiration template (copied by init-mind.sh)

## Script-Based Access (Exclusive Data Layer)
The LLM NEVER reads or edits aspiration JSONL files directly. All operations go through scripts:

| Script | Purpose | Stdin |
|--------|---------|-------|
| `load-aspirations-compact.sh` | Cached compact active aspirations (dedup-aware) | — |
| `aspirations-read.sh --active` | Return active aspirations as full JSON | — |
| `aspirations-read.sh --active-compact` | Compact active aspirations (no descriptions/verification) | — |
| `aspirations-read.sh --id <id>` | Return one aspiration by ID | — |
| `aspirations-read.sh --summary` | Compact one-liner per aspiration | — |
| `aspirations-read.sh --archive` | Return archived aspirations | — |
| `aspirations-read.sh --meta` | Return metadata | — |
| `aspirations-add.sh` | Validate + append new aspiration | JSON |
| `aspirations-update.sh <asp-id>` | Validate + replace aspiration | JSON |
| `aspirations-update-goal.sh <goal-id> <field> <value>` | Update single goal field | — |
| `aspirations-add-goal.sh <asp-id>` | Validate + append goal to aspiration (auto-assigns ID) | JSON |
| `aspirations-complete.sh <asp-id>` | Mark completed + move to archive | — |
| `aspirations-retire.sh <asp-id>` | Mark retired (never-started) + move to archive | — |
| `aspirations-archive.sh` | Sweep completed/retired to archive | — |
| `aspirations-meta-update.sh <field> <value>` | Update metadata field | — |
| `evolution-log-append.sh` | Append evolution event | JSON |

Scripts validate JSON schema before writing. On validation failure: exit non-zero with error.

## Archival Rules
- Completed/retired aspirations move from live → archive via `aspirations-complete.sh`, `aspirations-retire.sh`, or `aspirations-archive.sh`
- Archive file is append-only — never modify archived records
- Live file stays small (only active aspirations)
- `max_active` cap enforced by evolve phase: if over limit, complete lowest-priority/oldest first
