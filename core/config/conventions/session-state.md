# Session File Manifest

The authoritative list of every file that may live in `agents/<agent>/session/` is
`core/config/session-manifest.yaml`. Scripts parse that YAML directly; this
section is the human-readable entry point and must stay in sync with the YAML.

## Phase 2.6 — Two-Tier Session Layout

Each agent has **two** session-related directories:

| Path | Scope | Contents |
|------|-------|----------|
| `agents/<agent>/session/` (singular) | **Agent-wide, cross-session** | `agent-state`, `agent-mode`, `persona-active`, `handoff.yaml`, `working-memory.yaml`, `pending-questions.yaml`, `running-session-id`, `latest-session-id`, `runner-token`, `runner-heartbeat`, `iteration-checkpoint.json`, `compact-*`, etc. Everything that must survive across Claude Code session boundaries OR represents an agent-wide pointer (current runner SID, current mode). |
| `agents/<agent>/sessions/<SID>/` (plural) | **Per-session, self-describing** | `binding.yaml` (agent + mode + started_at + started_by), `session-summary.yaml` (counts written at /stop), scratch files. The dir name IS the SID; counting subdirs = counting sessions. L1 hook sanctions arbitrary writes here as the per-session scratch home (see `.claude/rules/path-resolution.md` "Phase 2.6 sanctioned scratch"). |

**Why both:** singular `session/` files survive into NEW Claude Code sessions
with different SIDs (e.g., compact-checkpoint at autocompact resume). Plural
`sessions/<SID>/` files are tied to one Claude Code session lifetime.

**Stale sweep:** `cleanup-stale-bindings.sh` removes both legacy
`.active-agent-<SID>` files at PROJECT_ROOT (pre-Phase-2.6) and entire
`sessions/<SID>/` dirs when the 3-signal predicate (mtime > 24h + running-sid
mismatch + heartbeat stale) fires. Per-session ephemerals go with the dir.



**Single source of truth:** new session files go into the YAML. Recovery
(`/start --recover`), snapshotting (`session-snapshot.sh`), and desync checks
(`session-desync-check.sh`) pick them up from there automatically — no
per-file updates to skill pseudocode or recovery scripts.

## Manifest fields

Each entry in `core/config/session-manifest.yaml` declares:

| Field | Meaning |
|-------|---------|
| `file` | Basename inside `agents/<agent>/session/` (no path prefix) |
| `purpose` | One-line human description |
| `writer` | Script(s) or skill(s) authorized to write — cross-check with `.claude/rules/user-interaction.md` |
| `recovery_action` | `preserve` (leave untouched) or `clear` (rm on `/start --recover`) |
| `required_when_idle` | `true` if presence while IDLE is normal; `false` if a desync warning |
| `required_when_running` | `true` if presence while RUNNING is normal; `false` if a desync warning |

## Recovery action summary (at time of writing)

| recovery_action | Meaning | Example files |
|-----------------|---------|---------------|
| `preserve` | Survives `/start --recover`. Either durable data (handoff.yaml, working-memory.yaml) or files whose owners idempotently regenerate them. | `agent-state`, `agent-mode`, `handoff.yaml`, `working-memory.yaml`, `execution-diary.jsonl`, `pending-questions.yaml` |
| `clear` | `rm -f`'d by `/start --recover` because presence after a crash means "stale transient." | `loop-active`, `stop-loop`, `stop-requested`, `stop-target-mode`, `iteration-checkpoint.json`, `compact-pending`, `compact-checkpoint.yaml`, `runner-heartbeat`, `running-session-id`, `compact-checkpoint.json` |

## Manifest consumers

| Consumer | What it reads | Action |
|----------|---------------|--------|
| `/start --recover` (Phase 0.7) | `files` with `recovery_action: clear` | `rm -f agents/<agent>/session/<file>` for each |
| `session-snapshot.sh` | All `files` entries | Stat each; emit JSON (exists, mtime, size, recovery_action) |
| `session-desync-check.sh` | `invariants` block + snapshot output | Evaluate each invariant rule; log warnings advisory |

## Desync invariants

