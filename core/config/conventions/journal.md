# Journal Index JSONL Format

The journal session index uses JSONL (one JSON object per line) with script-based access.
Journal content `.md` files remain in `mind/journal/{year}/{month}/{YYYY-MM-DD}.md` as before.
The JSONL index tracks which sessions wrote to which journal files.

## File Layout
- `mind/journal.jsonl` — Session index (one record per session)
- `mind/journal/{year}/{month}/{YYYY-MM-DD}.md` — Content files (unchanged)

## Record Schema
Required: `session`, `date`, `journal_file`
Defaults: `goals_completed` (0), `goals_attempted` (0)
Optional: `key_events`, `hypotheses_resolved`, `aspirations_created`

## Script-Based Access (Exclusive Data Layer)
The LLM NEVER reads or edits `mind/journal.jsonl` directly. All operations go through scripts:

| Script | Purpose | Stdin |
|--------|---------|-------|
| `journal-read.sh --session <n>` | Record for session N | — |
| `journal-read.sh --date <YYYY-MM-DD>` | Records for a date | — |
| `journal-read.sh --recent [N]` | Last N session records (default 5) | — |
| `journal-read.sh --summary` | Compact one-liner per session | — |
| `journal-add.sh` | Validate + append new session record | JSON |
| `journal-update.sh <session>` | Update existing session record | JSON |
| `journal-merge.sh <session>` | Merge fields into existing record | JSON |

All backed by `core/scripts/journal.py` (Python 3, stdlib only).
