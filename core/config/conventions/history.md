# File History Convention

## Overview

Self-contained file versioning via `.history/` directories. Before any write script overwrites a file, it saves a copy with a timestamp. Agents can browse, diff, and restore any historical version.

## Directory Structure

```
world/.history/                               — Mirror structure of world/
  knowledge/tree/weather.md/                  — One dir per versioned file
    2026-03-26T14-30-00_alpha.md              — Snapshot before alpha's edit
    2026-03-26T15-45-00_beta.md               — Snapshot before beta's edit
  aspirations.jsonl/
    2026-03-26T14-30-00_alpha.jsonl           — Snapshot before alpha's change
```

History directories mirror the path structure of the base directory (world/ or meta/).

## Snapshot Filename Format

```
{timestamp}_{agent}{extension}.gz
```

- **timestamp**: `YYYY-MM-DDTHH-MM-SS` (hyphens, not colons — filesystem safe)
- **agent**: Name of the agent making the change
- **extension**: Same as the original file
- **.gz**: gzip compression (default since fix-ballooning-history-2026-05-22).
  Compression ratio for text/JSONL/YAML/MD is typically 5–10×.

Optional `.meta` sidecar: `{snapshot}.meta` (i.e. `<file>.<ext>.gz.meta`) contains
a one-line summary. The sidecar is **not** compressed — it stays grep-able.

**Backward compatibility**: legacy uncompressed snapshots (`{timestamp}_{agent}{extension}`
without the `.gz` suffix) remain readable indefinitely. `history-list.sh`,
`history-restore.sh`, `history-diff.sh`, and `history-prune.sh` all handle both
forms. `history-restore.sh <file> <version>` accepts the version name with or
without `.gz` — the resolver tries both.

## Snapshot Blacklist

Some files have no real restore value and produce pathological history growth
when snapshotted on every write. The blacklist in `core/scripts/_fileops.py`
(`_SNAPSHOT_BLACKLIST`) names those files; matching writes **skip** the
`.history/` snapshot but still append to `changelog.jsonl` (the audit trail
is preserved).

Current entries:

| Base | Pattern | Why |
|------|---------|-----|
| world | `presence/` | Per-agent liveness heartbeats. Rewritten >>1 Hz across all running agents. Zero historical interest — current state is the only state that matters. |
| world | `board/` | Append-only message-board logs (coordination/findings/decisions/general/etc.). The file IS the history; full-file snapshots multiply storage by O(N²) with no restore value (changelog.jsonl keeps the audit trail). Added g-115-2789-b (2026-07-20) after pre-g-115-2410 daemon direct-writes accreted ~1.15G of frozen board snapshots. |
| meta | `gate-firings.jsonl` | Append-only gate-decision audit log. The file IS the history; full-file snapshots multiply storage by O(N²). |

To add an entry:

1. Confirm the file truly has no restore value (the changelog and the live
   file together must be sufficient for every conceivable audit).
2. Add the pattern to `_SNAPSHOT_BLACKLIST` in `core/scripts/_fileops.py`.
   Use a key from `_classify_base()` (`world` / `meta` / `agent` / `claude`).
   Trailing `/` matches all paths under that directory.
3. Delete the corresponding `.history/<pattern>/` subtree (it is dead
   storage from this point forward — free it).
4. Add a row to the table above with the "why".
5. Pair the SSOT-pair: a test in
   `core/scripts/tests/test_fileops_snapshot_blacklist_and_gzip.py` that
   asserts the new pattern is honored.

## Per-File Snapshot Cap

Even with the blacklist and gzip, files written hundreds of times per day
(aspirations.jsonl, reasoning-bank.jsonl) accumulate snapshots
faster than the weekly date-tiered prune can keep up. The per-file cap
bounds that growth continuously inside the write path.

Policy: `DEFAULT_SNAPSHOT_CAP = 500` for any file not in
`_PER_FILE_SNAPSHOT_CAP`. Known high-churn files have lower per-file caps:

| Base | File | Cap |
|------|------|-----|
| world | `aspirations.jsonl`        | 100 |
| world | `reasoning-bank.jsonl`     | 100 |
| world | `guardrails.jsonl`         | 100 |
| world | `pipeline.jsonl`           | 100 |
| world | `team-state.yaml`          | 100 |
| world | `aspirations-meta.json`    |  50 |
| meta  | `changelog.jsonl`          | 100 |
| meta  | `improvement-velocity.yaml`|  50 |
| meta  | `goal-selection-strategy.yaml`| 50 |
| meta  | `spark-questions.jsonl`    | 100 |