The YAML also defines `invariants` — rules the desync checker evaluates after
each snapshot. Violations are logged (append-only) to
`agents/<agent>/session/desync-warnings.jsonl` and echoed to stdout, but **never
block** (`session-desync-check.sh` always exits 0). Auto-remediation is out
of scope by design — the cost of a false-positive delete outweighs the cost
of an unread warning.

Current invariants (see YAML for the canonical list):

| id | Rule | Meaning |
|----|------|---------|
| `heartbeat_without_running` | `runner-heartbeat` fresh < 120s AND state ≠ RUNNING | Crashed runner or a leaked heartbeat file |
| `stop_requested_when_idle` | `stop-requested` exists AND state == IDLE | Partial /stop left signal behind |
| `iteration_checkpoint_when_idle` | `iteration-checkpoint.json` exists AND state == IDLE | Aborted mid-iteration |
| `loop_active_when_idle` | `loop-active` exists AND state == IDLE | Stale loop signal |
| `running_without_heartbeat` | state == RUNNING AND `runner-heartbeat` missing | Crash before first heartbeat tick |

Adding a new invariant: append to `invariants:` in the YAML, add a matching
handler keyed by `id` in `core/scripts/session_desync_check.py::_HANDLERS`.
Missing handlers are logged as `info`-severity "unhandled" rather than
crashing the checker.

---

# Ad-hoc scratch workspace (`agents/<agent>/session/scratch/`)

The canonical home for every ephemeral file an agent creates during
execution: bridge / Studio query outputs, JSON staging for inline edits,
probe dumps, one-shot work files. **Use this path, not repo-root
`.scratch/` and not per-goal `session/work/<goal-id>/` dirs** — those
were ad-hoc agent habits that accumulated cruft because no recovery
path knew to clean them.

## Rules

1. **Path**: write to `agents/<agent>/session/scratch/<anything>`. Per-goal
   namespacing is encouraged when concurrent goals risk collision —
   e.g., `agents/<agent>/session/scratch/<goal-id>/<file>`.
2. **Auto-clear**: the manifest entry for `scratch/` carries
   `type: dir` + `recovery_action: clear`. `session-manifest-clear.sh`
   wipes contents (keeps the directory) on `/start --recover` and on
   `recovery-gate.sh` auto-recovery — exactly the same trigger as the
   transient signal files.
3. **Never archive from scratch**: anything that should outlive the
   session belongs in the proper store — `agents/<agent>/experience/` for
   experience archives, `agents/<agent>/temp/` for working docs that drain
   to the tree, `world/knowledge/tree/` for knowledge, `world/conventions/`
   for reusable schemas, and so on. Scratch is for the IO buffer between
   "the thing produced output" and "I extracted what mattered."
4. **No ceremony**: scratch is created by `/start` and by recovery; the
   agent does NOT need to `mkdir` defensively.

## type: dir manifest entries

The `type` field on a manifest entry is what distinguishes a directory
from a regular file:

```yaml
- file: scratch
  type: dir
  recovery_action: clear
  ...
```

Behavior differences from default `type: file`:

| Aspect | `file` (default) | `dir` |
|---|---|---|
| Existence check | `Path.is_file()` | `Path.is_dir()` |
| Snapshot `size` field | `st.st_size` | `null` (recursive sum would slow every snapshot) |
| Recovery `clear` action | `Path.unlink()` | wipe contents (`rmtree` children, then `mkdir`-stable) |
| Orphan-scan visibility | scanned at session/ top level | dir itself never flagged (orphan scan only looks at top-level files) |

If you add another dir-typed manifest entry, the snapshot + clear
scripts already handle it generically — no per-entry code change needed.

## glob: true manifest entries

When a writer produces files whose names rotate (per-session sentinels,
per-iteration dumps), declare an fnmatch pattern with `glob: true`:

```yaml
- file: aspirations-incremented-session-*.txt
  glob: true
  recovery_action: clear
  ...
```

Behavior:

| Aspect | regular entry | `glob: true` |
|---|---|---|
| `file` field | exact filename | fnmatch pattern (e.g. `*-session-*.txt`) |
| Snapshot `exists` | true if path exists | true if **any** match exists |
| Snapshot extras | `mtime`, `size` | `match_count`, `matches[]` (filenames only), `mtime` of newest |
| Recovery `clear` | `unlink()` of single file | `unlink()` of every match |
| Orphan scan | exact-name skip | files matching any registered glob pattern are skipped |

