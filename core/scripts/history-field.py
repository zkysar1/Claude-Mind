#!/usr/bin/env python3
"""history-field — read ONE record's ONE field out of a historical snapshot.

WHY THIS EXISTS (g-115-8462). Recovering a clobbered narrative note needs the
PRE-WRITE text of one field of one goal. Every existing history operation is
whole-FILE: `history.py {list,restore,diff,prune}`. On a store that is one LINE
per aspiration, `diff` therefore emits ~38 MB to recover one field, and getting
the field out of that blob needs a hand parse of a governed store — which the
store-read gate correctly refuses. The missing capability was a READ, not a
write. This is that read.

READ-ONLY BY CONSTRUCTION, and the distinction is load-bearing.
`_history_store.restore()` RETURNS reconstructed bytes and writes nothing; the
destructive behaviour guard-4165 / guard-5651 warn about lives in
`history.py cmd_restore`, which writes those bytes over the LIVE file. This tool
reaches the content through `_read_snapshot_text` — the same read-only path
`cmd_diff` uses — and never calls cmd_restore. Do not "simplify" it onto the
restore CLI.

STDOUT IS THE FIELD VALUE ALONE (guard-315): never the goal, never the
containing aspiration. Everything else goes to stderr, so the stdout is directly
consumable by `goal-field-append.sh --value-file`.

ABSENT, NULL AND EMPTY ARE THREE DIFFERENT ANSWERS and are never collapsed (the
rb-245 class; guard-1753 for the reader half). exit 4 = the record has no such
key; exit 5 = the key is there and its value is JSON null; exit 0 with 0 bytes =
the key is there and its value is the empty string. A caller that cannot tell
those apart will read "nothing was lost" off a snapshot that never carried the
field.

The null branch writes NOTHING to stdout deliberately. `json.dumps(None)` is the
four-character text "null", and stdout here feeds `goal-field-append.sh
--value-file` — so collapsing null into the value lane appends the WORD "null"
into a narrative field, which is a write nobody asked for and which the next
clobber audit would then read as content. (Found by the g-115-8462 fresh-eyes
dispatch on this file's own first commit.)

Non-string values are emitted as PRETTY JSON for human inspection. Do not pipe
that into a `*-append.sh` JSONL writer — multi-line JSON corrupts those
(guard-2179). Capture to a file and hand the file over.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from history import (  # noqa: E402
    resolve_target,
    _find_snapshot_by_name,
    _find_history_snapshots,
    _read_snapshot_text,
)

EXIT_OK = 0
EXIT_ERR = 1
EXIT_NO_RECORD = 3
EXIT_NO_FIELD = 4
EXIT_NULL_FIELD = 5


def find_record(text, record_id):
    """Locate a record by id in a JSONL snapshot.

    Handles BOTH shapes this repo's stores use: a flat store where each line IS
    the record, and world/aspirations.jsonl where each line is an ASPIRATION
    carrying a nested goals[] array. Checking only the top level would return a
    clean not-found for every goal in the store that motivated this tool.
    """
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # a banner or comment line is not a parse failure worth aborting on
        if not isinstance(rec, dict):
            continue
        if rec.get("id") == record_id:
            return rec, "top-level"
        for child in (rec.get("goals") or []):
            if isinstance(child, dict) and child.get("id") == record_id:
                return child, f"nested under {rec.get('id')}"
    return None, None


def main():
    ap = argparse.ArgumentParser(
        description="Read one record's one field from a historical snapshot (read-only).")
    ap.add_argument("file", help="store path, virtual prefixes ok (e.g. world/aspirations.jsonl)")
    ap.add_argument("version", help="snapshot name exactly as history-list.sh prints it")
    ap.add_argument("--goal", "--record", dest="record_id", required=True,
                    help="record id to extract (e.g. g-115-4701)")
    ap.add_argument("--field", required=True, help="field name (e.g. progress_note)")
    args = ap.parse_args()

    target = resolve_target(args.file)
    version_path = _find_snapshot_by_name(target, args.version)
    if version_path is None:
        if not _find_history_snapshots(target):
            print(f"Error: No history for {args.file}", file=sys.stderr)
        else:
            print(f"Error: Version {args.version!r} not found (use history-list.sh "
                  f"for exact names — the audit prints a TRUNCATED name that does not resolve)",
                  file=sys.stderr)
        return EXIT_ERR

    text = _read_snapshot_text(version_path, target)
    rec, where = find_record(text, args.record_id)
    if rec is None:
        print(f"Error: record {args.record_id!r} not in snapshot {args.version}",
              file=sys.stderr)
        return EXIT_NO_RECORD

    if args.field not in rec:
        print(f"Error: record {args.record_id} exists in this snapshot ({where}) but has "
              f"NO field {args.field!r}. This is ABSENT, not empty — the pre-write state "
              f"carried no such field. Keys present: {sorted(rec)}", file=sys.stderr)
        return EXIT_NO_FIELD

    value = rec[args.field]
    if value is None:
        print(f"Error: record {args.record_id} exists in this snapshot ({where}) and HAS "
              f"field {args.field!r}, but its value is NULL. This is neither absent (exit "
              f"4) nor empty (exit 0, 0 bytes) — nothing was written to stdout, because "
              f"json.dumps(None) is the literal text 'null' and this stdout feeds "
              f"goal-field-append.sh --value-file.", file=sys.stderr)
        return EXIT_NULL_FIELD

    out = value if isinstance(value, str) else json.dumps(value, indent=2)
    sys.stdout.write(out)
    # CHARS FIRST, and both reported. `len(str)` is CHARACTERS; `wc -c` on the
    # redirected stdout is BYTES, and they differ by every non-ASCII character
    # (an em-dash is 3 bytes, 1 char) -- measured on the first real recovery,
    # 1296 chars vs 1304 bytes. narrative-clobber-audit.py names its fields
    # `pre_chars`/`post_chars`, so CHARS is the number that reconciles with the
    # audit row you are recovering from; anything comparing to `wc -c` needs the
    # other. Labelling one of them "bytes" makes the two stores silently
    # non-comparable in exactly the direction that hides a partial recovery.
    print(f"[history-field] {args.record_id}.{args.field} @ {args.version}: "
          f"{len(out)} chars / {len(out.encode('utf-8'))} bytes ({where})",
          file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
