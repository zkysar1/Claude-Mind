# Reasoning Bank JSONL Format

Reasoning bank entries use JSONL (one JSON object per line) with script-based access:

## File Layout
- `world/reasoning-bank.jsonl` — Live reasoning bank entries

## Record Schema
Required: `id`, `title`, `type`, `category`, `content`, `created`
Defaults: `status` ("active"), `when_to_use` (""), `utilization` (zeros)
Optional: `source_goal`, `source_hypothesis`, `tags`, `related_entries`, `experience_ref`, `preventive_guardrail`, `entry_type`, `poignancy`

`experience_ref` (format `exp-SLUG`, see `experience.md`) links the lesson
to the full-fidelity trace it was learned from. Completes the evidence
chain `guardrail → reasoning bank → experience → goal`. Optional — may be
null when the lesson is user-provided or not tied to a specific execution.

`preventive_guardrail` is intentionally dual-purpose:

- **Linked form**: a `guard-NNN` ID (optionally a comma-separated list
  like `"guard-340,guard-341"`) pointing at already-filed guardrails that
  enforce the RB entry's lesson.
- **Candidate form**: prose describing the rule that *should* become a
  guardrail but has not been filed yet (e.g., rb-464's "guard-stale-narrative
  (to be filed): ..."). This is a backlog signal — the author is capturing
  the rule text inline so it can be filed later in a guardrail-mining pass.

Both shapes are schema-valid. The `learning-routing-audit.sh` script classifies
them at read time: linked-form IDs that don't resolve are true drift; prose
values surface under `guardrail_candidates_unfiled` as an opportunity queue,
not a schema error. Mining candidate-form prose into real guardrails drains
the queue over time without forced migration.

ID format: `rb-NNN` (zero-padded 3-digit, regex: `^rb-\d{3}$`)
Valid types: `success`, `failure`, `user_provided`
Valid statuses: `active`, `retired`
Valid `entry_type`: `procedure` (or null — the default)

### `entry_type` — optional reasoning-bank taxonomy (g-306-11)

`entry_type` is an OPTIONAL tag that classifies an entry by SHAPE, orthogonal
to `type` (success/failure) and `category` (the topic):

- **null** (default) — an ordinary reasoning lesson (heuristic, diagnostic,
  causal insight). The overwhelming majority of entries.
- **`procedure`** — a reusable, repeatable MULTI-STEP how-to: an ordered
  sequence of steps to follow when a recurring class of task appears (e.g.
  "to add an optional field to a daemon-mirrored store: update both validators
  verbatim-in-sync, both defaults, then the retrieve filter + wrapper + doc +
  tests"). Set this only when `content` actually spells out reusable steps.

Additive + null-safe: the RB validator has no unknown-field gate, and entries
written before this field existed read back as null. **NO embeddings, NO new
store** — `entry_type` is a single optional string on the existing JSONL
record. Set it at write time by including `"entry_type": "procedure"` in the
`reasoning-bank-add.sh` stdin JSON; retrofit an existing entry with
`reasoning-bank-update-field.sh <id> entry_type procedure`.

**Retrieval filter**: `retrieve.sh --entry-type procedure --category <cat>`
restricts the returned `reasoning_bank` + `meta_lessons` to procedure-tagged
entries (filter applied before sort/cap/counter-bump, so non-procedure
entries' `retrieval_count` is never polluted). Omitting `--entry-type` is the
default and returns all entry types unchanged. Producers: `/aspirations-spark`
Phase 6.5 (general pattern capture) and `/aspirations-state-update`'s
procedure-encoding step (reusable multi-step procedures specifically).

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
Defaults: `status` ("active"), `utilization` ({`times_active`: 0, `times_skipped`: 0, `times_helpful`: 0, `times_noise`: 0, `retrieval_count`: 0, `utilization_score`: 0.0}). Authoritative field list: `core/scripts/reasoning-bank.py` `UTILIZATION_COUNTERS`. No top-level `times_triggered` — that field belongs to `pattern-signatures.jsonl`, not guardrails.
Optional: `tags`, `related_patterns`, `violation_history`, `experience_ref`

`experience_ref` (format `exp-SLUG`, see `experience.md`) links the
prescriptive rule to the full-fidelity trace it was learned from. Same
schema and semantics as on reasoning-bank records. Optional.

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

All backed by the `core/scripts/guardrails-*.sh` wrappers above (Python 3, stdlib only). Direct read/write of `world/guardrails.jsonl` is prohibited — use the wrappers exclusively.

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
Side effects: increments `utilization.times_active` on matched (unless `--dry-run`). Pre-2026-05-09 also incremented `times_skipped` on every non-matching active record per call; the audit found this fired hundreds of times per session and inflated skip counters by 2-15x retrieval count, so the increment was removed. The semantically correct `times_skipped` writer is `reflect-bookkeeping.py` `cmd_utilization_delta` (LLM deliberation marks items as skipped). The `utilization-stats.py` exposure floor was simultaneously narrowed to `retrieval_count` alone — see that script's docstring.

All backed by `core/scripts/guardrail-check.py` (Python 3, stdlib only).

---

# Operational Gotcha Convention

Entries representing operational friction knowledge — error patterns, environment
quirks, debugging lessons, infrastructure footguns — SHOULD include the tag
`ops-gotcha` in their `tags` array. This applies to both reasoning bank and
guardrail entries.

Examples of operational gotchas:
- "Always use `export` for env vars when scripts call Python subprocesses"
- "Mock must patch at the import location, not the definition location"
- "Compact checkpoint file can exceed expected size after 50+ goals"

## Store Selection

- **Reasoning bank** for diagnostic gotchas ("when you see X, the cause is Y"):
  `type: "failure"` (self-discovered) or `type: "user_provided"` (told by user).
  Include the error pattern or symptom in `when_to_use.conditions`.
- **Guardrails** for prescriptive gotchas ("always do X" / "never do Y"):
  use `trigger_condition` to describe when the rule applies.

Both: include `"ops-gotcha"` in `tags`.

For routing beyond this RB-vs-guardrail pair (tree, pipeline, experience,
locators, journal, working memory, etc.), see
`core/config/conventions/learning-routing.md`. Multi-store encoding pairs
(e.g., RB + guardrail linked via `preventive_guardrail`) are documented there.

## Encoding Triggers

Operational gotchas are encoded by three paths:
1. **Phase 6.5 auto-detection** — structural keyword scan after goal execution (mandatory when signals present)
2. **`/respond` Step 7.5 OPS_GOTCHA** — when user shares operational friction knowledge
3. **Consolidation Step 0.7** — safety net sweep of session journal for unencoded error-then-fix patterns