The orphan scanner uses `fnmatch.fnmatch(name, pattern)` against every
glob entry, so registering one pattern covers every current and future
filename matching it. Use sparingly — exact entries are clearer when
the writer always produces the same name.

---

# Agent State Machine

- State file: `agents/<agent>/session/agent-state` (plain text, no YAML)
- Valid values: `RUNNING`, `IDLE`. Absence = UNINITIALIZED.
- ONLY `/start` and `/stop` may write to this file (via `session-state-set.sh`)
- Claude MUST NOT modify agent-state under any circumstances
- Boot and aspirations check agent-state before executing (defense in depth)
- All reads via `session-state-get.sh`, all writes via `session-state-set.sh`

---

# Agent Mode

- Mode file: `agents/<agent>/session/agent-mode` (plain text, no YAML)
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

1. Observer sessions MUST NOT write to: `agent-state`, `agent-mode`, `persona-active`, `running-session-id`, `latest-session-id` (both SID files are runner-owned per `guard-340`; lockstep pair)
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

# MIND_SID Contract

`MIND_SID` is the authoritative this-session identifier. Every Bash tool call
and every shell script that participates in session-state bookkeeping reads it
to know which Claude Code session it belongs to.

## How it's set

- **PreToolUse[Bash] hook** (`core/scripts/bash-agent-inject.sh`) injects
  `MIND_SID=<session-id>` on every Bash call made from the LLM. The session
  ID is extracted from the hook input stdin JSON (`session_id` field) provided
  by the Claude Code harness — not from any on-disk proxy.
- Scripts invoked by other scripts (not directly by the LLM) inherit
  `MIND_SID` from the parent environment.
- **Never read `agents/<agent>/session/latest-session-id` as a proxy for
  `MIND_SID`** (`guard-341`). That file holds the RUNNER's SID across the
  agent's lifecycle; in observer/reader/assistant contexts the reading
  session's `$MIND_SID` differs from the runner's, so substituting the
  file content silently corrupts SID-comparison logic (the 2026-04-20
  bravo-hang). The env var is the single source of truth for "what is
  THIS session's SID." `latest-session-id` IS still read by other code
  paths for legitimate reasons — see "Two-file SID design" below.

## Who reads it

| Consumer | Behavior |
|----------|----------|
| `heartbeat-tick.sh` | Advances `runner-heartbeat` mtime every iteration + every 60s during B7 waits. Content is irrelevant (pure mtime). |
| `/stop` observer branch | Reads `$MIND_SID` and compares against `running-session-id`. Match → promote to runner; mismatch → observer branch. |
| `recovery-gate.sh` | Reads `heartbeat-stale.sh` (fresh/stale only) for Condition 2. Also reads SessionStart `source` field and skips the gate entirely when source is `compact` or `resume` (continuations, not fresh startups). See `compact-recovery.md` "Recovery-gate source matching". |
| `session-save-id.sh` | Writes `$MIND_SID` into `running-session-id` at RUNNING entry. |

## Observer vs runner contract

- **Runner session**: `$MIND_SID` == `$(cat running-session-id)` AND state == RUNNING.
- **Observer session**: `$MIND_SID` != `$(cat running-session-id)` AND state == RUNNING (observer joined a session already owned by another runner).
- **No-runner session**: `$MIND_SID` is set AND state == IDLE (a fresh `/start` before it transitions the state, or an assistant-mode session).

The runner vs observer distinction is ONLY meaningful during state == RUNNING.

## Two-file SID design (running-session-id + latest-session-id)

The agent has TWO SID files in its session dir, written and read for distinct
purposes. They are NOT redundant. Both are runner-owned (`guard-340`).

| File | Distinguishing role | Deleted on `/stop`? |
|------|---------------------|---------------------|
| `running-session-id` | **Presence-meaningful.** Existence == "an autonomous runner currently owns this agent." Content == that runner's SID. Used by `/stop`'s runner-vs-observer detection (compare `$MIND_SID` to file content) and by `recovery-gate.sh` Path B (existence as the corruption canary). | Yes (graceful-stop D-step) |
| `latest-session-id` | **Content-only, lifecycle-persistent.** Same SID as `running-session-id` while both exist. Survives `/stop` so post-stop tooling has access to the most-recent runner SID. | No |

