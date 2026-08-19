# File History Convention

## Overview

Self-contained file versioning via `.history/` directories. Before any write script overwrites a file, it saves a copy with a timestamp. Agents can browse, diff, and restore any historical version.

## Directory Structure

There are **two** layouts under `.history/`. The content-addressed store is
authoritative; the legacy mirror tree is read-side fallback only.

```
world/.history/
  snapshots/<rel-path-to-file>/<ts>_<agent>.yaml   — AUTHORITATIVE: version manifests
    aspirations.jsonl/
      2026-03-26T14-30-00_alpha.yaml               — ~250 bytes; points at content, is not content
  blobs/<hash[:2]>/<hash[2:]>.gz                   — content-addressed full snapshots
    a3/f91c…e02.gz                                 — name = sha256(uncompressed), payload = gzip(content)
  patches/<hash[:2]>/<hash[2:]>.from.<base>.gz     — binary delta from blob <base> to <hash>

  aspirations.jsonl/                               — LEGACY mirror tree (read-only fallback)
    2026-03-26T14-30-00_alpha.jsonl.gz             — snapshots taken before the 2026-05-22 cutover
```

**The version YAML holds no file content.** It is a manifest carrying `hash`,
`encoding` (`full` | `delta` | `dropped`), `base` (set only when
`encoding: delta`), `size_bytes`, `agent`, `summary`, `timestamp`, and
`chain_length`. Restoring a version reads the manifest, then resolves `hash`
through the store: a `full` encoding reads `blobs/<hash[:2]>/<hash[2:]>.gz`
directly; a `delta` walks back through `patches/` to the nearest full blob and
applies the chain forward. `encoding: dropped` means a vacuum pass deleted the
underlying storage and kept the manifest as audit metadata — **the version is
listed but no longer restorable.**

Every storage file is write-once-immutable and content-addressed, which is what
makes the store safe under multi-machine sync: two boxes writing different
content produce different filenames, so they never collide. An anchor (forced
full blob) is written every `DEFAULT_ANCHOR_INTERVAL` (20) saves, bounding
restore latency by capping patch-chain depth.

The legacy tree mirrors the path structure of the base directory
(`world/` or `meta/`), one directory per versioned file.

### Which layout a write goes to

The selection is **global, not per-file** (`_fileops.save_history` docstring;
Stage 2, 2026-05-22, fix-ballooning-history):

| Condition | Legacy tree | New store |
|---|---|---|
| default | not written | **written (authoritative)** |
| `FILEOPS_HISTORY_KEEP_LEGACY_WRITES=1` | written | written (dual-write rollback hatch) |
| `FILEOPS_HISTORY_USE_NEW_STORE=0` | not written | not written — **no snapshot lands at all** |

The last row is a testing hatch and must not be set in production: with the new
store disabled and legacy writes not re-enabled, `save_history` writes nothing
while still returning normally.

## Snapshot Filename Format

This section describes the **legacy** mirror-tree format. In the authoritative
store, manifests are `<ts>_<agent>.yaml` and the payload files under `blobs/`
and `patches/` are hash-named, not timestamp-named (see "Directory Structure").

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

## What Is Snapshotted — and What Is Not

**A file is versioned only if its write goes through `_fileops.save_history`.**
That is the sole snapshot writer (see "How It Works"). Nothing else in the
system creates a snapshot, so the question "is this file recoverable?" reduces
entirely to "does its write path reach that function?"

Three outcomes, and the third is the one that surprises readers:

| Write path | Snapshot | Changelog |
|---|---|---|
| through an `_fileops` locked write, or the daemon's `history.snapshot` (which delegates to it) | **yes** | yes |
| same, but the file matches `_SNAPSHOT_BLACKLIST` | no (deliberate — see below) | yes |
| **never reaches `_fileops` at all** | **no** | sometimes |

The third row has no opt-in and no marker. A file in that class looks exactly
like a versioned one from the outside: it lives under `world/`, it may well
appear in `changelog.jsonl`, and nothing anywhere announces that it has no
history. It is discoverable only by reading its writer.

### Knowledge-tree node bodies are in the third class

`world/knowledge/tree/**/*.md` node bodies are **not versioned**. Measured
2026-08-10 against the live store: **0 of 2,694** live node `.md` files have a
snapshot in either layout — the new store's `snapshots/knowledge/tree/` holds
exactly one tracked path (`_tree.yaml`, the index) and zero `.md` paths, and the
legacy `.history/knowledge/tree/` directory does not exist.

This is a **gap in the write path, not a deliberate exclusion.** The evidence is
that `knowledge/tree/` does not appear in `_SNAPSHOT_BLACKLIST` — the sanctioned,
documented mechanism for opting a file class out. Nothing was opted out; the
writes simply never arrive.

Both of the two ways a node body gets written miss the snapshot writer:

