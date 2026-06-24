# Pipeline JSONL Format

Hypothesis pipeline records use JSONL (one JSON object per line) with script-based access:

## File Layout
- `world/pipeline.jsonl` — Live records (discovered, active, resolved)
- `world/pipeline-archive.jsonl` — Archived records (append-only)
- `world/pipeline-meta.json` — Metadata (stage_counts, accuracy cache, micro_hypothesis_stats)

## Record Schema
Required: `id`, `title`, `stage`, `horizon`, `type`, `confidence`, `position`, `formed_date`, `category`
Defaults: `slug` (from id), `rationale` (""), `outcome` (null), `reflected` (false), `surprise` (null)
Optional: `outcome_detail`, `outcome_date`, `reflected_date`, `verification`, `resolves_by`,
          `resolves_no_earlier_than`, `strategy`, `depth`, `mechanism`, `context_manifest`,
          `context_quality`, `process_score`, `replay_metadata`, `source_validation`, `experience_ref`,
          `evidence_for`, `evidence_override`

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

## Resolution-Evidence Requirement (g-303-27)

A move INTO the `resolved` stage with `outcome` of `CONFIRMED` or `CORRECTED`
is REJECTED (`400 resolution_evidence_required`) unless the record carries at
least one verifiable external-evidence pointer, so the accuracy number is
independently auditable (from the g-303-15 calibration-honesty audit: ~53% of
CONFIRMED/CORRECTED records had no `outcome_detail` at all). Enforced at the
resolution single-writer (the daemon `pipeline_write.move` -> resolved, plus
`add` at `stage=resolved`); `update`/`update-field` are tweak paths and stay
ungated so legacy evidence-less records can still be edited.

An evidence pointer is any of: a structured `experience_ref` / `evidence_for`
field, OR a recognized shape in `outcome_detail` / `rationale` / `verification`
/ `links` — a goal-id (`g-NNN-NN`), commit SHA, `file:line`, session-id, an
`rb-`/`guard-`/`exp-`/`msg-` id, a canonical-script name (`foo.sh`/`foo.py`),
or a percentage with measurement context. The detector is GENEROUS (any one
shape passes); the precise compliance rate is tracked separately as
`accuracy.evidence_pct` in `pipeline-meta.json` (surfaced via
`pipeline-read.sh --accuracy`).

`EXPIRED` / `UNRESOLVABLE` outcomes are exempt (no prediction was validated).
Escape hatch: set `evidence_override` to a short non-empty reason string in the
resolve merge JSON (e.g. a math proof where the derivation IS the evidence).
The reason persists in the record (more auditable than a transient CLI flag).
