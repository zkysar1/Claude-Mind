# Journal Index JSONL Format

The journal session index uses JSONL (one JSON object per line) with script-based access.
Journal content `.md` files remain in `agents/<agent>/journal/{year}/{month}/{YYYY-MM-DD}.md` as before.
The JSONL index tracks which sessions wrote to which journal files.

## File Layout
- `agents/<agent>/journal.jsonl` — Session index (one record per session). **Script-written** via
  `journal-add.sh` / `journal-update.sh` / `journal-merge.sh`.
- `agents/<agent>/journal/{year}/{month}/{YYYY-MM-DD}.md` — Narrative detail file (content).
  **Hand-written** by the agent. NOT created, appended to, or modified by
  `journal-add.sh`. The `journal_file` field inside the JSONL record is a POINTER to
  this detail file, not an instruction to write there.

## The Two-File Pairing (CRITICAL)

Every journal session has TWO artifacts that MUST stay in sync:

1. **Index entry** in `agents/<agent>/journal.jsonl` — written by `journal-add.sh` from stdin JSON.
2. **Narrative detail** in `agents/<agent>/journal/YYYY/MM/YYYY-MM-DD.md` — written by hand
   (Write for new files, Edit for appending additional session blocks during a day).

The scripts enforce path validity and schema of the index entry only. They do NOT
touch the `.md` file. Callers who rely on `journal-add.sh` alone and assume the
narrative was also written are filing a false-negative conclusion — the index says
"session happened on this file" but the file itself may have no content for that
session. Always pair the script call with an Edit/Write to the referenced `.md`.

See session 96 (source of this convention) for the incident where this pairing
was implicit and produced a false missing-detail-file audit.

## Record Schema
Required on stdin: `journal_file` — the stored field VALUE is agent-name-scoped and carries **no `agents/` prefix**: it is the bound agent's name followed by `/journal/YYYY/MM/YYYY-MM-DD.md` (the validator anchors `^{agent}/journal/...$` with the literal bound-agent name). This field value is agent-relative — distinct from the on-disk path `agents/<agent>/journal/...` referenced above.
Auto-allocated / auto-defaulted if absent: `session` (next integer = max+1), `date` (today ISO)
Defaults: `goals_completed` (`[]`), `key_events` (`[]`), `tags` (`[]`), `hypotheses_resolved` (`0`), `hypotheses_created` (`0`)
Array-of-strings fields (validator-enforced — rejects a non-list value or a non-string element): `goals_completed`, `key_events`, `tags`
Authoritative schema: `mind_api/src/store_registry.py` → `STORE_REGISTRY["journal"]` + `_journal_validate` (daemon-only runtime).

## Script-Based Access (Exclusive Data Layer)
The LLM NEVER reads or edits `agents/<agent>/journal.jsonl` directly. All operations go through scripts:

| Script | Purpose | Stdin |
|--------|---------|-------|
| `journal-read.sh --session <n>` | Record for session N | — |
| `journal-read.sh --date <YYYY-MM-DD>` | Records for a date | — |
| `journal-read.sh --recent [N]` | Last N session records (default 5) | — |
| `journal-read.sh --summary` | Compact one-liner per session | — |
| `journal-add.sh` | Validate + append new session record | JSON |
| `journal-update.sh <session>` | Update existing session record | JSON |
| `journal-merge.sh <session>` | Merge fields into existing record | JSON |

All backed by the `core/scripts/journal-*.sh` wrappers above (Python 3, stdlib only). Direct read/write of `agents/<agent>/journal.jsonl` is prohibited — use the wrappers exclusively.

## Journal vs Experience

Journal = per-session narrative summary. Experience = per-goal or
per-hypothesis full-fidelity evidence. Both stores describe "things that
happened" at different granularities and serve different readers. The
full comparison lives in `core/config/conventions/learning-routing.md`.

Mnemonic: experience is evidence; journal is narrative.
