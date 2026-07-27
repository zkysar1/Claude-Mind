# Temp Store Convention

Canonical temp store for agent working documents pending drain to the
knowledge tree. The single permitted write target for transient working
files that used to scatter across `reports/` and ad-hoc locations.

- **Path**: `agents/<agent>/temp/`
- **Lifecycle**: between-step staging — preserved across iterations,
  compactions, and recovery; drained to the knowledge tree by the
  `/drain-temp` skill (Phase 5 of the file-model normalization).
- **Durability**: own-cloud S3-synced (git-ignored). The owncloud sweep
  pushes `temp/` to S3 like other governed agent state (it is not in
  `_EXCLUDE_DIRS`), and `pull_temp` — folded into `pull_continuity`, run at
  `/start` — resumes it on a machine-move via a prefix listing + the same
  no-clobber freshness gate as session continuity. So temp/ working docs
  survive a cross-machine agent move without waiting on a git round-trip.
  ALL of `temp/` is gitignored (g-115-1765) — working docs, the `drained/`
  audit trail, pure ephemera (`.log`/`.txt`), and any ad-hoc scripts/subdirs.
  Durability is the S3 sync above, not git: `temp/` is a transient staging area
  (everything drains to the tree or is discarded), so it does not belong on the
  shared git surface — and cross-agent temp peeking is an anti-pattern anyway
  (see 'Searching temp/'). Only `.gitkeep` is tracked, to preserve the dir on a
  fresh clone. Unlike `session/`/`sessions/`, the ignore is now a portable
  committed `.gitignore` rule (it previously lived only in a machine-local
  `.git/info/exclude`, which did not travel to fresh boxes — the g-115-1765 bug).

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
| Git | Gitignored — all of `temp/` except `.gitkeep` (g-115-1765); durability via S3 sync | Gitignored (`**/session/`) |

**Decision rule**: if the content might be worth encoding into the
knowledge tree later, write to `temp/`. If it is a throwaway IO buffer
consumed within the current step, write to `session/scratch/`.

## File naming

Flat directory, timestamped filenames for uniqueness:

```
temp/<type>-<YYYY-MM-DDTHH-MM-SS>.md
temp/<type>-<YYYY-MM-DDTHH-MM-SS>.json
```

Examples: `temp/design-notes-2026-06-02T14-30-00.md`,
`temp/diag-<subsystem>-2026-06-02T14-30-00.md`,
`temp/audit-<topic>-2026-06-02.md`.

Cadence-ritual briefings (`fresh-eyes-*.md`, `fresh-eyes-program-*.md`,
`fresh-eyes-tree-*.md`, `felt-sense-*.md`) are the EXCEPTION: they self-encode
their durable value at creation (their own Phase 5.6 / lane sweeps write to
tree/RB/guardrails/Self), so they are archival-by-design and land straight in
`temp/drained/` — they never enter the drain queue as already-encoded slush
(g-115-1838). Do not treat a `temp/drained/fresh-eyes-*.md` as an undrained
working doc.

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
carry zero retrieval value (their knowledge is in the tree). As of
g-115-2948 this GC is **automated** — `temp-drain-purge.sh` **Lane 2**
(`gc_drained_archive`) prunes `drained/` files older than `--drained-age-days`
(default 30) on every run, via the same guarded `find -maxdepth 1 -type f
-mtime +N -delete` pattern (the `drained/` dir itself is always preserved).
No separate maintenance goal is needed. The "no other subdirectories" rule
above is likewise enforced by **Lane 3** (`cleanup_stray_dirs`): any dir
DIRECTLY under `temp/` that is NOT `drained/` and is untouched past the
`--age-min` guard (default 120 min) is removed via a per-dir-bounded guarded
`find "$stray" -delete` — sweeping abandoned scratch subdirs (e.g. a leftover
`gNNN-session/`) that neither the ephemera purge (`-type f`) nor the `.md`/
`.json` drain ever reach.

## Pure ephemera (.log/.txt/.py/.sh/.err/.raw/.out/.bak + 0-byte empties) — purged, not drained

temp/ holds TWO file classes, and only ONE of them drains:

