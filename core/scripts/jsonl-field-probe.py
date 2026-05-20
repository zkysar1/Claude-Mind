#!/usr/bin/env python3
"""JSONL Field Probe — diagnostic tool for verifying field presence in JSONL stores.

Reads the last N records of a JSONL file and reports whether a dotted field
path is present, plus a sample value. This is the mechanical floor for the
rb-245 lesson: "Before concluding X has Y=0 across N records, read one record
and verify field Y exists."

This is a DIAGNOSTIC tool (always exits 0). It is NOT a gate. Pairs with
`zero-count-gate.py`: run this first, then feed its output to the gate via
`--file-probed`, `--field-probed`, and `--probe-result`.

Design notes:
- Dotted field paths traverse dicts only (e.g., `utilization.times_active`).
  Array indexing is not supported — JSONL records are records, not nested
  collections, and zero-count audits probe scalar counter fields.
- The probe reads the LAST N records (via a tail-from-end scan) because
  append-only JSONL stores accumulate records over time and the tail reflects
  the current schema; the head may contain legacy records from a pre-migration
  era. Exactly the schema-drift condition rb-245 describes.
- Fail-open on file errors: print a diagnostic message and exit 0. The caller
  (typically the LLM preparing a zero-count claim) sees the error and should
  NOT proceed as if the field were confirmed present.
- No locks taken. This is a read-only diagnostic; a concurrent writer mid-append
  at worst produces a partial last line, which we tolerate by skipping unparseable
  lines.
"""

import argparse
import json
import sys
from pathlib import Path


def _get_dotted(record, dotted):
    """Traverse `record` by dotted path. Return (found: bool, value).

    Only dict traversal is supported. A missing segment at any level returns
    (False, None). An explicit null terminal ALSO returns (False, None) — for
    rb-245, a field that has never been populated is operationally identical to
    a field that isn't in the schema. This keeps this probe aligned with
    audit-schema-gate.py's _get_dotted (same semantic for the same question)."""
    cur = record
    for seg in dotted.split("."):
        if not isinstance(cur, dict):
            return (False, None)
        if seg not in cur:
            return (False, None)
        cur = cur[seg]
    if cur is None:
        return (False, None)
    return (True, cur)


def _tail_records(path, n):
    """Return up to the last `n` parseable JSON records from a JSONL file.

    Reads the whole file — for the store sizes the framework uses (a few MB
    at most), this is simpler and more reliable than byte-seek-from-end
    tailing, which gets fiddly on Windows line endings and multi-byte UTF-8.
    Unparseable lines are skipped silently (they may be partial lines from
    a concurrent writer or legacy malformed entries)."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        raise RuntimeError(f"read failed: {type(e).__name__}: {e}")

    records = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            # Partial / malformed — skip, don't fail. rb-245 is about schema
            # drift, not JSONL corruption; a single bad line doesn't invalidate
            # the probe.
            continue
    return records[-n:] if n > 0 else records


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=(
            "Diagnostic probe: verify a dotted field path exists in the last N "
            "records of a JSONL file. Pair with zero-count-gate.py before "
            "claiming any zero-count / missing-field conclusion (rb-245)."
        )
    )
    ap.add_argument("--file", required=True,
                    help="Path to the JSONL file to probe.")
    ap.add_argument("--field", required=True,
                    help="Dotted field path (e.g., utilization.times_active). "
                         "Dict traversal only; arrays not supported.")
    ap.add_argument("--sample-count", type=int, default=1,
                    help="Number of trailing records to read (default: 1).")
    ap.add_argument("--output", default="json", choices=["json", "human"],
                    help="Output format.")
    args = ap.parse_args(argv)

    result = {
        "file": args.file,
        "field": args.field,
        "records_sampled": 0,
        "field_present": False,
        "sample_value": None,
        "record_index": None,
        "probe_error": None,
    }

    try:
        path = Path(args.file)
        if not path.is_file():
            result["probe_error"] = f"file not found: {args.file}"
            return _emit(result, args.output)

        records = _tail_records(path, max(1, args.sample_count))
        result["records_sampled"] = len(records)
        if not records:
            result["probe_error"] = "no parseable records in file"
            return _emit(result, args.output)

        # Check each sampled record (newest to oldest). A field is "present"
        # if ANY sampled record has it — a single hit refutes the zero-count
        # claim, which is the anti-pattern rb-245 targets.
        for offset, rec in enumerate(reversed(records)):
            found, value = _get_dotted(rec, args.field)
            if found:
                result["field_present"] = True
                result["sample_value"] = value
                # record_index: -1 = last, -2 = second-to-last, etc.
                result["record_index"] = -(offset + 1)
                break

    except Exception as e:
        # Fail-open — diagnostic tools must never block their caller.
        result["probe_error"] = f"{type(e).__name__}: {e}"

    return _emit(result, args.output)


def _emit(result, output_format):
    if output_format == "json":
        print(json.dumps(result, indent=2))
    else:
        print(f"File: {result['file']}")
        print(f"Field: {result['field']}")
        print(f"Records sampled: {result['records_sampled']}")
        print(f"Field present: {result['field_present']}")
        if result["field_present"]:
            print(f"Sample value: {result['sample_value']!r} "
                  f"(at record_index={result['record_index']})")
        if result["probe_error"]:
            print(f"Probe error: {result['probe_error']}")
    # Diagnostic tool: always exit 0. The caller interprets field_present.
    return 0


if __name__ == "__main__":
    sys.exit(main())