### Why both exist

1. **Per-session throttle key.** `decision-rules-staleness.sh` uses
   `latest-session-id` as a once-per-session WARN throttle. The throttle
   key must be a SID-shaped value that changes per autonomous session,
   AND must remain readable across `/stop`+`/start` boundaries so the
   throttle file `decision-rules-staleness-last-warned-sid` has a stable
   reference frame. `running-session-id` is gone after `/stop`, so it
   can't fill this role.

2. **Self-consistency canary in the autocompact four-witness check.**
   `session-save-id.sh` four-witness match requires
   `compact-pending == running-session-id == latest-session-id ==
   .active-agent-<old-SID> contents`. The two SID files are SEPARATE
   witnesses precisely so a buggy writer that updates one without the
   other is caught. The 2026-04-20 observer-clobber incident motivated
   this — a path was writing one file solo, desyncing the pair, and
   `/stop` misrouted the runner to the observer branch. Two witnesses
   detect that class of bug; one witness cannot.

### Lockstep invariant

Every legitimate writer updates BOTH files atomically (`.tmp` + `mv`):
- `/start` IDLE→autonomous Step 3 (the canonical first-writer)
- `/start` UNINITIALIZED Phase C8 (same atomic pair-write)
- `session-save-id.sh` autocompact-resume (when `COMPACT_AGENT` is set)

The same-session-reconnect path in `session-save-id.sh` (`SID == _SAVED_RUNNER`)
writes `latest-session-id` only because `running-session-id` already holds
the same value — skipping the redundant write is an optimization, not a
divergence. Files stay in lockstep.

### When to add a new caller

- Reading `running-session-id` content: only if you need to check
  "am I the runner" — compare `$MIND_SID` to the file content.
  Existence-only check: see `recovery-gate.sh` Path B.
- Reading `latest-session-id` content: only if you need a SID-shaped
  per-session key that survives `/stop` (rare — usually a different
  per-session marker is more appropriate). Never as a proxy for
  `$MIND_SID` (`guard-341`).
- Writing either: do not. The canonical writers above are exhaustive.
  If a new write site looks necessary, the design is probably wrong;
  file an Unblock goal first.

## Cross-references

- `rb-386` — the 2026-04-20 `/stop` hang. Root cause: two files used as
  divergent detection inputs had been written by different callers and
  desynced. Fix was NOT to delete `latest-session-id` (the file persists
  for the reasons documented above) — it was to (1) make `$MIND_SID` the
  single source of truth for "this session's SID", (2) restrict ALL writes
  to both files to runner-legitimacy gates, and (3) simplify `/stop` Step 5
  to compare `$MIND_SID` against `running-session-id` only. The file
  stayed; the protocol that misused it was removed.
- `guard-340` — both SID files are runner-owned; lockstep pair-write
- `guard-341` — do-not-read-latest-session-id-as-a-proxy-for-`$MIND_SID`

---

# Runner Heartbeat

Per-iteration liveness signal for the autonomous runner (pure mtime). Read by
observer `/stop`, the SessionStart `recovery-gate.sh`, and desync-checker
invariants.

## File contract

- **Path**: `agents/<agent>/session/runner-heartbeat`
- **Writers** (all call `core/scripts/heartbeat-tick.sh`):
  - aspirations/SKILL.md Phase -0.5 — once per iteration.
  - `.claude/skills/start/SKILL.md` IDLE→RUNNING and Phase C8 — one-shot
    seed BEFORE `session-state-set.sh RUNNING` to uphold the "state=RUNNING
    ⟹ fresh heartbeat exists" invariant (rb-323).
  - `core/scripts/interruptible-sleep.sh` — every 60s during B7 waits so
    mtime can't cross the staleness threshold during a 1800s cap sleep.
- **Content**: irrelevant (the probe reads mtime only).
- **mtime**: seconds-granularity liveness timestamp.
- **Recovery action**: `clear` — `/start --recover` and `recovery-gate.sh`
  both remove the file as part of the session-manifest clear list.
