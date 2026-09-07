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
- Dotted field paths traverse dicts AND descend lists (e.g.,
  `utilization.times_active`, and `goals.id` across every element of a `goals`
  array). Positional indexing is still not supported — you cannot ask for
  `goals.3.id`. The old dict-only traversal is the g-115-9120 list-descent
  defect; see `_descend`.
- BY DEFAULT THE EXISTENCE CHECK READS EVERY RECORD, not the tail. The tail is
  the right window for "what does the CURRENT schema look like" and the wrong
  one for "does this field exist at all", which is the question rb-245 actually
  asks — and the two only diverge in the direction that hurts: a heterogeneous
  tail manufactures a false ABSENT that the downstream gate then consumes as
  evidence FOR the negation. Measured on core/config/verify-learning-checks.jsonl
  (g-115-9120): field `x` present on 10,779 of 10,851 records, yet the old
  default returned field_present:false because the single final record carries
  a different shape. Pass an explicit `--sample-count N` when you deliberately
  want the tail window; `records_in_file` and `sample_is_complete` say which
  population any given answer is about.
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


def _descend(node, segs):
    """Every non-null value `segs` resolves to under `node`, as a list.

    A LIST IS TRANSPARENT, NOT TERMINAL — this is the g-115-9120 fix. The
    previous version traversed dicts only, so the first list on the path
    returned "not found" and NO --sample-count could cure it. Measured on
    world/aspirations.jsonl at full sample: `goals` read present and `goals.id`
    read ABSENT in the same run over the same 24 records, because traversal
    reached the goals ARRAY and stopped instead of descending into its
    elements. That false-absent then fed zero-count-gate as the rb-245
    schema-probe half and PASSED the negation the gate exists to refuse.

    A list mid-path is mapped over with the SAME remaining segments, so
    `goals.id` collects the id of every element. A list as the TERMINAL value
    is returned as itself (unchanged from before) — `--field goals` still
    reports the array."""
    if not segs:
        # An explicit null terminal is ABSENT, not present: for rb-245 a field
        # that was never populated is operationally identical to one that is
        # not in the schema.
        return [] if node is None else [node]
    if isinstance(node, list):
        out = []
        for item in node:
            out.extend(_descend(item, segs))
        return out
    seg = segs[0]
    if not isinstance(node, dict) or seg not in node:
        return []
    return _descend(node[seg], segs[1:])


