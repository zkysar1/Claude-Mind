# Agent State Machine

- State file: `<agent>/session/agent-state` (plain text, no YAML)
- Valid values: `RUNNING`, `IDLE`. Absence = UNINITIALIZED.
- ONLY `/start` and `/stop` may write to this file (via `session-state-set.sh`)
- Claude MUST NOT modify agent-state under any circumstances
- Boot and aspirations check agent-state before executing (defense in depth)
- All reads via `session-state-get.sh`, all writes via `session-state-set.sh`

---

# Agent Mode

- Mode file: `<agent>/session/agent-mode` (plain text, no YAML)
- Valid values: `reader`, `assistant`, `autonomous`. Absence = `reader` (safe default).
- ONLY `/start` and `/stop` may write to this file (via `session-mode-set.sh`)
- Claude MUST NOT modify agent-mode under any circumstances
- Skills check mode at entry via `session-mode-get.sh` and refuse if insufficient
- All reads via `session-mode-get.sh`, all writes via `session-mode-set.sh`

Mode is the single user-facing control. State and persona are derived at the agent level:
- reader → IDLE, persona light (knowledge access, no agent character)
- assistant → IDLE, persona full (agent identity, tone, personality)
- autonomous → RUNNING, persona full + perpetual loop

Observer sessions (see below) are the exception: they run reader/assistant mode while the
agent-level state remains RUNNING. They do not write to mode or state files.

Mode-specific behavioral rules live in `core/config/modes/{mode}.md`.

---

# Observer Sessions

When an agent is RUNNING (autonomous mode), other sessions can connect as **observers**
via `/start <agent> --mode reader` or `/start <agent> --mode assistant`.

## Rules

1. Observer sessions MUST NOT write to: `agent-state`, `agent-mode`, `persona-active`, `running-session-id`
2. Observer sessions MUST NOT call: `session-state-set.sh`, `session-mode-set.sh`, `session-persona-set.sh`
3. Observer sessions still bind via `.active-agent-<SID>` (per-session, no contention)
4. Mode is tracked in-memory from the `/start` flow — no file needed
5. The stop hook Gate 0 handles observers automatically (SID ≠ runner SID → allow stop)

## Concurrency Safety

- **Reader observers**: Fully safe — zero writes, zero contention
- **Assistant observers**: Writes to knowledge tree (`.md` files) and JSONL stores
  (via append-only scripts) are generally safe. Working memory (`wm-*.sh`) has no
  cross-process locking — concurrent writes may silently overwrite. User is warned.

---

# Session State Script Access

Session control files in `<agent>/session/` are accessed exclusively via scripts.
The LLM MUST NOT read or write `agent-state`, `agent-mode`, `persona-active`, signal files,
or `stop-block-count` directly. All access goes through scripts:

| Script | Purpose |
|--------|---------|
| `session-state-get.sh` | Returns: RUNNING, IDLE, or UNINITIALIZED |
| `session-state-set.sh <value>` | Validates and writes (RUNNING or IDLE only) |
| `session-mode-get.sh` | Returns: reader, assistant, autonomous (default: reader) |
| `session-mode-set.sh <value>` | Validates and writes (reader, assistant, or autonomous only) |
| `session-persona-get.sh` | Returns: true, false, or unset |
| `session-persona-set.sh <value>` | Validates and writes (true or false only) |
| `session-signal-set.sh <name>` | Creates marker file (loop-active or stop-loop only) |
| `session-signal-clear.sh <name>` | Removes marker file |
| `session-signal-exists.sh <name>` | Exit 0 if exists, exit 1 if not |
| `session-counter-get.sh` | Returns stop-block-count integer (0 if missing) |
| `session-counter-increment.sh` | Atomic increment, returns new value |
| `session-counter-clear.sh` | Removes counter file |

All backed by `core/scripts/session.py` (Python 3, stdlib only).

---

# Generic YAML Store (agent directory files)

YAML state files in the agent directory are accessed via generic scripts.
File paths are relative to the agent directory (`$AGENT_DIR`). Path traversal outside it is rejected.

