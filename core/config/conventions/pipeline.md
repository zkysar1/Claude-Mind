# Pipeline JSONL Format

Hypothesis pipeline records use JSONL (one JSON object per line) with script-based access:

## File Layout
- `world/pipeline.jsonl` — Live records (discovered, active, resolved)
- `world/pipeline-archive.jsonl` — Archived records (append-only)
- `world/pipeline-meta.json` — Metadata (stage_counts, accuracy cache, micro_hypothesis_stats)

## Record Schema
Required: `id`, `title`, `stage`, `horizon`, `type`, `confidence`, `position`, `formed_date`, `category`
Defaults: `slug` (from id), `rationale` (""), `outcome` (null), `reflected` (false), `surprise` (null)
Optional: `outcome_detail`, `outcome_date`, `reflected_date`, `reflected_by`, `verification`, `resolves_by`,
          `resolves_no_earlier_than`, `strategy`, `depth`, `mechanism`, `context_manifest`,
          `context_quality`, `process_score`, `replay_metadata`, `source_validation`, `experience_ref`,
          `evidence_for`, `evidence_override`

ID format: `YYYY-MM-DD_slug` (regex: `^\d{4}-\d{2}-\d{2}_[a-z0-9-]+$`)

## Formation-Quality Gate (move to a non-discovered stage)

`pipeline-add.sh` accepts a skeletal record (only the base Required fields
above), but `pipeline-move.sh` INTO a non-discovered stage (`active` /
`resolved`) enforces `validate_formation_quality` (g-240-36), which is
STRICTER than the add-time schema. Populate these BEFORE the move or it is
rejected with `formation_quality_failed`:

- **`claim` >=20 chars** — required for ANY non-discovered stage; the testable
  assertion. (Discovered stage is exempt when `claim` is absent — it falls back
  to `title` — but a present-yet-<20-char `claim` is rejected even at discovered.)
- **`resolves_by` + a resolution method** — short/long-horizon records must name
  a resolution date (`resolves_by`) AND >=10 chars in one of
  `resolution_criteria` / `resolution_method` / `rationale` (HOW the outcome gets
  decided). Micro/session horizons are exempt (implicit self-check).
- **`measurement_channel` >=5 chars** — short-horizon `active` records must name
  the artifact/log/script/metric that settles the prediction (or
  `verification_channel` / `resolution_source`). Long-horizon exempt (external
  events); discovered exempt (still drafting).

Include these in the INITIAL `pipeline-add.sh` payload to avoid the
add-clean-then-move-rejected round-trip. Source of truth:
`core/scripts/pipeline.py::validate_formation_quality`. (rb-2609)

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

## Tombstone-in-Live Archival (g-115-1986 / g-115-2326)

A move to `archived` does NOT remove the record from `pipeline.jsonl` — it
stays as a `stage=archived` tombstone carrying an `archived_date` (its prune
clock), and the archive copy is appended to `pipeline-archive.jsonl` exactly
once (deduped by id). Rationale: the own-cloud merge is a per-file
union-by-id that cannot express a cross-file removal, so a pre-removal
remote copy used to resurrect archived records at their old stage (94
records, 2026-07-11); the in-place stage flip converges fleet-wide via the
monotonic stage rank. `archive_sweep` maintains tombstones: stamps a missing
`archived_date`, prunes tombstones older than `PRUNE_GRACE_DAYS` (14d,
`pipeline_write.py`), and reports `pruned_count` alongside `archived_count`.
Consequences for readers: `stage=archived` records in the LIVE file are
normal (not corruption); an id may exist in BOTH files by design —
`compute_meta` (CLI + daemon) dedups the join by id so nothing double-counts.

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