(spark-questions cap added g-115-2410 after measuring 877 snapshots in 4 days
on cc-04. Board channels were also capped then, but that cap was SUPERSEDED by
the `board/` blacklist in g-115-2789-b (2026-07-20) — blacklisted files get
zero snapshots, so no cap ever applies to them. Note the caps bound the LEGACY
per-file tree only — the Stage-2 CAS-delta store dedups content and is GC'd by
`_history_store.vacuum`, so it needs no count cap.)

Enforcement: `save_history()` calls `_prune_to_cap()` immediately after
writing a new snapshot, dropping the oldest non-`.meta` files by parsed
timestamp until count ≤ cap. Paired `.meta` sidecars are dropped with
their snapshots. Unparseable filenames (legacy artifacts, manual backups)
are left alone. OneDrive lock/permission errors are swallowed — the
weekly recurring prune sweeps what auto-prune couldn't drop in-call.

Latency bound: a single `_prune_to_cap` call drops at most
`MAX_SNAPSHOTS_DROPPED_PER_CALL` (default 50) snapshots, so the next write
to a file currently at thousands-over-cap doesn't hang for 30-60s inside
the locked write. Excess surplus drains across multiple successive writes;
high-churn files converge to cap in <1 day at their normal write rate.

Lookup precedence:
1. Per-base named override
2. `DEFAULT_SNAPSHOT_CAP`

To add or change an override: edit `_PER_FILE_SNAPSHOT_CAP` in
`core/scripts/_fileops.py`. Pair the change with a regression test in
`core/scripts/tests/test_fileops_snapshot_blacklist_and_gzip.py`.

The cap is the steady-state bound. Existing surplus snapshots are trimmed
naturally over the next N writes (where N = current_count - cap).

## How It Works

There is ONE snapshot writer: `_fileops.save_history`. Direct-python write
scripts call it via the `_fileops.py` locked write functions; the daemon's
write path (`mind_api/src/history.py::snapshot`) DELEGATES to it since
g-115-2410 (2026-07-16). Before that unification the daemon re-implemented
the Stage-0 format (uncompressed full copy, no cap, no blacklist) and grew
one box's `.history/` by 13.9GB in 4 days — the docstring's "mirrors
_fileops exactly" claim had gone three generations stale. Delegation makes
that divergence class structurally impossible.

All write scripts delegate to `_fileops.py` locked write functions. These automatically:
1. Acquire a file lock
2. Check the snapshot blacklist; skip steps 3 + 6 for matching files
3. gzip-compress the current file into `.history/` (via `save_history`)
4. Perform the atomic write
5. Append to `changelog.jsonl`
6. Auto-prune the per-file snapshot dir to the cap (oldest dropped first)
7. Release the lock

No manual history calls needed — it happens transparently on every write.

## Script API

### List versions
```bash
bash core/scripts/history-list.sh <file>
```

### Restore a version
```bash
bash core/scripts/history-restore.sh <file> <version-name>
```
Saves current state before restoring (so restores are reversible).

### Diff current vs historical
```bash
bash core/scripts/history-diff.sh <file> <version-name>
```

### Prune old snapshots
```bash
bash core/scripts/history-prune.sh [--dry-run]
```

Retention policy:
- Keep all versions from the last 7 days
- Keep one daily snapshot for days 8-30
- Keep one weekly snapshot for days 31+

### Manual save (non-Python scripts)
```bash
bash core/scripts/history-save.sh <file> <agent> [summary]
```

## Changelog

`world/changelog.jsonl` — auto-appended by every locked write operation:

```json
{
  "timestamp": "2026-03-26T14:30:00",
  "agent": "alpha",
  "file": "knowledge/tree/weather.md",
  "action": "edit",
  "summary": "",
  "lines_changed": 3
}
```

### Read changelog
```bash
bash core/scripts/changelog-read.sh [--since <duration>] [--agent <name>] [--file <substring>] [--last <N>] [--json]
```

### Changelog stats
```bash
bash core/scripts/changelog-stats.sh [--since <duration>]
```

## Cross-Machine Behavior

File locks are local — they don't protect across machines synced via OneDrive/NAS. For cross-machine:
- **Last-writer-wins** (standard filesystem behavior)
- **Nothing is lost** — the overwritten version is preserved in `.history/`
- **The changelog records what happened** — agents can detect conflicts