| Class | Extensions | Carries knowledge? | `/drain-temp` action |
|---|---|---|---|
| Drainable working docs | `.md`, `.json` (with content) | Yes — analyses, briefings, designs | Encode to tree/RB/experience, then move to `drained/` |
| Pure ephemera | `.log`, `.txt`, `.py`, `.sh`, `.err`, `.raw`, `.out`, `.bak` | No — test-suite output, tool dumps, one-off scratch scripts, raw command-output dumps, backup copies | **Purge (delete)** in Phase 1.5, once older than a 120-min age guard |
| Empty scratch | ANY name **except dotfiles**, 0 bytes (`-empty`) | No — nothing was ever written | **Purge (delete)** in Phase 1.5, once older than the 120-min age guard |

> ⚠ **`.py`, `.sh`, `.raw`, `.out`, `.bak`, and 0-byte files are purged too.**
> The glob in `core/scripts/temp-drain-purge.sh` is
> `*.log, *.txt, *.py, *.sh, *.err, *.raw, *.out, *.bak` OR `-empty` (0-byte,
> any name). A one-off helper script, a raw command-output dump
> (`selector.raw`, `probe.out`), a `.bak`, or an empty scratch file left in
> `temp/` **will be deleted** once it is >120 min old. That is the intended
> behaviour (these are ephemera), but do not leave a script or dump you want to
> keep here — promote a script to `core/scripts/`/`world/scripts/`, and encode a
> dump's value (or move it out) before it ages past the guard. **Dotfiles (names
> starting with `.`) are EXCLUDED from both lanes** (`! -name '.*'`): temp/'s
> tracked 0-byte `.gitkeep` (and any dotfile marker) is protected from the
> `-empty` lane — else the drain would delete the only git-tracked file in temp/
> and iteration-commit would commit the deletion, breaking the fresh-clone dir
> guarantee above (g-115-2947 fresh-eyes catch). This table and the `find_expr`
> glob in `temp-drain-purge.sh` MUST be updated together.

