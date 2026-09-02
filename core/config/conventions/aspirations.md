# Aspiration JSONL Format

Aspirations use JSONL (one JSON object per line) with script-based access:

## File Layout

### World Queue (collective task list — `world/`)
- `world/aspirations.jsonl` — Live active/pending world aspirations
- `world/aspirations-archive.jsonl` — Completed/retired (append-only)
- `world/aspirations-meta.json` — Metadata (session_count, readiness_gates)

### Agent Queue (per-agent local tasks — `<agent>/`)
- `agents/<agent>/aspirations.jsonl` — Agent's local work queue (maintenance, decomposed sub-goals)
- `agents/<agent>/aspirations-archive.jsonl` — Agent's completed local tasks
- `agents/<agent>/aspirations-meta.json` — Agent aspiration metadata

### Shared
- `meta/evolution-log.jsonl` — Evolution events (append-only)
- `core/config/world-aspirations-initial.jsonl` — World bootstrap aspirations (copied by init-world.sh)
- `core/config/agent-aspirations-initial.jsonl` — Agent maintenance goals (copied by init-agent.sh)
- `core/config/agent-aspirations-onboard.jsonl` — Onboarding aspiration for subsequent agents

## Dual-Scope Bootstrap IDs

The world queue and agent queue each have a bootstrap aspiration seeded at init
time, and they **share the ID `asp-001`** by convention:

| Source | ID | Title | Seeded by |
|---|---|---|---|
| `world/aspirations.jsonl` | `asp-001` | Explore and Learn | `init-world.sh` (copies `core/config/world-aspirations-initial.jsonl`) |
| `world/aspirations.jsonl` | `asp-002` | Operating Rhythm | `init-world.sh` (same seed file — never-terminal recurring container carrying the `/sprint-planning` cadence goal `g-002-01`; world-scope, so ONE agent claims each firing rather than every agent running its own sprint) |
| `agents/<agent>/aspirations.jsonl` | `asp-001` | Maintain Agent Health | `init-agent.sh` (copies `core/config/agent-aspirations-initial.jsonl`) |
| `agents/<agent>/aspirations.jsonl` | `asp-003` | Orient and Specialize | `init-agent.sh` (appends `core/config/agent-aspirations-onboard.jsonl` for subsequent agents) |

These are **canonically different aspirations** in different files, sharing the
bootstrap ID by convention. Goal IDs similarly share namespace across scopes:
`g-001-01` in `world/aspirations.jsonl` is "Identify learning domain," while
`g-001-01` in `agents/<agent>/aspirations.jsonl` is "Reflect and journal." The parent
aspiration's source (`--source world` vs `--source agent`) disambiguates.

### Rules

1. **Always use `--source world` or `--source agent`** when reading or writing
   aspirations programmatically. The scripts enforce scope — omitting `--source`
   defaults to world, which is correct only when you intend the world queue.
2. **Qualify scope in narrative contexts** (board posts, journal entries, reports,
   goal descriptions): write "world asp-001" or "`<agent>` asp-001," not bare
   "asp-001." A bare ID in prose is ambiguous when both scopes exist.
3. **Goal-selector already handles this**: `goal-selector.py` reads from BOTH
   queues and tags each candidate with `source: "world"` or `source: "agent"`.
   Downstream skills propagate `source` per the Source Routing Protocol below.

## ID Allocation (g-328-29 — server-side, in-lock)

**The `id` field on `/v1/aspirations/add` is OPTIONAL.** When absent (or
`"auto"`/`""`), the daemon mints the next `asp-NNN` INSIDE the write lock:
max+1 across the target queue's live file ∪ archive, both read under the
same lock that appends the record. Embedded goal ids are minted in the same
lock as `g-NNN-01..` in array order (goals carrying explicit ids under auto
allocation are refused with `auto_id_goal_conflict` — the caller cannot know
the asp number yet). The response returns `aspiration_id` +
`id_allocated: true`; callers read the id back instead of pre-computing it.
Goal ids on `/v1/aspirations/add-goal` were already minted in-lock
(`_allocate_goal_id`) — this extends the same guarantee to aspiration ids.

**Why option (a) — atomic in-lock allocation.** The alternative shapes were
(b) optimistic mint + retry-on-collision (client keeps minting, server
refuses, client increments — leaves the race window and adds a retry loop
every caller must implement) and (c) a dedicated counter file (a second
write surface with its own lock ordering and drift-vs-truth reconciliation).
(a) closes the race at the only place that already serializes queue writes,
adds zero new files, and keeps explicit ids working for transplant and
migration callers.