| Script | Purpose |
|--------|---------|
| `mind-read.sh <file> [--field <path>] [--json]` | Read file or specific field |
| `mind-set.sh <file> <path> <value> [--string]` | Set a scalar field (auto-detects type) |
| `mind-increment.sh <file> <path>` | Increment numeric field, prints new value |
| `mind-append.sh <file> <path>` | Append JSON from stdin to array field |
| `mind-write.sh <file>` | Full file replacement from stdin (JSON or YAML) |

Dot-notation for nested access: `current_assessment.resolved_hypotheses`.
Numeric segments index into arrays: `gaps.0.status`.
All backed by `core/scripts/mind-yaml.py` (Python 3, PyYAML).

---

# Working Memory Scripts

Working memory (`<agent>/session/working-memory.yaml`) has its own dedicated script family.
The LLM MUST NOT read or write `working-memory.yaml` directly — all access via `wm-*.sh`.
Full schema and pruning rules: `core/config/conventions/working-memory.md`.

| Script | Purpose |
|--------|---------|
| `wm-read.sh [slot] [--json]` | Read slot or full WM (updates accessed_at) |
| `echo '<json>' \| wm-set.sh <slot>` | Set slot value (updates updated_at) |
| `echo '<json>' \| wm-append.sh <slot>` | Append to array slot (auto-adds _item_ts) |
| `wm-clear.sh <slot>` | Null scalars, empty arrays |
| `wm-ages.sh [--json]` | Report all slot ages |
| `wm-prune.sh [--dry-run]` | Mid-session pruning per config thresholds |
| `wm-init.sh` | Initialize from template (Phase -1) |
| `wm-reset.sh` | Reset to template (consolidation Step 5) |

All backed by `core/scripts/wm.py` (Python 3, PyYAML).

---

# Compact Checkpoint (PreCompact / SessionStart Hooks)

When autocompact fires, `PreCompact` hook saves encoding state before context compression:

- **File**: `<agent>/session/compact-checkpoint.yaml`
- **Written by**: `core/scripts/precompact-checkpoint.sh` (PreCompact hook, matcher: auto)
- **Injected by**: `core/scripts/postcompact-restore.sh` (SessionStart hook, matcher: compact — stdout injected into context)
- **Consumed by**: aspirations loop Phase -0.5c (processes encoding queue in fresh context, then deletes checkpoint)

The checkpoint accumulates across multiple compactions (`compact_count` field). If precompact fires
again before the loop re-enters, prior encoding items are preserved in `prior_encoding_items`.

Phase -0.5c processes a budget of `min(5, queue_length)` encoding items — a lightweight mid-session
encoding pass, not full consolidation. Remaining items stay in the encoding queue for session-end.

Hooks configured in `.claude/settings.json` (project-level, not skill-scoped).

---

# Report Timestamp

`<agent>/session/last-report-timestamp` — plain text file containing an ISO timestamp.
Written by `/agent-completion-report` Phase 5 after generating a report. Read by Phase 1
to determine the report window. If missing, report falls back to `handoff.yaml`
session start time or shows lifetime totals.

Report files: `/agent-completion-report` Phase 4 writes the full report markdown to
`<agent>/reports/completion-report-{timestamp}.md` (timestamped archive) and
`<agent>/COMPLETION-REPORT.md` (latest, overwritten each run).

---

# Context Read Deduplication

Hooks prevent redundant file reads AND skill invocations between compaction cycles:

- `PreToolUse[Read]` gates re-reads of tracked files (exit 2 = block)
- `PreToolUse[Skill]` gates duplicate skill invocations (exit 2 = block, combined gate+record)
- `PostToolUse[Read]` auto-records reads
- `PostToolUse[Write,Edit]` invalidates modified tree nodes
- PreCompact clears the tracker

**Note**: `PostToolUse` does not fire for the Skill tool (the Skill tool injects content
into the conversation stream rather than returning a traditional tool result). The
`PreToolUse[Skill]` hook therefore combines gating and recording in a single step: on
first invocation it records the SKILL.md path and allows; on subsequent invocations it
blocks with "Skill /name instructions already in context."

Skills use `load-conventions.sh` in Step 0 to batch-check which conventions need loading.

