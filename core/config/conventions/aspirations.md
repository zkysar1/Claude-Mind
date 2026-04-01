# Aspiration JSONL Format

Aspirations use JSONL (one JSON object per line) with script-based access:

## File Layout

### World Queue (collective task list — `world/`)
- `world/aspirations.jsonl` — Live active/pending world aspirations
- `world/aspirations-archive.jsonl` — Completed/retired (append-only)
- `world/aspirations-meta.json` — Metadata (session_count, readiness_gates)

### Agent Queue (per-agent local tasks — `<agent>/`)
- `<agent>/aspirations.jsonl` — Agent's local work queue (maintenance, decomposed sub-goals)
- `<agent>/aspirations-archive.jsonl` — Agent's completed local tasks
- `<agent>/aspirations-meta.json` — Agent aspiration metadata

### Shared
- `meta/evolution-log.jsonl` — Evolution events (append-only)
- `core/config/world-aspirations-initial.jsonl` — World bootstrap aspirations (copied by init-world.sh)
- `core/config/agent-aspirations-initial.jsonl` — Agent maintenance goals (copied by init-agent.sh)
- `core/config/agent-aspirations-onboard.jsonl` — Onboarding aspiration for subsequent agents

## Script-Based Access (Exclusive Data Layer)

The LLM NEVER reads or edits aspiration JSONL files directly. All operations go through scripts.
Two script families — world (default) and agent — operate on separate queues via the same Python engine.

### World Queue Scripts (default — operate on `world/aspirations.jsonl`)

| Script | Purpose | Stdin |
|--------|---------|-------|
| `load-aspirations-compact.sh` | Cached compact active aspirations (dedup-aware) | — |
| `aspirations-read.sh --active` | Return active world aspirations as full JSON | — |
| `aspirations-read.sh --active-compact` | Compact active aspirations (no descriptions/verification) | — |
| `aspirations-read.sh --id <id>` | Return one world aspiration by ID | — |
| `aspirations-read.sh --summary` | Compact one-liner per world aspiration | — |
| `aspirations-read.sh --archive` | Return archived world aspirations | — |
| `aspirations-read.sh --meta` | Return world aspirations metadata | — |
| `aspirations-add.sh` | Validate + append new world aspiration | JSON |
| `aspirations-update.sh <asp-id>` | Validate + replace world aspiration | JSON |
| `aspirations-update-goal.sh <goal-id> <field> <value>` | Update single goal field in world queue | — |
| `aspirations-add-goal.sh <asp-id>` | Validate + append goal to world aspiration (auto-assigns ID) | JSON |
| `aspirations-complete.sh <asp-id>` | Mark world aspiration completed + archive | — |
| `aspirations-retire.sh <asp-id>` | Mark world aspiration retired + archive | — |
| `aspirations-archive.sh` | Sweep completed/retired world aspirations to archive | — |
| `aspirations-meta-update.sh <field> <value>` | Update world aspirations metadata | — |
| `evolution-log-append.sh` | Append evolution event | JSON |

### World-Only Operations (no agent equivalent)

| Script | Purpose |
|--------|---------|
| `aspirations.py claim <goal-id> <agent-name>` | Atomically claim a world goal for an agent |
| `aspirations.py release <goal-id>` | Release a claimed world goal |
| `aspirations.py complete-by <goal-id> <agent-name>` | Mark world goal completed with agent attribution |

### Agent Queue Scripts (operate on `<agent>/aspirations.jsonl`)

| Script | Purpose | Stdin |
|--------|---------|-------|
| `agent-aspirations-read.sh --active` | Return active agent aspirations | — |
| `agent-aspirations-read.sh --active-compact` | Compact active agent aspirations (no descriptions/verification) | — |
| `agent-aspirations-read.sh --id <id>` | Return one agent aspiration by ID | — |
| `agent-aspirations-read.sh --summary` | Compact one-liner per agent aspiration | — |
| `agent-aspirations-read.sh --archive` | Return archived agent aspirations | — |
| `agent-aspirations-read.sh --meta` | Return agent aspirations metadata | — |
| `agent-aspirations-add.sh` | Validate + append new agent aspiration | JSON |
| `agent-aspirations-update.sh <asp-id>` | Validate + replace agent aspiration | JSON |
| `agent-aspirations-update-goal.sh <goal-id> <field> <value>` | Update single goal field in agent queue | — |
| `agent-aspirations-add-goal.sh <asp-id>` | Validate + append goal to agent aspiration | JSON |
| `agent-aspirations-complete.sh <asp-id>` | Mark agent aspiration completed + archive | — |
| `agent-aspirations-retire.sh <asp-id>` | Mark agent aspiration retired + archive | — |
| `agent-aspirations-archive.sh` | Sweep completed/retired agent aspirations to archive | — |

Scripts validate JSON schema before writing. On validation failure: exit non-zero with error.

### Under the Hood

Both script families delegate to `aspirations.py` with a `--source {world|agent}` flag.
Agent wrappers pass `--source agent`; world wrappers use the default (`world`).
Goal-selector reads from BOTH queues and tags candidates with `source: "world"` or `source: "agent"`.

## Archival Rules
- Completed/retired aspirations move from live → archive via `aspirations-complete.sh`, `aspirations-retire.sh`, or `aspirations-archive.sh`
- Archive file is append-only — never modify archived records
- Live file stays small (only active aspirations)
- `max_active` cap enforced by evolve phase: if over limit, complete lowest-priority/oldest first
- **Recurring goal protection (data layer enforced):**
  - `aspirations-complete.sh` and `aspirations-retire.sh` **refuse** aspirations with recurring goals (exit 1 with BLOCKED message). Use `--force` to override.
  - `aspirations-archive.sh` (sweep) auto-recovers such aspirations to `active` status and resets corrupted recurring goals to `pending`.
  - `aspirations-update-goal.sh` **blocks** setting `status=completed` on recurring goals. Use `complete-by` for cycle tracking.
  - `recompute_progress` excludes recurring goals from completion counts. Summary shows `+ N recurring` suffix.
  - These guards prevent LLM drift from killing recurring goals by archiving their parent aspiration.
- **Premature-archival protection (data layer enforced):**
  - `aspirations-complete.sh` **refuses** aspirations where any non-recurring goal is not in a terminal status (`completed`, `skipped`, `expired`, `decomposed`). Exit 1 with BLOCKED message listing unfinished goals. Use `--force` to override.
  - `aspirations-retire.sh` **warns** (stderr) when retiring aspirations with unfinished goals, but does not block — retirement is intentional abandonment.
  - `aspirations-archive.sh` (sweep) auto-recovers completed aspirations with unfinished non-recurring goals to `active` status (same pattern as recurring-goal recovery).
  - These guards prevent post-autocompact narrative fabrication from archiving aspirations before their goals are actually done.
