# Pipeline JSONL Format

Hypothesis pipeline records use JSONL (one JSON object per line) with script-based access:

## File Layout
- `mind/pipeline.jsonl` — Live records (discovered, evaluating, active, resolved)
- `mind/pipeline-archive.jsonl` — Archived records (append-only)
- `mind/pipeline-meta.json` — Metadata (stage_counts, accuracy cache, micro_hypothesis_stats)

## Record Schema
Required: `id`, `title`, `stage`, `horizon`, `type`, `confidence`, `position`, `formed_date`, `category`
Defaults: `slug` (from id), `rationale` (""), `outcome` (null), `reflected` (false), `surprise` (null)
Optional: `outcome_detail`, `outcome_date`, `reflected_date`, `verification`, `resolves_by`,
          `resolves_no_earlier_than`, `strategy`, `depth`, `mechanism`, `context_manifest`,
          `context_quality`, `process_score`, `replay_metadata`, `source_validation`, `experience_ref`

ID format: `YYYY-MM-DD_slug` (regex: `^\d{4}-\d{2}-\d{2}_[a-z0-9-]+$`)

## Script-Based Access (Exclusive Data Layer)
The LLM NEVER reads or edits pipeline JSONL files directly. All operations go through scripts:

| Script | Purpose | Stdin |
|--------|---------|-------|
| `pipeline-read.sh --stage <s>` | All records in stage | — |
| `pipeline-read.sh --id <id>` | Single record (live then archive) | — |
| `pipeline-read.sh --summary` | Compact one-liner per record | — |
| `pipeline-read.sh --counts` | Stage counts from meta | — |
| `pipeline-read.sh --accuracy` | Accuracy report from meta | — |
| `pipeline-read.sh --unreflected` | Resolved + reflected=false | — |
| `pipeline-read.sh --replay-candidates` | Spaced repetition filter | — |
| `pipeline-read.sh --archive` | Archived records | — |
| `pipeline-read.sh --meta` | Full metadata | — |
| `pipeline-add.sh` | Validate + append (default stage=discovered) | JSON |
| `pipeline-update.sh <id>` | Validate + replace record | JSON |
| `pipeline-update-field.sh <id> <field> <value>` | Update single field | — |
| `pipeline-move.sh <id> <stage>` | Move between stages (optional stdin JSON merge) | JSON |
| `pipeline-archive.sh` | Sweep old resolved to archive | — |
| `pipeline-recompute-meta.sh` | Full recount from records | — |
| `pipeline-meta-update.sh <field> <value>` | Update single meta field | — |

Key design: `pipeline-move.sh` with stdin merge enables atomic resolve operations:
```bash
echo '{"outcome":"CONFIRMED","surprise":2,"outcome_date":"2026-03-09"}' | pipeline-move.sh <id> resolved
```

Scripts validate JSON schema before writing. On validation failure: exit non-zero with error.
All backed by `core/scripts/pipeline.py` (Python 3, stdlib only except PyYAML for migration).
