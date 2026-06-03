# Temp Store Convention

Canonical temp store for agent working documents pending drain to the
knowledge tree. The single permitted write target for transient working
files that used to scatter across `reports/` and ad-hoc locations.

- **Path**: `agents/<agent>/temp/`
- **Lifecycle**: between-step staging — preserved across iterations,
  compactions, and recovery; drained to the knowledge tree by the
  `/drain-temp` skill (Phase 5 of the file-model normalization).
- **Durability**: git-tracked AND own-cloud S3-synced. The owncloud sweep
  pushes `temp/` to S3 like other governed agent state (it is not in
  `_EXCLUDE_DIRS`), and `pull_temp` — folded into `pull_continuity`, run at
  `/start` — resumes it on a machine-move via a prefix listing + the same
  no-clobber freshness gate as session continuity. So temp/ working docs
  survive a cross-machine agent move without waiting on a git round-trip.
  `temp/` is NOT gitignored, unlike `session/` and `sessions/`.

---

## Why this exists

The knowledge retrieval pipeline searches a fixed set of stores (knowledge
tree, reasoning bank, guardrails, pattern signatures, experience, beliefs,
experiential index). A working document written ONLY to a slush directory
(historically `agents/<agent>/reports/`) is in none of them — it is
invisible to `/prime` and `retrieve.sh` forever. A second, disconnected
retrieval surface is not a style problem; it is lost knowledge.

`temp/` resolves this by being explicitly a STAGING area, not an archive:
every file in `temp/` either drains into the knowledge tree (the one
long-term retrieval surface) or is discarded. Nothing in `temp/` is meant
to live there permanently.

## temp/ vs session/scratch/

| Property | `temp/` | `session/scratch/` |
|---|---|---|
| Path | `agents/<agent>/temp/` | `agents/<agent>/session/scratch/` |
| Scope | Agent-wide (not per-session) | Per-session |
| Lifetime | Preserved until drained by `/drain-temp` | Wiped on `/start --recover` and recovery-gate auto-recovery |
| Recovery | preserve (drain is the only deletion path) | clear (`session-manifest.yaml recovery_action: clear`) |
| Content | Working documents with reuse value that DRAIN to the tree: analyses, briefings, audits, design docs, snapshots | IO buffers with no reuse value: probe dumps, JSON staging, one-shot work files |
| Drain target | Knowledge tree / reasoning bank / experience | Nowhere — ephemeral by definition |
| Git | Tracked | Gitignored (`**/session/`) |

**Decision rule**: if the content might be worth encoding into the
knowledge tree later, write to `temp/`. If it is a throwaway IO buffer
consumed within the current step, write to `session/scratch/`.

## File naming

Flat directory, timestamped filenames for uniqueness:

```
temp/<type>-<YYYY-MM-DDTHH-MM-SS>.md
temp/<type>-<YYYY-MM-DDTHH-MM-SS>.json
```

Examples: `temp/fresh-eyes-2026-06-02T14-30-00.md`,
`temp/felt-sense-2026-06-02.md`,
`temp/design-notes-2026-06-02T14-30-00.md`.

(NOT temp/: completion reports are the single `COMPLETION-REPORT.md` pointer at
the agent root — git history is their archive; phase-cost telemetry and the
completion delta-baseline (`last-outcome-snapshot.yaml`) are operational state
under `session/`. temp/ holds only working docs that DRAIN to the knowledge tree.)

Flat directory with ONE structural exception: `drained/` (below). No
other subdirectories, no goal-specific nesting, no ad-hoc scripts.

## drained/ subdirectory

```
temp/drained/
```

When `/drain-temp` (Phase 5) processes a file — extracting its value into
the knowledge tree, reasoning bank, or experience archive — it moves the
file to `temp/drained/` with its original name, leaving an audit trail of
what was drained and when. `temp/drained/` contents older than 30 days
carry zero retrieval value (their knowledge is in the tree) and may be
removed by a maintenance goal.

## Searching temp/

"Search through temp and find that thing" needs no dedicated tool: temp/ is a
flat directory of text files on the local disk, so the agent's own `Grep` /
`Glob` over `agents/<agent>/temp/` IS the search surface. On a multi-machine
setup the continuity pull (above) materializes the agent's temp/ locally, so the
same local `Grep` works on any machine.

There is deliberately NO `temp-search` daemon endpoint: a second search path
would violate single-source-of-truth (two ways to find the same file) for zero
gain over `Grep`, and cross-agent temp peeking is an anti-pattern — agents
coordinate through `world/board/`, not by reading each other's temp/ (see
`core/config/conventions/coordination.md`). Each agent searches its OWN temp/.

## The agent-dir write-surface allowlist