**Raw command-output dumps** (redirecting `goal-selector.sh`, `retrieve.sh`, a
`/tree` summary, or any script's stdout to a file for inspection) are IO buffers
with no reuse value — per the `temp/ vs session/scratch/` table above they
belong in `session/scratch/`, not `temp/`. When convenience lands one in `temp/`,
name it with a `.raw` or `.out` extension (`selector.raw`, NOT `selector.json`)
so Phase 1.5 **purges** it, rather than Phase 1 enumerating a bare `.json` as a
drainable working doc and Phase 3 archiving megabytes of valueless scratch into
`drained/`. The extension is the stable purge marker; a bare-named `.json` dump
is treated as a working doc and drained. (g-115-2947)

Pure ephemera lands in temp/ legitimately — the framework's own guidance
redirects test-suite output here (`.claude/rules/run-full-suite-after-deep-code.md`
writes `agents/<agent>/temp/suite.log`), and one-shot tool dumps (`leak-check.txt`)
follow the same path. These files have nothing to encode, so `/drain-temp`
DELETES them rather than archiving to `drained/`: all of `temp/` (including
`drained/`) is gitignored (g-115-1765), so archiving untracked ephemera into
`drained/` would only relocate slush between two ignored paths. Deletion loses
no history — there is none to lose (nothing under `temp/` is git-tracked). The
gitignore is now a portable committed `.gitignore` rule; it previously lived
ONLY in a machine-local `.git/info/exclude`, which did not travel to fresh
boxes — so temp/ committed there every iteration until g-115-1765 moved the
ignore into the shared `.gitignore`.

Both classes feed the aspirations-precheck temp-pressure signal
(`core/scripts/precheck-eval.py` `cmd_temp_pressure`): `count` (docs) +
`ephemera_count` (.log/.txt) = `pressure_count`, which drives the warn / drain
thresholds. Before g-115-1727 the metric AND the drain glob both saw only
`.md`/`.json`, so ephemera-only accumulation was invisible to both and grew
unbounded — the exact slush-directory failure mode this convention exists to
prevent, for the one file class the drain missed.

The 120-min purge age guard protects an actively-written `suite.log` from an
in-flight run (the daemon-safe full suite is ~32 min); a just-completed log is
purged on the next drain cycle. The temp-pressure metric applies NO age guard —
it counts all ephemera so a recent slush still triggers the drain that will
later purge it.

### The purge MUST go through the guarded helper — never a hand-rolled `rm`

The Phase 1.5 purge MUST call `core/scripts/temp-drain-purge.sh` — the canonical
GUARDED purge path. Do NOT hand-roll an `rm` (or reconstruct the find/rm inline)
on a temp-dir variable. The helper asserts the temp dir is set + non-empty,
absolute, strictly under `PROJECT_ROOT`, and `basename=='temp'` BEFORE any
deletion, then deletes via `find … -maxdepth 1 -type f (ephemera globs) -mmin
+120 -delete` — never a per-file `rm` on an interpolated path (and `-maxdepth 1`
leaves `drained/` untouched).

WHY (g-115-1876): a hand-rolled `rm -f "$TEMP_DIR/$f"` where `$TEMP_DIR` resolves
empty becomes an `rm` on a root-relative path, which Claude Code flags as a
dangerous-rm and PROMPTS for confirmation EVEN under
`--dangerously-skip-permissions` (the fleet launch mode). An autonomous agent
cannot answer its own dialog, so the loop looks alive (`agent-state=RUNNING`)
while hanging at zero progress until a human intervenes — an agent hung 46+ min
this way (observed 2026-07-09, cc-05). The guarded helper eliminates the whole
hand-rolled-rm class: there is exactly one purge path, and it fails loud
(non-zero exit, deletes nothing) rather than ever issuing a dangerous rm.
Regression-guarded by `core/scripts/tests/test_temp_drain_purge.sh` (8 guard
cases + a dry-run smoke + purge-lane behavior tests that assert the two lanes
— ephemera extensions and 0-byte empties — purge while content-docs, fresh
files, and `drained/` contents are excluded, run against the SSOT
`_purge_find_predicate` function).

**General rule (applies beyond temp):** any framework guidance that has an agent
construct an `rm` on a variable path MUST guard the variable (set + non-empty +
expected-shape) first, or route through a helper that does. An unguarded `rm` on
a possibly-empty variable is an agent-hang hazard, not just a data-loss hazard —
the dialog it triggers cannot be answered by an autonomous agent.

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

## Migration (reports/ → temp/ → removed)

`agents/<agent>/reports/` no longer exists. It was the legacy slush pile for
working docs (analyses, fresh-eyes reviews, phase plans); the file-model
normalization replaced it with `temp/` (a staging area that DRAINS to the
knowledge tree) plus this allowlist gate. reports/ was briefly retained as a
FROZEN git-tracked archive — then **removed entirely** (user-directed,
2026-06-02): all 665 legacy files across the 6 agents were `git rm`'d and the
directories deleted. **Git history is the archive** — every removed file stays
recoverable via `git log` / `git show`, so nothing is lost.

Writers were repointed in the earlier Phase 3 BEFORE the gate activated —
briefings (`fresh-eyes-*`, `felt-sense-*`) write to `temp/`; phase-cost
telemetry and the completion delta-baseline write to `session/`; the
timestamped completion-report archive was dropped (the `COMPLETION-REPORT.md`
pointer's git history is its archive). So no live writer targets reports/.

**Why removed, not kept frozen (2026-06-02 override).** The earlier Phase-6
frozen-archive compromise had already established that the legacy corpus was
overwhelmingly already-captured or stale — goal-closure artifacts whose learning
was encoded into the tree at closure, or superseded proposals. The ~26 tree-node
citations that previously resolved to on-disk reports files have been folded: the
dangling pointers were removed and any essential detail inlined into the node,
with the source marked git-archived. With the corpus encoded-or-stale and the
citations folded, the on-disk archive no longer earned its keep; removing it
eliminates a second, disconnected retrieval surface entirely. git history
preserves the bytes — `git show <rev>:agents/<agent>/reports/<file>` recovers any
one of them.

The Phase-4 allowlist gate still DENIES `reports/` (it is not on the permitted
list), which prevents the directory from being silently recreated. There is no
agent-side override — a genuinely needed write goes to `temp/` (working docs) or
`session/` (operational state). A bulk historical recovery, if ever needed, uses
`git checkout`/`git show` of the removed paths.

## Cross-references

- `core/config/conventions/learning-routing.md` — the full "where does this
  learning go?" decision tree across all stores
- `core/config/session-manifest.yaml` — `session/` file tiers (`temp/` is
  outside `session/` and is not manifest-governed)
- `core/config/conventions/session-state.md` — two-tier session layout
- `.claude/rules/path-resolution.md` — L1 path governance and cruft prevention