**The incident this closes** (2026-07, BRD "owncloud-fence-freeze"): two
agents filed aspirations concurrently; both ran the SKILL-layer
`max(NNN across active ∪ archive) + 1` read OUTSIDE any lock, both computed
`asp-334`, and the second write landed as a duplicate id (double-mint).
In-lock minting makes concurrent auto adds serialize and receive distinct
sequential ids.

**Scope and residual risk.**
- Uniqueness is per-queue (world and agent queues share the `asp-001`
  bootstrap id by convention — see Dual-Scope Bootstrap IDs above); the
  mint scans only the target queue, matching the id-space semantics.
- Cross-box residual: two machines filing against different local replicas
  before own-cloud sync converges can still double-mint — the write lock is
  per-box. Today the merge/CAS layer surfaces such collisions; if the
  conflict-rate metric (`GET /v1/admin/write-queue`, g-328-28) shows this
  actually occurring, the documented escalation is a remote-lock-table
  atomic-counter allocator (conditional add — fleet-global, monotonic,
  no scan).
- Format ceiling: `_ASP_ID_RE` (and the CLI's `ASP_ID_RE`) accept exactly
  3-digit `asp-NNN`. The mint's `:03d` format grows naturally past 999
  (`asp-1000`), but downstream update paths would reject it — the same
  ceiling client-side minting had. Expand both regexes to `\d{3,4}`
  (mirroring the 2026-05-19 g-NNN-NNNN goal-id expansion) before the fleet
  approaches asp-999.

## Script-Based Access (Exclusive Data Layer)

The LLM NEVER reads or edits aspiration JSONL files directly. All operations go through scripts.
Two script families — world (default) and agent — operate on separate queues via the same Python engine.

### World Queue Scripts (default — operate on `world/aspirations.jsonl`)

| Script | Purpose | Stdin |
|--------|---------|-------|
| `load-aspirations-compact.sh` | Cached compact active aspirations (dedup-aware) | — |
| `aspirations-query.sh --goal-status <status>` | Query goals by status across both queues (lightweight) — **LIVE only, see note below** | — |
| `aspirations-query.sh --goal-field <field> <value>` | Query goals by field value across both queues — **LIVE only** | — |
| `aspirations-query.sh --title-contains <substr>` | Query goals by title substring across both queues — **LIVE only** | — |
| `aspirations-read.sh --active` | Return active world aspirations as full JSON | — |
| `aspirations-read.sh --active-compact` | Compact active aspirations (no descriptions/verification) | — |
| `aspirations-read.sh --id <id>` | Return one world aspiration by ID | — |
| `aspirations-read.sh --summary` | Compact one-liner per world aspiration | — |
| `aspirations-read.sh --archive` | Return archived world aspirations | — |
| `aspirations-read.sh --meta` | Return world aspirations metadata | — |
| `aspirations-add.sh` | Validate + append new world aspiration | JSON |
| `aspirations-update.sh <asp-id>` | Validate + replace world aspiration | JSON |
| `aspirations-update-goal.sh <goal-id> <field> <value>` | Update single goal field in world queue. **Clearing a field**: pass the literal `null` as `<value>` (parsed to JSON null; `true`/`false`/`[]` likewise parse to JSON forms). An empty-string value is REJECTED by the wrapper's required-positional check — `""` is not the clear form. Clearing `defer_reason` with `null` passes the probe-before-defer gate (fires only on non-null defer writes); on a blocked goal the daemon flips status back to `pending`. Same semantics in `agent-aspirations-update-goal.sh`. | — |
| `aspirations-add-goal.sh <asp-id>` | Validate + append goal to world aspiration (auto-assigns ID). Gated add pipeline: origin-signal, goal-duplication, **operator-offload** (recurring goals must carry `offload_decision` — see `goal-schemas.md` Recurring Goal Fields; bypass `--override-offload`), stale-read. `--override-all "<reason>"` bulk-bypasses (audited to `world/override-bypass-ledger.jsonl`). | JSON |
| `aspirations-complete.sh <asp-id>` | Mark world aspiration completed + archive | — |
| `aspirations-complete-intent.sh <asp-id>` | Close world aspiration via intent-satisfaction pathway (evidence-gated) | JSON |
| `aspirations-retire.sh <asp-id>` | Mark world aspiration retired + archive | — |
| `aspirations-archive.sh` | Sweep completed/retired world aspirations to archive | — |

> **"across both queues" means WORLD + AGENT, never LIVE + ARCHIVE.** Every
> `aspirations-query.sh` form reads only the two LIVE files. When an aspiration
> completes it is archived, and all of its goals leave that query — a lookup by
> goal-id then returns NOT-FOUND, which is byte-identical to the answer for a
> goal that never existed. Any lookup deciding "is goal X done?" must ALSO read
> `aspirations-read.sh --archive` (2026-07-26: 353 archived aspirations /
> 2278 completed goals — not a rare edge; re-measured 2026-08-21: 370 archived).
> No `--json`: this wrapper never accepted one and `--archive` already returns a
> JSON list. The flag was silently DROPPED until g-115-5438 made the wrapper
> refuse unknown flags, at which point this documented command became rc=2 —
> which is how a doc-only caller of a swallowed flag surfaces. Confirmed live
> 2026-08-22 (cc-08): `aspirations-query.sh --title-contains <x> --json` now
> exits 2 with an empty STDOUT and the refusal on STDERR, so a caller that pipes
> it into a JSON parser reads a clean "0 hits" and can conclude an owned test red
> is unowned. Drop the flag; the output is already JSON. Prefer keeping the two results
> DISTINCT rather than merging them: a goal completed inside an archived
> aspiration is more finished than a merely-completed live one, and an id in
> NEITHER store is an anomaly worth reporting rather than skipping.
> Measured cost of assuming otherwise (g-115-3332): a fleet PR probe built on
> the live query alone silently dropped 6 of the 11 findings it was written to
> surface — all owned by one archived aspiration — while reporting its own
> status as `ok`. See `guard-1555`, `rb-5272`.
| `aspirations-meta-update.sh <field> <value>` | Update world aspirations metadata | — |
| `evolution-log-append.sh` | Append evolution event | JSON |

### Claim-Lifecycle Operations (both queues, `--source`-selected)

| Script | Purpose |
|--------|---------|
| `aspirations-claim.sh <goal-id> [agent-name]` | Atomically claim a goal for an agent |
| `aspirations-release.sh <goal-id> [--source world\|agent] [--reason <text>] [--reason-kind <token>]` | Release a claimed goal; `--reason` records WHY, `--reason-kind` types it (see *Typed release negatives*) |
| `aspirations-complete-by.sh [--source world\|agent] <goal-id> [agent-name]` | Mark goal completed with agent attribution |

Agent name defaults to `$MIND_AGENT` for claim and complete-by.

**This section was titled "World-Only Operations (no agent equivalent)" and said
"claim and release are world-only" until g-306-258.** That was true when written and
is now false: `g-306-238` taught the claim endpoint `&source=agent`, and `g-306-249`
gave the release wrapper `--source`. The wrapper's default is still `world`, so an
agent-queue release that omits the flag silently resolves the wrong queue and
strands the claim — which is precisely why the flag is written out in the table
above rather than left implicit.

#### Typed release negatives (`--reason-kind`, g-115-8163)

A release records a NEGATIVE — "this agent, on this box, did not complete this
goal" — in the goal's `release_negatives` array. `--reason` has always carried
the narrative; `--reason-kind` carries the one token a consumer can actually
query on:

| token | means |
|---|---|
| `locus` | the remaining step needs a machine this box is not |
| `capability` | a credential, tool or permission this agent lacks |
| `role` | the actor who must act is someone else (often a person) |
| `not-due` | timing — a window, a wait, a cadence not yet elapsed |
| `progress` | real progress landed; the goal is still valid and unfinished |
| `other` | none of the above fits |

**Why a token and not a better classifier.** Classifying the prose was tried and
MEASURED: over the 52 live reason strings, the over-matching locus regex returned
8 matches with 3 true (62.5% false-positive) and the under-matching one returned
3 while missing every row gated on a GUI-only tool — those name no host at
all, so a hostname pattern cannot see them. One row reading *"this box can still run
this goal ... NOT FOR LOCUS"* MATCHES the locus regex. No threshold fixes that;
only the releasing agent knows the kind, so only the releasing agent can assert it.

Three properties worth not re-deriving:

1. **`locus` and `capability` are deliberately NOT one token.** A credential
   barrier does not clear by moving boxes (the fleet shares its IAM principals),
   so a consumer that conflates them re-routes work to boxes that also cannot run
   it. Merging them is the natural "simplification" a later reader will reach for.
2. **Absent means UNMEASURED, never "no barrier."** Every row predating the flag
   carries no `kind`, so a consumer must fail OPEN on absence. Reading absent as
   "not locus" would silently mark the entire backlog as locus-clear.
3. **The wrapper nudges but never refuses.** `--reason` without `--reason-kind`
   prints an advisory to stderr and proceeds. Refusing would strand a claim, which
   is far worse than an untyped row — a release must never fail closed.

The vocabulary is duplicated in exactly two places, `_REASON_KINDS` in
`core/scripts/aspirations-release.sh` and `RELEASE_REASON_KINDS` in
`mind_api/src/endpoints/aspirations_write.py`. They are pinned equal by an
AST-based parity test (`core/scripts/tests/test_release_reason_kind.py`), because
a shell/daemon constant pair otherwise drifts silently — the wrapper would refuse
a token the daemon accepts, or forward one it drops.

#### Claim Protocol (Goal Lifecycle)

World goals MUST be claimed before execution to prevent duplicate work across agents.
Agent-queue goals are single-agent, so a claim is not required for mutual exclusion —
but they DO carry claims when one is taken (g-306-238), and a claim that is taken must
be released with `--source agent` or it outlives the session.

| Step | Script | When |
|------|--------|------|
| **Claim** | `aspirations-claim.sh <goal-id>` | Before Phase 4 execution (world goals) |
| **Release** | `aspirations-release.sh <goal-id> --source {source} --reason "<why>" --reason-kind <token>` | On execution failure, infrastructure failure, goal revert, or session end |
| **Complete-by** | `aspirations-complete-by.sh <goal-id>` | On verified completion (Phase 5.3) |

**Rules:**
1. `goal-selector.py` skips goals claimed by another agent — claims are respected at selection time.
2. Claim is atomic **with respect to a DIFFERENT agent** — if another agent
   claimed first, the script exits non-zero. On conflict, re-enter the selection
   loop. **Claim exclusion is AGENT-scoped, NOT session-scoped** (g-115-3176):
   the endpoint's conflict test is `existing != agent_name`, so a claim from a
   different *session of the same agent* falls through as an idempotent no-op
   and BOTH sessions believe they hold an exclusive claim, with no warning to
   either. Do NOT read "claims prevent duplicate execution" as "only one session
   can be working a goal" — that assumption is false today. Observed live
   2026-07-25: two sessions of the same agent held one world goal 16 minutes
   apart; the second was still doing reconnaissance when the first completed the
   goal, one write away from creating duplicate credentials in an external
   service. Wasted work is the mild failure mode; duplicate production side
   effects are the real one. **If you run a second session of an agent that is already
   running autonomously, treat claims as advisory and verify by hand before any
   irreversible action.**
3. Claim-clearing invariant: any transition to a terminal status (`completed`,
   `skipped`, `expired`, `decomposed`, `superseded`), AND each successful cycle
   of a recurring goal via `complete-by`, clears `claimed_by` and `claimed_at`.
   Only `pending`, `in-progress`, `blocked` goals may carry a live claim.
   Enforced in `cmd_complete_by` and `cmd_update_goal`.
4. Session boundary: release all held claims at session end (consolidation/handoff).
   Release query filters `--goal-status pending,in-progress,blocked` as defense
   in depth — terminal goals should already be claim-free per Rule 3.
5. Self-heal: `aspirations-clear-stale-claims.sh [--dry-run]` sweeps any residue
   left by past writers. Idempotent; zero-effect when no residue exists.
6. **Claiming session identity** (`claimed_by_sid`, g-115-3176): a claim records
   the SID of the claiming session alongside `claimed_by`.
   `aspirations-claim.sh` sends `&sid=$MIND_SID` (hook-injected into every Bash
   call); the daemon stamps `goal["claimed_by_sid"]` — but ONLY when supplied, so
   a take-back or a legacy caller never erases the holding session's identity.
   This makes a same-agent cross-session collision **diagnosable** (compare
   `claimed_by_sid` against your own `$MIND_SID`).
7. **Session-scoped claim exclusion** (g-115-3176, landed): `claim` returns
   **409 `same_agent_other_session`** when the holder is a DIFFERENT session of
   the same agent that is positively confirmed to be this agent's LIVE
   autonomous runner (`running-session-id` match + fresh `runner-heartbeat`
   mtime vs `runner_heartbeat.stale_minutes`). Unchanged: different-agent
   take-back, same-session re-claim (idempotent no-op), legacy sid-less claims,
   and takeover of a **dormant** holder — which is logged to history/changelog
   as `cross-session take-over from dormant <sid>`. The runner always wins:
   an observer session cannot take a goal from the live loop, but the loop can
   take one from a dormant observer.

   The probe **fails open on every ambiguous path**, and that asymmetry is
   load-bearing (same reasoning as
   `.claude/rules/check-team-state-before-silent.md`): a FRESH heartbeat is
   positive evidence of life, but a STALE one is ambiguous — an idle session
   and a broken heartbeat writer are indistinguishable, and a live agent has
   been observed reading 59h stale. So the REFUSAL is gated on freshness and
   the ALLOW is never gated on staleness. A wrong allow permits only what was
   already possible; a wrong refusal would wedge the goal for every session of
   the agent.
8. **`release` / `complete-by` WARN, never refuse** (g-115-3176 outcome 5):
   invoked by a session that does not hold the claim, both apply the operation
   and return a `warnings` entry naming the holding SID (also stderr). They must
   NOT refuse: `stranded-claim-sweep.py --apply` releases claims left by DEAD
   sessions and therefore always runs from a non-holding session, so a refusal
   would break that sweep fleet-wide and wedge exactly the stranded goals it
   repairs. The claim side can refuse because the caller can pick another goal;
   the release side cannot, because there is no other path to un-wedge a goal.
   Both wrappers send `&sid=$MIND_SID` — without it the guard is structurally
   dead, which is the original bug's shape. Both also clear `claimed_by_sid`
   along with the claim: a stamp that outlives its claim would mislabel the next
   sid-less claimer with the previous holder's session.

#### Claim Expiry (Straggler Mitigation)

Claims have a configurable timeout (`multi_agent.claim_timeout_hours` in `aspirations.yaml`,
default 4 hours). If a claim is older than this threshold, `goal-selector.py` treats it as
expired — the goal becomes eligible for other agents to claim.

This prevents indefinite blocking when a claiming agent's session crashes or ends without
releasing. Based on ["Language Model Teams as Distributed Systems"](https://arxiv.org/abs/2603.12229)
Finding 5: decentralized teams mitigate stragglers via dynamic work reallocation.

### Cross-Aspiration Dependency Enforcement

The `blocked_by` field on goals resolves **globally** across all active aspirations (both
world and agent queues). If `g-170-03` has `blocked_by: ["g-168-06"]` where `g-168-06` is
in a different aspiration, the block is enforced — `g-170-03` will not appear as a candidate
until `g-168-06` is completed or decomposed.

This prevents temporal consistency violations (Finding 3 of the distributed systems paper)
where an agent starts work before its cross-aspiration dependencies are met.

### Agent Queue Scripts (operate on `agents/<agent>/aspirations.jsonl`)

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
| `agent-aspirations-meta-update.sh <field> <value>` | Update agent aspirations metadata field | — |
| `agent-aspirations-archive.sh` | Sweep completed/retired agent aspirations to archive | — |

Scripts validate JSON schema before writing. On validation failure: exit non-zero with error.

### Under the Hood

Both script families delegate to `aspirations.py` with a `--source {world|agent}` flag.
Agent wrappers pass `--source agent`; world wrappers use the default (`world`).
Goal-selector reads from BOTH queues and tags candidates with `source: "world"` or `source: "agent"`.

### Source Routing Protocol

When `goal-selector.sh` selects a goal, its output includes `"source": "world"` or
`"source": "agent"`. This field tells downstream skills which queue the goal belongs to.

**Rules:**
1. **Propagate source to all script calls**: Append `--source {source}` to every
   `aspirations-*.sh` call when operating on the selected goal's aspiration.
   When source is `"world"` (default), `--source` may be omitted.
2. **Same-queue for child operations**: Goals spawned during execution (blocker-unblock,
   investigation, idea) go to the same queue as the parent aspiration.
3. **Compact data includes source**: `load-aspirations-compact.sh` returns data from
   both queues. Each entry has a `"source"` field. Use the aspiration variable's
   `.source` field for routing (e.g., `{asp.source}`, `{target_asp.source}` — match
   whatever variable name is in scope, not a hardcoded `asp`).
4. **Cross-queue exception**: Creating a goal in a different queue than the parent is
   valid but must use the explicit target script (no `--source` passthrough).

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
  - `recompute_progress` also emits `progress.fan_out_ratio` = `total_goals / initial_goal_count` (2 dp), recomputed on every goal add/update. `initial_goal_count` is the non-recurring goal count stamped once at aspiration creation (daemon `add` endpoint in `mind_api/src/endpoints/aspirations_write.py`; idempotent — never overwritten). `fan_out_ratio` is `null` when `initial_goal_count` is absent (aspiration predates the metric — added 2026-05-15; no inferred backfill of legacy aspirations) or `0` (growth ratio from an empty seed is undefined). Interpretation: ≈1.0 = specced upfront; >1.5 = discovery-heavy / fanned-out (Charlie/Zeta-shaped work). **Dual-mirror invariant:** the `progress` dict shape in `recompute_progress` (`core/scripts/aspirations.py`) and `_recompute_progress` (`mind_api/src/endpoints/aspirations_write.py`) MUST stay identical — changing one without the other desyncs the CLI and daemon write paths.
  - These guards prevent LLM drift from killing recurring goals by archiving their parent aspiration.
- **Premature-archival protection (data layer enforced):**
  - `aspirations-complete.sh` **refuses** aspirations where any non-recurring goal is not in a terminal status (`completed`, `skipped`, `expired`, `decomposed`, `superseded`). Exit 1 with BLOCKED message listing unfinished goals. Use `--force` to override, or `aspirations-complete-intent.sh` for the intent-satisfaction pathway.
  - `aspirations-retire.sh` **warns** (stderr) when retiring aspirations with unfinished goals, but does not block — retirement is intentional abandonment.
  - `aspirations-archive.sh` (sweep) auto-recovers completed aspirations with unfinished non-recurring goals to `active` status (same pattern as recurring-goal recovery).
  - These guards prevent post-autocompact narrative fabrication from archiving aspirations before their goals are actually done.
- **Resurrection reconcile (data layer enforced, 2026-08-16 — rb-8064 / guard-4066):**
  - The live store's own-cloud merge (`coordination_merge.merge_aspirations`) is a UNION by aspiration id, so a record removed from the live file by complete/retire/sweep comes back PRISTINE from any box still holding it (goals `pending`, no `outcome_note`, no `last_modified`). Measured 9 of 29 live aspirations also present in the archive; 8 were resurrected retirements (7 cross-world `asp-xw-*` stubs).
  - `aspirations-archive.sh` (sweep) runs the reconcile FIRST: a live goal the archive already holds terminal, while the live copy is non-terminal, unclaimed and not modified after the archive's `retired_at`/`completed_at`, gets the archive's disposition back (status + outcome fields, stamped `resurrection_reconciled_at`); when nothing post-dates the archive the aspiration is re-archived by the same sweep. A live goal the archive never saw, a claim, or a newer `last_modified` is post-archive work and keeps the aspiration live untouched. Predicate SSOT: `core/scripts/_aspirations_resurrection.py`; read-only detector: `core/scripts/aspirations-resurrection-scan.py` (verify-learning `no-resurrected-aspiration-live`).
  - "Append-only" above means records are never DROPPED from the archive; a row IS replaced by id when its aspiration is archived again, and that replace UNIONS goals (`_archive_replace_row`) — the archive is the last home of a terminal goal record (eviction re-homes nothing), and a resurrected snapshot can carry fewer goals than the row it supersedes. `complete`, `complete-intent` and `retire` upsert (never append a second row for an id).

## Intent-Satisfied Closure

The framework measures BOTH goal completion (*what did we do?*) AND intent satisfaction
(*what did we achieve?*). When an aspiration's `motivation` has been met by already-completed
goals but trailing goals are blocked on external factors or turned out unnecessary, the
intent-satisfaction pathway allows archival without per-goal completion of the remainder.

**Pathway:** `aspirations-complete-intent.sh <asp-id>` reads an `intent_satisfaction` JSON
block on stdin and atomically (a) validates evidence, (b) transitions trailing goals listed
in `superseded_goal_ids` to `status=superseded`, (c) persists the block on the aspiration,
and (d) archives the aspiration via the normal completion gate.

### `intent_satisfaction` schema (aspiration-level field)

```yaml
intent_satisfaction:
  claimed_at: ISO-8601            # auto-stamped by script (do not supply)
  evidence_goal_ids: [goal_id]    # completed goals whose outcomes map to motivation
  rationale: string               # >=40 chars, must quote motivation text
  superseded_goal_ids: [goal_id]  # non-recurring, non-terminal goals being mooted
```

### Validation (structural, script-enforced — not LLM-trusted)

1. **Evidence cardinality**: `len(evidence_goal_ids) >= max(scope_min, ceil(0.5 * non_recurring_goal_count))`. Scope floors in `core/config/aspirations.yaml` under `intent_satisfaction.min_evidence_by_scope` (sprint=2, project=3, initiative=5).
2. **Evidence quality**: every evidence goal must exist in this aspiration, be non-recurring, have `status=completed`, and have non-empty `verification.outcomes`.
3. **Superseded goals**: must exist in this aspiration, be non-recurring, and be currently non-terminal. No goal may appear in both `evidence_goal_ids` and `superseded_goal_ids`.
4. **Post-supersession closure**: after applying supersession, every non-recurring goal must be in terminal status. If any remain, the command exits non-zero.
5. **Rationale length**: >=40 chars.
6. **Rationale-motivation token overlap**: rationale must share at least one 4+ char token with `motivation`, forcing the LLM to quote the intent text rather than paraphrase freely.

### New goal terminal status: `superseded`

`superseded` is distinct from `skipped` (abandoned), `expired` (past deadline), and
`decomposed` (broken up). It means the goal was mooted by aspiration-level intent
satisfaction — the aspiration's outcome made this work unnecessary. The trailing-goal
transition can ONLY happen via `aspirations-complete-intent.sh`; `aspirations-update-goal.sh`
rejects direct `status=superseded` writes to keep the evidence gate enforceable.

Superseded goals carry a `superseded_by_aspiration: <asp-id>` backref for trajectory
analysis. Goal-selector's `completion_pressure` scoring treats `superseded` as terminal —
zombie aspirations that close via this pathway stop distorting priority signals automatically.

## Aspiration-Level `deadline` Field

An aspiration MAY carry an optional top-level `deadline` field — an ISO-8601 date
(`YYYY-MM-DD`) naming a fixed external deadline for the whole aspiration (e.g. the
ARC Prize 2026 final-submission clock, `2026-11-02`). It is distinct from a goal's
`resolves_by`/`deadline`: it applies to every goal under the aspiration without each
goal having to restate it.

```yaml
deadline: "2026-11-02"   # optional ISO-8601 date; omit when the aspiration has no fixed external deadline
```

**Consumption (goal-selector `deadline_urgency`, g-318-04):** when a candidate goal
has no own `resolves_by`/`deadline`, the scorer inherits its aspiration's `deadline`
(`goal.get("resolves_by") or goal.get("deadline") or asp.get("deadline")`). The
urgency ramp is:

| Days until deadline | `deadline_urgency` raw |
|---|---|
| <= 1 | 3 |
| <= 3 | 2 |
| <= 7 | 1 |
| <= 30 | 0.5 |
| <= 90 | 0.25 |
| > 90 or unset | 0 |

The long-horizon steps (0.5 at 30d, 0.25 at 90d) let a fixed deadline create gentle
prioritization pull months out — enough to bias selection toward deadline-bound work
without overriding near-term urgency (3/2/1) or `priority` weight. Omitting `deadline`
leaves scoring byte-identical to the pre-g-318-04 behavior (the inherited value is
`None`, so `remaining` is `None` and `deadline_urgency` is 0).

## Self-Generated Aspirations: `supply_evidence` and the aspiration-supply gate

An aspiration the agent invents for itself — the all-blocked B2 → create-aspiration
from-self path, B6.7 blocker-pattern synthesis, complete-review replacement
generation — is the one class of record where the writer had no external signal
and every incentive to fill an empty queue. The daemon's add endpoint runs
`core/scripts/gates/aspiration_supply.py` (gate id `aspiration-supply-gate`,
g-357-82 / g-357-83) on every aspiration BEFORE the per-goal gates. It fires when
`origin_signal` contains any of `idle_supply.gated_origin_patterns`
(`all_blocked`, `all-blocked`, `idle_fallback`, `blocker_pattern`,
`constraint-aware`, `successor`, `replacement`, …) or when `motivation` is
successor-shaped ("Replace(s) completed X with Y") under ANY origin — a
replacement for completed work is self-generated by construction, and stamping
it `user_directive` is an origin misattribution the gate refuses. User-directed
and board-derived aspirations return `gated: false` and are untouched.

Aspiration-level fields (all optional on user-directed records, REQUIRED on
gated ones):

```yaml
supply_evidence:
  gap: string       # >= 40 chars — what is MISSING, concretely (file, node, capability, user-visible outcome)
  needle: string    # >= 30 chars — what the user can do or see once this lands
  checked: [string] # >= 2 entries that EXIST: asp-/g- ids in the store, tree node keys,
                    # files under world/ meta/ agents/ or the project root, msg-/rb-/guard-/sig-/pq- ids,
                    # hypothesis ids (YYYY-MM-DD_slug). URLs and category labels are not referents.
created_at: ISO     # stamped by the add endpoint (setdefault — transplant callers keep their own)
created_by_agent: s # stamped from the request's agent header; both feed the daily cap
```

Checks, in order (all must hold): `origin_misattributed`, `supply_evidence_missing`,
`gap_unspecified` / `needle_unspecified`, `referents_unverified`, `blocker_as_gap`
(the motivation's first sentence or the gap is blocker-shaped — "X is human-blocked
/ awaiting approval / until access is restored"; a blocked lane is a reason to wait,
not to build adjacent work; `Unblock:`-titled aspirations are exempt because they
target the blocker itself), `overlaps_active` (≥ `overlap_threshold` of the
candidate's distinctive tokens already belong to an ACTIVE aspiration → file goals
under it), `overlaps_archived` (same against a completed/retired one, allowed only
when that id is in `checked`), `daily_cap` (`max_self_generated_per_agent_per_day`), `no_goals`
(`min_goals`, default 1 — an aspiration filed without a first verifiable goal is a title, not work:
the selector has nothing to pick and the loop stays all-blocked). Path referents resolve from the
world, meta, project and agent roots AND tree-relative (`intelligence/foo.md` → `world/knowledge/tree/intelligence/foo.md`).
Refusal shape: HTTP 400 `{"error": "aspiration_supply_blocked", "gate":
"aspiration-supply-gate", "gate_output": {failures[], overlaps[], remedy}}`.

Override: `aspirations-add.sh --override-supply "<why>"` (header
`X-Mind-Override-Supply`; `--override-all` fans into it) — audited to
`world/aspiration-supply-overrides.jsonl` and to the bulk ledger. Reserved for
user/operator-directed re-filing; the from-self path never overrides.
Pre-flight the same evaluator on a candidate with
`echo '<json>' | py -3 core/scripts/aspiration-supply-gate.py --output human`
(rc 0 pass / 1 block / 2 usage). Thresholds: `core/config/aspirations.yaml`
→ `idle_supply`. Tests: `core/tests/gates/test_aspiration_supply_gate.py`
(replay of the deployment portfolio that motivated the gate),
`mind_api/tests/test_runtime_aspiration_supply_gate.py` (daemon wiring).

## Goal-ID Argument Convention (unified)

Every script in this repo that takes a goal ID accepts ALL of these flag forms
interchangeably, regardless of which form the script's docstring shows:

```
<script>.sh --goal g-115-03        # canonical flag
<script>.sh --goal-id g-115-03     # alias of --goal
<script>.sh --goal=g-115-03        # equals form
<script>.sh --goal-id=g-115-03     # equals form
```

Bare positional `g-NNN-NN` ALSO works on the 6 scripts whose underlying API is
positional (see list below) — that's their natural argument handling, not a
normalizer feature. For the 6 flag-API scripts, an explicit `--goal`/`--goal-id`
flag is required.

**Recommended canonical form for new pseudocode:** `--goal <id>`. Reads identically
whether the script's underlying API is positional or flag-based and removes the
LLM cognitive load of remembering which scripts use which style.

### Why dual-accept exists

Historical: 6 data-layer scripts (`aspirations-claim.sh`, `-release.sh`,
`-complete-by.sh`, `-update-goal.sh`, `agent-aspirations-update-goal.sh`,
`goal-completion-evidence.sh`) take a positional `<goal-id>`. 6 orchestration
scripts (`iteration-close.sh`, `predicate-eval.sh`, `utilization-feedback.sh`,
`background-jobs.sh`, `pending-agents.sh`, `meta-impk.sh`) take `--goal` (and
`meta-impk.sh` historically used `--goal-id`). Within a single loop iteration the
LLM bounced between three styles. The dual-accept normalizer removes that
friction without breaking any pre-existing call site.

### Implementation

`core/scripts/_goal-arg-normalize.sh` — a sourceable shell library. Each of
the 12 wrappers above sources it with `GOAL_NORMALIZE_TARGET={positional|--goal|--goal-id}`
to declare its underlying API. The library reads `$@`, extracts the goal value
from any of the four flag forms, and rewrites `$@` into the target form before
the script reaches its parse logic. Adding a new goal-id-accepting script
requires only:

```bash
GOAL_NORMALIZE_TARGET=--goal source "$CORE_ROOT/scripts/_goal-arg-normalize.sh"
```

inserted before the script reads its arguments.

### What dual-accept does NOT do

- It does NOT inspect bare positional values. The earlier draft auto-detected
  `^g-[0-9]+-[0-9]+$`, but that broke `iteration-close.sh --summary "g-NNN-NN"`
  by hijacking the summary value as a goal id. Single source of truth: the goal
  id arrives via an explicit flag, OR via the script's own positional contract.
- It does NOT change the underlying Python script's API. Wrappers still forward
  the canonicalised form to Python via `--goal`/`--goal-id`/positional as before.
- It does NOT guard against malformed goal IDs — that validation happens in the
  Python layer.