- **Desync invariants** (see manifest YAML):
  - `heartbeat_without_running` — fresh < 120s AND state ≠ RUNNING → leaked
  - `running_without_heartbeat` — state == RUNNING AND file missing → crash before first tick

## Freshness threshold

`core/config/aspirations.yaml → runner_heartbeat.stale_minutes`. Probed via
`core/scripts/heartbeat-stale.sh`. Missing block = misconfig (fails loud);
no shell fallback. No caller branches on the exit code — all callers read
stdout.

## Two-way probe output

`heartbeat-stale.sh` emits exactly one of:

| Output | Meaning |
|--------|---------|
| `fresh` | mtime within threshold. |
| `stale` | mtime older than threshold OR file missing. Runner presumed crashed. |

## Why pure mtime (no writer-identity check)

An earlier design wrote the writer's `$MIND_SID` into the heartbeat content
and added a third output `orphaned` for identity mismatch. It was removed
2026-04-21 after the autocompact-RUNNING→IDLE incident: every post-compact
SessionStart rewrote `running-session-id` to the new SID but left heartbeat
content at the old SID (no writer touched it), producing an `orphaned`
verdict on every compact. Tier-A background jobs masked it via Condition 4
until B7 all-blocked drained them, at which point recovery-gate fired on a
latent false positive and demoted the live runners.

The identity layer only helped in one edge case (an observer `/stop` wanting
to self-promote when `running-session-id` went stale). The single atomic
pair-write in `/start` IDLE Step 3 and Phase C8 (`running-session-id` +
`latest-session-id` together, always) already prevents that staleness — so
the self-promote branch was dead code. Keeping mtime-only is simpler and has
no known failure mode given the 60s tick during waits.

---

# Session State Script Access

Session control files in `agents/<agent>/session/` are accessed exclusively via scripts.
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
| `session-signal-set.sh <name>` | Creates marker file. Valid names: `loop-active`, `stop-loop`, `stop-requested`. `stop-loop` is guarded (rejected while RUNNING); `stop-requested` has no guard (must be settable during RUNNING for graceful stop). Source of truth: `VALID_SIGNALS` in `core/scripts/session.py`. |
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

Working memory (`agents/<agent>/session/working-memory.yaml`) has its own dedicated script family.
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

- **File**: `agents/<agent>/session/compact-checkpoint.yaml`
- **Written by**: `core/scripts/precompact-checkpoint.sh` (PreCompact hook, matcher: auto)
- **Injected by**: `core/scripts/postcompact-restore.sh` (SessionStart hook, matcher: compact — stdout injected into context)
- **Consumed by**: aspirations loop Phase -0.5c (processes encoding queue in fresh context, then deletes checkpoint)

The checkpoint accumulates across multiple compactions (`compact_count` field). If precompact fires
again before the loop re-enters, prior encoding items are preserved in `prior_encoding_items`.

Phase -0.5c processes a budget of `min(5, queue_length)` encoding items — a lightweight mid-session
encoding pass, not full consolidation. Remaining items stay in the encoding queue for session-end.

Hooks configured in `.claude/settings.json` (project-level, not skill-scoped).

## Cross-Session Binding (compact-pending Breadcrumb)

Separate from the encoding checkpoint above, the agent binding carries forward via
a `agents/<agent>/session/compact-pending` breadcrumb written by the stop hook before it
BLOCKs (autocompact resume). `core/scripts/session-save-id.sh` consumes the
breadcrumb on the next SessionStart event to update `running-session-id` from the
pre-compact SID to the new post-compact SID.

**Source-gated**: Breadcrumb consumption fires ONLY when SessionStart stdin
includes `"source": "compact"`. Other source values (`startup`, `resume`) skip
the breadcrumb logic entirely. This prevents new windows opening with fresh SIDs
from hijacking another agent's pending breadcrumb and corrupting that agent's
`running-session-id`.

**Four-witness self-binding match**: When a compact event fires, the breadcrumb
matcher requires four signals to agree before consuming:
1. `agents/<agent>/session/compact-pending` contents (the pre-compact SID)
2. `agents/<agent>/session/running-session-id` contents
3. `agents/<agent>/session/latest-session-id` contents
4. `<project_root>/.active-agent-<old-SID>` contents (the agent name)

