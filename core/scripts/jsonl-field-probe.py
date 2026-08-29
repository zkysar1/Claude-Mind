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
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _path_helpers import normalize_msys_path  # noqa: E402


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


# A value written as a bare date, or with a zeroed time component, is the
# precision-fallback tell guard-3265 names: a backfill that had only a DATE
# stamps midnight, so every record in that batch shares one literal instant and
# appears to co-occur with every other. Matches "2026-08-29" and
# "2026-08-29T00:00:00[.000][Z|+00:00]" (space separator too).
_DATE_ONLY_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:[T ]00:00:00(?:\.0+)?(?:Z|[+-]\d{2}:?\d{2})?)?$"
)


def _value_key(value):
    """Canonical, hashable, printable key for any JSON value.

    Every value goes through json.dumps rather than only the unhashable ones.
    Two reasons: dict/list values would raise in a Counter, and — less
    obviously — Python hashes True == 1 and False == 0, so a raw-scalar key
    would silently MERGE a boolean field's counts with an integer field's.
    A frequency probe whose whole purpose is to expose value concentration
    must not create concentration of its own."""
    return json.dumps(value, sort_keys=True, default=str)


def _frequency(records, field, top_n, min_repeat):
    """Value-frequency distribution of `field` across `records`.

    Read-only and total: a record missing the field is counted as missing, not
    skipped silently, so `values_found + records_missing_field` always equals
    `records_scanned` and a reader can tell a concentrated field from a mostly
    absent one (guard-2298: report the population beside the filtered count)."""
    counter = Counter()
    date_only = 0
    values_found = 0
    for rec in records:
        found, value = _get_dotted(rec, field)
        if not found:
            continue
        values_found += 1
        counter[_value_key(value)] += 1
        if isinstance(value, str) and _DATE_ONLY_RE.match(value):
            date_only += 1

    def _share(n):
        return round(n / values_found, 4) if values_found else None

    repeated = [(k, c) for k, c in counter.most_common() if c >= min_repeat]
    top = counter.most_common(top_n)
    return {
        "records_scanned": len(records),
        "values_found": values_found,
        "records_missing_field": len(records) - values_found,
        "distinct_values": len(counter),
        "top_values": [
            {"value": k, "count": c, "share": _share(c)} for k, c in top
        ],
        "top_value_share": _share(top[0][1]) if top else None,
        "min_repeat": min_repeat,
        "repeated_cluster_count": len(repeated),
        "repeated_clusters": [
            {"value": k, "count": c} for k, c in repeated[:top_n]
        ],
        "repeated_clusters_truncated": max(0, len(repeated) - top_n),
        "date_only_values": date_only,
        "date_only_share": _share(date_only),
    }


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
    ap.add_argument("--sample-count", type=int, default=None,
                    help="Number of trailing records to read (default: 1). "
                         "Unchanged in the existence check, where values below "
                         "1 still read exactly 1 record. Under --frequency the "
                         "default is instead ALL records, and 0 or negative "
                         "also means all — a histogram of one record is not a "
                         "histogram.")
    ap.add_argument("--output", default="json", choices=["json", "human"],
                    help="Output format.")
    ap.add_argument("--frequency", action="store_true",
                    help="ADDITIVE mode: also report the VALUE-FREQUENCY "
                         "distribution of --field (top values by count, "
                         "distinct-value count, repeated clusters, and the "
                         "bare-date/midnight share). Satisfies the histogram "
                         "step guard-3265 and guard-2144 prescribe. Off by "
                         "default: without it the output is byte-identical to "
                         "the pre-existing existence check.")
    ap.add_argument("--top-n", type=int, default=10,
                    help="With --frequency: how many top values and repeated "
                         "clusters to list (default: 10). The COUNTS are "
                         "always computed over every record; only the listing "
                         "is truncated, and the surplus is reported.")
    ap.add_argument("--min-repeat", type=int, default=2,
                    help="With --frequency: a value seen at least this many "
                         "times is reported as a repeated cluster (default: 2). "
                         "N records sharing one exact value is a write event, "
                         "not N coincidences.")
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
    # Additive by construction: the key is absent unless --frequency was
    # passed, so every pre-existing caller sees the same dict it always saw
    # (guard-3274 — do not convert a default into a new required shape).
    if args.frequency:
        result["frequency"] = None

    try:
        # normalize_msys_path FIRST () — same defect as its gate
        # sibling audit-schema-gate.py: a caller interpolates "$WORLD_DIR/..."
        # into argv, which is MSYS-flavored on Git Bash, and Windows Python
        # mangles the leading "/" to the current drive. This probe is the
        # EVIDENCE half of rb-245, so an unfixed probe leaves the gate unable
        # to be fed even once the gate itself resolves paths correctly.
        # No-op on POSIX and on already-Windows paths.
        path = Path(normalize_msys_path(args.file))
        if not path.is_file():
            result["probe_error"] = f"file not found: {args.file}"
            return _emit(result, args.output)

        # The existence path keeps `max(1, n)` EXACTLY as it was, for every
        # value of --sample-count including 0 and negatives. The first draft
        # made 0 mean "all records" in both modes, which silently changed what
        # `--sample-count 0` did for existing callers — caught by diffing this
        # script against its own HEAD baseline (guard-3274: do not redefine an
        # existing argument value while adding a mode).
        if args.frequency:
            # New mode, so no prior behaviour to preserve: unset, 0 or negative
            # all mean every record. A histogram of one record is not a histogram.
            n = 0 if args.sample_count is None else args.sample_count
            records = _tail_records(path, n if n > 0 else 0)
        else:
            n = 1 if args.sample_count is None else args.sample_count
            records = _tail_records(path, max(1, n))
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

        if args.frequency:
            result["frequency"] = _frequency(
                records, args.field, max(1, args.top_n), max(1, args.min_repeat)
            )

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
        freq = result.get("frequency")
        if freq:
            print(f"Distinct values: {freq['distinct_values']} "
                  f"over {freq['values_found']} value(s) "
                  f"in {freq['records_scanned']} record(s) "
                  f"({freq['records_missing_field']} missing the field)")
            for row in freq["top_values"]:
                print(f"  {row['count']:>6}  {row['share']}  {row['value']}")
            if freq["repeated_cluster_count"]:
                print(f"Repeated clusters (count >= {freq['min_repeat']}): "
                      f"{freq['repeated_cluster_count']}"
                      + (f" (+{freq['repeated_clusters_truncated']} not listed)"
                         if freq["repeated_clusters_truncated"] else ""))
            if freq["date_only_values"]:
                print(f"Bare-date / midnight values: {freq['date_only_values']} "
                      f"(share {freq['date_only_share']}) "
                      f"— precision-fallback tell, guard-3265")
        if result["probe_error"]:
            print(f"Probe error: {result['probe_error']}")
    # Diagnostic tool: always exit 0. The caller interprets field_present.
    return 0


if __name__ == "__main__":
    sys.exit(main())