def _get_dotted(record, dotted):
    """Traverse `record` by dotted path. Return (found: bool, values: list).

    `values` holds EVERY value the path resolves to — one entry for an ordinary
    dict path, N for a path that descends a list of N elements. Callers take
    values[0] as the sample and len(values) as the hit count; an empty list
    means absent.

    DELIBERATE DIVERGENCE FROM audit-schema-gate.py's _get_dotted (g-115-9120).
    This docstring used to claim the two were aligned "same semantic for the
    same question", and the null-terminal semantic IS still identical — but the
    list descent above is NOT, and the sibling is a private copy in that file
    rather than a shared import, so it keeps the old dict-only traversal. The
    divergence is intentional and the roles differ: that one is a GATE (it
    BLOCKS on a missing field, so a false-absent fails toward refusing) while
    this is a DIAGNOSTIC whose false-absent is consumed downstream as evidence
    FOR a negation. Do not "re-align" them by reverting this; the sibling's own
    list-descent limitation is a separate finding against that file."""
    values = _descend(record, dotted.split("."))
    return (bool(values), values)


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
    skipped silently, so `records_with_field + records_missing_field` always
    equals `records_scanned` and a reader can tell a concentrated field from a
    mostly absent one (guard-2298: report the population beside the filtered
    count).

    RECORDS AND VALUES ARE COUNTED SEPARATELY because they are no longer the
    same number (g-115-9120). A path that descends a list yields N values from
    ONE record, so `values_found` can exceed `records_scanned` and the
    conservation invariant has to hang on `records_with_field` instead. On an
    ordinary dict path the two are equal, which is why the distinction was
    invisible while traversal stopped at the first list."""
    counter = Counter()
    date_only = 0
    values_found = 0
    records_with_field = 0
    for rec in records:
        found, values = _get_dotted(rec, field)
        if not found:
            continue
        records_with_field += 1
        for value in values:
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
        "records_with_field": records_with_field,
        "records_missing_field": len(records) - records_with_field,
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
            "Diagnostic probe: verify a dotted field path exists in a JSONL "
            "file. BY DEFAULT IT READS EVERY RECORD, so field_present:false "
            "is a statement about the whole store; pass --sample-count N to "
            "restrict it to the last N. Pair with zero-count-gate.py before "
            "claiming any zero-count / missing-field conclusion (rb-245)."
        )
    )
    ap.add_argument("--file", required=True,
                    help="Path to the JSONL file to probe.")
    ap.add_argument("--field", required=True,
                    help="Dotted field path (e.g., utilization.times_active). "
                         "Traverses dicts and DESCENDS lists, so goals.id "
                         "resolves across every element of a goals array. "
                         "Positional indexing (goals.3.id) is not supported.")
    ap.add_argument("--sample-count", type=int, default=None,
                    help="Number of TRAILING records to read. Default: ALL "
                         "records, in both modes — the existence question is "
                         "about the whole store, and a tail-only default "
                         "manufactures a false ABSENT on any store whose last "
                         "record differs in shape (g-115-9120). An explicit "
                         "value below 1 still reads exactly 1 record in the "
                         "existence check, unchanged, and still means ALL "
                         "under --frequency. Read records_sampled beside "
                         "records_in_file to see which population any answer "
                         "is about.")
    ap.add_argument("--output", default="json", choices=["json", "human"],
                    help="Output format.")
    ap.add_argument("--frequency", action="store_true",
                    help="ADDITIVE mode: also report the VALUE-FREQUENCY "
                         "distribution of --field (top values by count, "
                         "distinct-value count, repeated clusters, and the "
                         "share of bare-date and midnight values). Satisfies "
                         "the histogram "
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
        # The population the answer is about, beside the filtered count
        # (guard-2298). A `field_present: false` from a PARTIAL sample is not
        # the same claim as one from a complete scan, and before 
        # nothing in this output let a reader tell them apart.
        "records_in_file": 0,
        "sample_is_complete": False,
        "field_present": False,
        "sample_value": None,
        # Deliberately NOT named `match_count`. That token already means a
        # POPULATION-wide tally elsewhere in this fleet (npc-composition-sweep
        # step2, capability-gate), and this one is scoped to the single record
        # `record_index` names — printing it beside `records_sampled: 10851`
        # under the fleet's existing reading would be a 1-vs-10851 lie. For a
        # whole-file tally use --frequency (`values_found` / `records_with_field`).
        "match_count_in_record": 0,
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

        # ONE read of the file; the window is a slice of it, so the total
        # population is known without a second pass.
        all_records = _tail_records(path, 0)
        result["records_in_file"] = len(all_records)

        # THE DEFAULT CHANGED (): unset now means ALL records in BOTH
        # modes. It used to mean exactly 1 here, which answered a question
        # nobody asked — "is this field on the LAST record" — while the caller
        # read it as "is this field in the schema" and fed the difference to
        # zero-count-gate as evidence FOR a negation.
        #
        # An EXPLICIT value keeps its old meaning exactly, including 0 and
        # negatives, which still read one record in the existence check
        # (guard-3274: do not redefine an existing argument value). That corner
        # is now the unsafe one rather than the default, and `records_sampled`
        # vs `records_in_file` makes it visible in the output.
        if args.frequency:
            # Unset, 0 or negative all mean every record here — a histogram of
            # one record is not a histogram.
            n = 0 if args.sample_count is None else args.sample_count
            records = all_records if n <= 0 else all_records[-n:]
        else:
            if args.sample_count is None:
                records = all_records
            else:
                records = all_records[-max(1, args.sample_count):]
        result["records_sampled"] = len(records)
        result["sample_is_complete"] = len(records) == len(all_records)
        if not records:
            result["probe_error"] = "no parseable records in file"
            return _emit(result, args.output)

        # Check each sampled record (newest to oldest). A field is "present"
        # if ANY sampled record has it — a single hit refutes the zero-count
        # claim, which is the anti-pattern rb-245 targets.
        for offset, rec in enumerate(reversed(records)):
            found, values = _get_dotted(rec, args.field)
            if found:
                result["field_present"] = True
                result["sample_value"] = values[0]
                # >1 when the path descended a list: the number of elements in
                # THIS record that carry it, so a partial match is visible
                # rather than collapsing to a bare boolean. Scoped to this one
                # record BY CONSTRUCTION — the loop breaks here, so records
                # older than `record_index` are never examined.
                result["match_count_in_record"] = len(values)
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
            if result["match_count_in_record"] > 1:
                # A list-descent partial is invisible in human mode otherwise —
                # the sample value alone reads as the single value of the field.
                print(f"Matches in that record: "
                      f"{result['match_count_in_record']} (path descends a list)")
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