**Scope**: `core/config/**`, `.claude/skills/**/SKILL.md` (Read AND Skill tool), `world/knowledge/tree/**`, `world/conventions/**`.
Partial reads (offset/limit) bypass tracking.

**Scripts**: `core/scripts/context-reads.py`, `core/scripts/context-reads-skill-gate.sh`, `core/scripts/load-conventions.sh`.

---

# Pending Background Agents

Tracks dispatched background agents (`Agent(run_in_background=true)`) so the stop hook
and aspirations loop can handle the idle-while-agents-work scenario correctly.

- **File**: `<agent>/session/pending-agents.yaml`
- **Written by**: aspirations-execute Phase 4 (before Agent dispatch)
- **Read by**: stop-hook.sh Gate 2.5, aspirations Phase -0.5a
- **Cleaned by**: aspirations post-Phase 9.7 (team shutdown), `pending-agents.sh prune-stale`

**Scripts**: `core/scripts/pending-agents.sh` (thin wrapper), `core/scripts/pending-agents.py`

| Subcommand | Purpose |
|-----------|---------|
| `register --id <id> --team <team> --goal <goal> --purpose <desc> [--timeout <min>]` | Register agent before dispatch |
| `deregister --id <id>` | Remove completed agent |
| `deregister-team --team <name>` | Remove all agents from a team |
| `list [--json]` | Show all registered agents |
| `has-pending` | Exit 0 if non-stale agents exist, exit 1 if not (runs prune-stale internally) |
| `prune-stale` | Remove agents past their timeout_minutes |
| `clear` | Delete file entirely |

**Stop hook Gate 2.5**: calls `has-pending`. If pending agents exist, stop is allowed
(exit 0) and the counter is cleared. Background agent completion notifications re-engage
the parent agent, which collects results in Phase -0.5a.

**Staleness guard**: agents past their `timeout_minutes` (default 10) are auto-pruned by
`has-pending` and `list`, preventing orphaned registrations from permanently disabling the stop hook.

---

# Background External Jobs

Tracks long-running external OS processes (hours+) so the aspirations loop can monitor
them via recurring goals and collect results on completion. Complements `pending-agents.yaml`
(which tracks short-lived Claude Code sub-agents).

- **File**: `<agent>/session/background-jobs.yaml`
- **Written by**: Forged skills with long-running background tasks (e.g., processor launch skills)
- **Read by**: Forged skills that monitor background tasks (e.g., processor monitor skills)
- **Cleaned by**: The monitoring skill on job completion or failure

**Scripts**: `core/scripts/background-jobs.sh` (thin wrapper), `core/scripts/background-jobs.py`

| Subcommand | Purpose |
|-----------|---------|
| `register --id <id> --type <type> --goal <goal-id> --pid <pid> --monitor-goal <id> --completion-check <cmd> [--metadata <json>]` | Register job before launch |
| `deregister --id <id>` | Remove completed/failed job |
| `check --id <id>` | Check job status: PID alive → running; PID dead → run completion_check |
| `list [--json]` | Show all registered jobs |
| `has-pending` | Exit 0 if any jobs exist, exit 1 if not |
| `clear` | Delete file entirely |

**Completion check delegation**: The `completion_check` field stores a command (resolved
relative to project root) that determines whether a dead process completed successfully
(exit 0) or failed (exit 2). This makes the tracker domain-agnostic — process-specific
completion logic lives in the skill's companion script, not in the framework.

**No staleness timeout**: Unlike `pending-agents.yaml`, background jobs have no automatic
timeout pruning. Jobs can legitimately run for hours. Cleanup is the responsibility of
the monitoring skill (via `deregister`) when the job completes, fails, or is abandoned.

**Recurring monitor goal pattern**: The launching skill creates a recurring goal with
`interval_hours: 0.5` that periodically invokes the skill in monitor mode. On each check,
the skill calls `background-jobs.sh check --id <job_id>` and branches on the result.
When the job completes, the monitor goal sets `recurring: false` and marks itself completed.

**Autocompact survival**: The YAML file persists on disk across context compression.
The recurring monitor goal persists in `aspirations.jsonl`. Phase 0 (aspirations-precheck)
resets completed recurring goals to `pending` after `interval_hours` elapses. No checkpoint
integration needed.