Any disagreement → restore the breadcrumb (best-effort) and skip. Defends against
torn state from concurrent autocompact and against orphaned breadcrumbs from
crashed sessions.

**Per-agent mkdir spinlock**: The `running-session-id` write is wrapped in a
`agents/<agent>/session/.binding-lock` mkdir-spinlock (atomic on POSIX and Windows
mingw bash). Stale locks > 30s are reaped before the spin. Prevents two
SessionStart hooks for the same agent from interleaving writes.

**PreCompact serialization gate**: To eliminate the cross-agent race where two
agents autocompact within milliseconds of each other, the PreCompact hook
acquires a project-root global lock at `<project_root>/.autocompact-serialize-lock/`
(mkdir-atomic on POSIX and Windows mingw). At most one agent holds the lock
between PreCompact and post-compact SessionStart — the other agent's PreCompact
spins for up to 60 s waiting for the lock to clear. The lock is released by
`session-save-id.sh` on ANY `source=compact` SessionStart event (after the
four-witness walk completes, regardless of whether a match was found), or
reaped by the next PreCompact's stale-cleanup if older than 5 minutes (crash
recovery).

The release fires unconditionally on `source=compact` because reader/assistant
sessions DO autocompact (any long Claude Code window can) but never write a
`compact-pending` breadcrumb — only the autonomous loop's stop-hook BLOCK
writes one. Releasing only on witness-match success would strand the lock
across every reader/assistant compact and starve subsequent autonomous
compacts for up to a minute.

The lock directory contains forensic files: `holder` (agent name), `sid` (the
session ID that claimed it), `timestamp` (epoch seconds at claim time). The
PreCompact hook timeout is raised to 90 s in `.claude/settings.json` (60 s
wait window + 30 s safety margin). Fail-open at every error path — a broken
gate degrades to the original race behavior, never worse.

Implementation: `core/scripts/precompact-serialize.sh` (acquire), the
"PreCompact serialization gate" comment block in `session-save-id.sh`
(release).

**Test**: `core/scripts/test-session-binding.sh` — runs seven scenarios in
an isolated `/tmp` sandbox (no project-state mutation). Scenarios 1-3 enforce
binding correctness; scenario 4 logs the gate-bypassed concurrent-race outcome
informationally; scenario 5 exercises the serialization gate; scenario 6
verifies stale-lock recovery; scenario 7 verifies the source=compact release
fires even without a breadcrumb (assistant-mode safety) and that
source=startup does NOT release the lock.

**History**: The cross-agent SID swap that hit alpha and bravo on 2026-04-19T00:54
was caused by either (a) a non-compact SessionStart event hijacking a fresh
breadcrumb (now blocked by source-gate) or (b) the concurrent autocompact race
(documented limitation). After the swap, every stop hook for either agent
failed Gate 0 (`sid-mismatch`) and ALLOWed exit instead of BLOCKing —
both autonomous loops died silently for hours.

---

# Report Timestamp

`agents/<agent>/session/last-report-timestamp` — plain text file containing an ISO timestamp.
Written by `/agent-completion-report` Phase 5 after generating a report. Read by Phase 1
to determine the report window. If missing, report falls back to `handoff.yaml`
session start time or shows lifetime totals.

Report file: `/agent-completion-report` Phase 4 writes the full report markdown to
`agents/<agent>/COMPLETION-REPORT.md` (the single latest-pointer report, overwritten
each run). Its git history is the permanent archive — there is no timestamped
`reports/` archive (that directory was abolished by the file-model normalization;
see `core/config/conventions/temp-store.md`). The delta baseline is saved to
`agents/<agent>/session/last-outcome-snapshot.yaml`.

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

- **File**: `agents/<agent>/session/pending-agents.yaml`
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

- **File**: `agents/<agent>/session/background-jobs.yaml`
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

# Background Job Hygiene

The "No staleness timeout" rule above is a feature, not a bug — 7h Processor
runs would be killed mid-work by any naive timeout. But it creates a second
responsibility: leaks accumulate silently when a monitoring skill crashes,
a watcher subshell is `disown`-ed, or a session closes mid-job. The
`scan-stale-jobs` skill owns that cleanup.

