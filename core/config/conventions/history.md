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
{timestamp}_{agent}{extension}
```

- **timestamp**: `YYYY-MM-DDTHH-MM-SS` (hyphens, not colons — filesystem safe)
- **agent**: Name of the agent making the change
- **extension**: Same as the original file

Optional `.meta` sidecar: `{snapshot}.meta` contains a one-line summary.

## How It Works

All write scripts delegate to `_fileops.py` locked write functions. These automatically:
1. Acquire a file lock
2. Copy the current file to `.history/` (via `save_history`)
3. Perform the atomic write
4. Append to `changelog.jsonl`
5. Release the lock

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