- **The `/tree` skill writes node `.md` files with the Write tool**, which does
  not touch `_fileops` at any point.
- **The daemon's `add-child` with a `body` field** (`tree_write.py`) calls
  `md_path.write_text(...)` followed by `changelog.append(...)` — changelog, no
  snapshot. Two lines below, `_write_tree_locked` calls
  `history.snapshot(...)` for `_tree.yaml`. Adjacent calls in the same block:
  the index is snapshotted, the body is not.

That asymmetry is why the index has history and the bodies do not, and it is the
reason a changelog entry is not evidence of recoverability. **A tree node edit
is not recoverable from `.history/`.** Recovery for node bodies comes from git
(for committed state) or from the sync backend's own versioning — not from this
subsystem.

**A node that DOES have a snapshot is not counterevidence.** The count is
box-dependent: a second box measured 10 of 1,321 nodes covered, and their
provenance was the sync layer's pre-pull snapshot (taken before an
authoritative remote overwrite), never an edit. So sampling a covered node and
concluding the write path versions bodies is a live trap — check provenance
before reading any non-zero count as coverage.

Auditing this requires probing **both** layouts: bulk stores land under
`.history/<rel-path>/`, node bodies (when they appear at all) under
`.history/snapshots/<rel-path>/`. Probing only the first reports a false 100%
uncovered. Note also that `history-list.sh` is known to emit false-absent
verdicts, so its "No history" output is admissible only when corroborated by a
direct filesystem count.

Cross-references: `guard-1359` encodes the mechanism (the Edit/Write tool does
not snapshot; only the governed `_fileops` path does). `g-115-4186` owns the
open remediation and carries the corpus measurements; the
`tree_nodes_without_prior_version` baseline in `meta/audit-baselines.yaml`
ratchets the count so it cannot silently grow while that fix is pending.

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
| meta | `gate-firings-*.jsonl` (glob) | The date segments the `GATE_FIRINGS_SEGMENTED` flush lane writes instead of the legacy file (cutover 2026-08-17): same append-only audit class, one RMW per box per iteration-close flush. First glob entry — `_is_snapshot_blacklisted` treats an entry containing `*`/`?` as an `fnmatchcase` glob over the relative path. A glob rather than the strict date shape `_gate_log._SEGMENT_RE` owns because `_gate_log` imports `_fileops` (cycle); the only over-match is a hypothetical `gate-firings-archive.jsonl`, the same audit-tail class. |
| world | `reasoning-bank-utilization.jsonl`, `guardrails-utilization.jsonl` (exact) | The two utilization counter sidecars g-358-05's not-yet-shipped writer flushes once per maintenance tick. FULL REWRITES of a ~1-2 MB file, not appends, so unblacklisted they would move the O(N²) churn that goal exists to kill out of the content object and into `.history/`. Restore value is nil: these are **advisory** retrieval-scoring counters with no cross-box read-after-write need, so a lost counter is a cosmetic scoring nuance (changelog still records every write). EXACT basenames, deliberately not a `<kind>-*.jsonl` glob — the names are static, and a glob would take both `<kind>-archive.jsonl` and every date segment. Names are owned by `_utilization_store.counters_name()`; `_fileops` cannot import it back, so the literals are pinned to it by test instead. Widening measured on cc-08 at add time: 0 of 61 existing world `.jsonl` files newly matched. |
| world | date segments `<kind>-YYYY-MM-DD.jsonl` — **deliberately NOT blacklisted** | Recorded because the `gate-firings-*.jsonl` row above pushes the other way and the precedent does not transfer. Those segments are append-only audit tail; rb/guardrail segments are the **content** store and are mutated in place (`status` → retired, `valid_to`, `next_review_eligible_at`), so they carry real restore value. Segmentation also fixes their churn on its own — a write touches one small day-file instead of the 20.7 MB whole store — leaving nothing for a blacklist entry to buy. |

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

Write **scripts** delegate to the `_fileops.py` locked write functions — but not
every write in the system is a write script. Direct `Write`/`Edit` tool writes
and the daemon's node-body `write_text` bypass this path entirely and are
therefore unversioned (see "What Is Snapshotted — and What Is Not"). For writes
that DO go through it, these steps happen automatically:
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
- **The overwritten version is preserved in `.history/` — for versioned files
  only.** This is not a blanket guarantee: it holds exactly for the first row of
  the table in "What Is Snapshotted — and What Is Not", and does NOT hold for
  blacklisted files or for any file whose write path never reaches
  `_fileops.save_history` (knowledge-tree node `.md` bodies among them). For
  those, a cross-machine overwrite is lost as far as this subsystem is
  concerned.
- **The changelog records what happened** — agents can detect conflicts. A
  changelog entry does not imply a snapshot exists; the two are written by
  different calls and the second is frequently absent.