**Scanner**: `world/scripts/stale-jobs-scan.sh` (subcommands `report`,
`reconcile`, `scan --auto-kill`). Identifies Tier A (registered jobs past
their type-specific lifetime threshold in `core/config/aspirations.yaml →
stale_scanner.thresholds`) and Tier B (unregistered OS-level orphans
matching known command-line signatures: Processor, llama-server,
roblox-bridge, ssh-efs).

**Auto-kill is gated**. The `scan --auto-kill` invocation reaps candidates
under (a) report-within-24h precondition, (b) 3-kill-per-run cap,
(c) 10-minute newborn cooldown, (d) graceful-taskkill-first with 30s wait
before `/F`. The `report` and `reconcile` subcommands never kill anything.

**Safety by construction**. The do-not-kill set at scan start contains:
scanner's own `Win32_Process.ParentProcessId` ancestry walk (validated via
`CreationDate` ordering to catch recycled PIDs); every PID registered
under every agent's `session/background-jobs.yaml` (cross-agent
fratricide is structurally impossible); every process matching
Claude-Code signatures (`@anthropic-ai/claude-code`, `node.*cli.mjs`,
`\claude.exe`).

**Recurring goal**: `scan-stale-jobs` is wired to `asp-115 Recurring
Infrastructure Monitoring` under a 4h-interval recurring goal. Reports
live at `core/logs/stale-scanner-report.jsonl`; kills (only when invoked
with `--auto-kill`) at `core/logs/stale-scanner-kills.jsonl` — both
audit-trail JSONL, one entry per scan invocation.

**Spawn-site contract**: All long-lived processes should register via
`background-jobs.sh register` at launch. Unregistered long-lived
processes rely on Tier B signature matching, which is best-effort — known
signatures catch known leaks, but a future unregistered spawn site may
run past threshold without being classified. Prefer registration.

**Rule**: register on spawn, rely on scanner for cleanup.

# Output-Sanity Gate (the 0-byte defense)

`check_job` trusts `pid_alive` to decide running-vs-done, and it trusts the
registered `completion_check` exit code (0 / 2) to decide completed-vs-failed.
Neither signal catches the case where the process exits cleanly, the
completion check exits 0, AND the declared output file is missing, zero-byte,
or unparseable. That class of failure is historically real (rb-061, rb-085,
guard-156, rb-247). The output-sanity gate closes it.

## Contract

At register time, a job may optionally declare `--output-artifacts` — a JSON
array of artifact specs. Each spec:

| Field | Meaning |
|-------|---------|
| `path` | Absolute filesystem path to the artifact. Required. |
| `min_bytes` | Minimum acceptable size. Default `1`. `0` allowed for "only care about existence". |
| `format` | Optional: `json` (attempt full parse) or `jsonl` (parse first non-empty line). Omit for size-only. |

## Semantics

When `check_job` has otherwise concluded `status == "completed"` (PID dead
AND `completion_check` exit 0), it runs every declared artifact through:

1. **Exists?** — Missing path → `output_check_failures[]` entry with reason `missing`.
2. **Size ≥ `min_bytes`?** — Too small → `too_small (Nb < Mb)`.
3. **Format check** (if specified):
   - `json` → full `json.loads` of file contents.
   - `jsonl` → read first line; fail if blank, else `json.loads` that one line.
4. Any other `format` value: size check is considered sufficient.

If `output_check_failures` is non-empty, `check_job` overrides
`status` from `completed` to `failed` and attaches the failures list.
MONITOR skills should treat that exactly like any other `failed` —
file an Investigate goal with the failures payload, do NOT deregister
silently, do NOT proceed to COLLECT.

## Backward compatibility

Jobs registered without `--output-artifacts` behave exactly as before —
the gate is a no-op when the artifact list is empty or absent. Existing
callers do not need to migrate; the gate only adds protection where an
opt-in declaration exists.

## Spawn-site responsibility

Any skill whose MONITOR step assumes a file on disk (COLLECT reading
`run_summary.json`, downstream consumer reading an aggregate `.jsonl`)
MUST declare those files via `--output-artifacts` at `register` time.
Rule (also encoded as a guardrail): **"If your downstream consumers
read files from disk, declare those files as `--output-artifacts`."**
Enforced at spawn-site review, not at runtime.