The canonical map of where an agent may write under its own dir, enforced
at write time by the Phase-4 hard gate
(`core/scripts/path-resolution-hook.py`, PreToolUse[Write|Edit|MultiEdit]).

| What you are writing | Write to |
|---|---|
| Reusable domain knowledge, lessons, patterns | Knowledge tree (`world/knowledge/tree/`) — the only retrieval surface |
| Experience traces (narratives of what happened) | `experience/` |
| Daily journal entries | `journal/` |
| Session state (signals, working memory, handoff) | `session/` (registered in `session-manifest.yaml`) |
| Per-session IO buffers / probe dumps | `session/scratch/` |
| Per-session binding metadata | `sessions/<SID>/` |
| Operational telemetry / diagnostic time-series (phase-costs) and delta baselines (`last-outcome-snapshot.yaml`) | `session/` — machine-local, never knowledge, regenerable |
| Completion-report dashboard (latest pointer) | `COMPLETION-REPORT.md` at the agent root (git history is its archive) |
| **Analyses, briefings, audits, design docs, snapshots** (working docs that DRAIN to the tree) | **`temp/`** — the home for the working docs that used to scatter into `reports/` |
| Agent identity, config, aspiration queue | The registered top-level agent files only (`self.md`, `*.jsonl`, `*.yaml`, …) |

**Permitted top-level directories** under `agents/<agent>/`:
`session`, `sessions`, `journal`, `experience`, `.history`, `temp`.

Anything else — `reports/`, a newly-invented directory, or a stray
top-level file not on the registered list — is denied by the Phase-4 gate
with a redirect here. The gate is an ALLOWLIST: it permits the canonical
locations and denies everything else, rather than blacklisting specific
names. This is deliberate — it teaches that there is a place for each kind
of output, and inventing a new location is the error to avoid.

## Gate behavior (Phase 4)

The allowlist gate lives in `core/scripts/path-resolution-hook.py`
(PreToolUse[Write|Edit|MultiEdit]). For a write under the bound agent dir:

1. The first path segment under `agents/<agent>/` is extracted.
2. If it is one of the permitted directories — ALLOW.
3. If the target is one of the registered top-level agent files — ALLOW.
4. Otherwise — DENY with an educational message listing the routing table
   and redirecting to `temp/`.

`reports` is not named in the deny logic; it is denied because it is not on
the allowlist — the same mechanism that denies any future invented
directory.

## Migration (reports/ → temp/)

`agents/<agent>/reports/` is a FROZEN git-tracked archive. Its existing files
remain on disk (and in git) but no NEW files may be written there once the
gate activates (Phase 4). Writers are repointed in Phase 3 BEFORE the gate
activates — briefings (`fresh-eyes-*`, `felt-sense-*`) move to `temp/`;
phase-cost telemetry and the completion delta-baseline move to `session/`;
the timestamped completion-report archive is dropped entirely (the
`COMPLETION-REPORT.md` pointer's git history is its archive).

**Frozen-archive, not mass-drain (Phase 6 — data-driven).** A census of the
legacy corpus (627 `.md` across 6 agents) found it is overwhelmingly
already-captured or stale: goal-closure artifacts whose learning was encoded
into the tree at closure time, files already cited from tree nodes (~26 — and
those citations resolve because the files stay on disk), or superseded
proposals (e.g. zeta's `new-agents-staging/` proposed charlie/delta/echo, which
now exist). Bulk-encoding that into the tree would inject stale, redundant nodes
and DEGRADE retrieval — the precise failure this whole normalization exists to
prevent. So `reports/` is kept as a frozen git archive, NOT mass-drained and NOT
deleted: the Phase-4 gate stops new writes, Phase 6 removes it from the
own-cloud S3 sweep (`_EXCLUDE_DIRS` — git is its cross-machine transport, not
S3), and any genuinely-unencoded *current* document is drained selectively
on demand (the same `/drain-temp` judgment applied to a named file). git history
+ the on-disk archive preserve everything; nothing is lost.

There is NO migration-bypass marker and no agent-side gate override. The
ordering IS the safety mechanism: every writer is repointed (Phase 3) and
verified before the allowlist gate is turned on (Phase 4), so no live writer
ever hits a denied `reports/` path. A genuinely needed bulk move uses shell
(`git mv`) or an `init-*.sh` step, both of which bypass the Write/Edit hook
by construction.

## Cross-references

- `core/config/conventions/learning-routing.md` — the full "where does this
  learning go?" decision tree across all stores
- `core/config/session-manifest.yaml` — `session/` file tiers (`temp/` is
  outside `session/` and is not manifest-governed)
- `core/config/conventions/session-state.md` — two-tier session layout
- `.claude/rules/path-resolution.md` — L1 path governance and cruft prevention
