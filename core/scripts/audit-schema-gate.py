#!/usr/bin/env python3
"""Audit-Schema Gate — verify pseudocode field names exist in actual JSONL records
before an agent forms conclusions from aggregation.

Rule source: reasoning-bank rb-245 ("Verify pseudocode field names against actual
JSONL schema before auditing. Read ONE record first and confirm the field exists
and is being written."). Captured after session-47 iter 51/52 reversal: the agent
audited `times_triggered` (top-level, legacy, unwritten) across 114 guardrails and
concluded "98% zero utilization." The real counter was `utilization.times_active`
(nested, actively written). One record sampled at audit-start would have caught it.

Called inline from audit/aggregation skill pseudocode BEFORE the count-over-records
loop. Supports dotted-path field names (e.g., `utilization.times_active`) to catch
the exact class of bug that motivated the rule.

Contract
  --jsonl-path <file>      required
  --field-names <csv>      required (comma-separated; dotted paths allowed)
  --sample-size <int>      default 3; how many records to sample from the top of the file
  --override "<text>"      fail-open with audit trail to stderr

Exit codes
  0: pass — every named field was observed (with a non-null value) in at least one
     of the sampled records. OR: fail-open paths (missing file, empty file, JSON
     parse errors, override). OR: nothing to block (`trigger_matched` is false).
  1: block — at least one named field was ABSENT (or null in all) sampled records.
     The caller should halt aggregation, revisit the field spelling, or supply an
     override if the absence is expected (e.g., legacy-field audit is intentional).

Output (stdout, JSON)
  jsonl_path, field_names, sample_size, records_sampled,
  fields_found (list of names present in ≥1 record),
  fields_missing (list of names absent or always-null),
  would_block (bool), reason (short string)
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _path_helpers import normalize_msys_path  # noqa: E402


# --- Dotted-path field lookup --------------------------------------------------

def _get_dotted(record, dotted_name):
    """Walk `dotted_name` (e.g., 'utilization.times_active') through nested dicts.

    Returns a 2-tuple (found, value). `found` is True iff every intermediate key
    exists AND the final value is not None. This is the definition that catches
    the iter-51 bug: a field that exists at the path but is universally null
    counts as absent.

    DO NOT CHANGE the `cur is None` → False semantics. An always-null field must
    register as "missing" for this gate to block the rb-245 failure mode (key
    present but never written). Treating null as "found" defeats the purpose.
    """
    cur = record
    for part in dotted_name.split("."):
        if not isinstance(cur, dict):
            return False, None
        if part not in cur:
            return False, None
        cur = cur[part]
    if cur is None:
        return False, None
    return True, cur


def _sample_records(jsonl_path, sample_size):
    """Read up to sample_size JSONL records from the top of the file.

    Returns (records, warnings). Unparseable lines are counted into warnings
    but do not abort — we want to sample whatever structured records we can
    reach.
    """
    records = []
    warnings = []
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for lineno, raw in enumerate(f, start=1):
                if len(records) >= sample_size:
                    break
                line = raw.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    warnings.append(f"line {lineno}: {exc}")
    except OSError as exc:
        warnings.append(f"read failed: {exc}")
    return records, warnings


# --- Gate entry -----------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Audit-schema gate: verify named fields exist in JSONL records before aggregation.",
    )
    parser.add_argument("--jsonl-path", required=True,
                        help="Path to the JSONL file whose records will be aggregated.")
    parser.add_argument("--field-names", required=True,
                        help="Comma-separated field names to check. Dotted paths allowed (e.g., 'utilization.times_active').")
    parser.add_argument("--sample-size", type=int, default=3,
                        help="Number of records to sample from the top of the file (default 3).")
    parser.add_argument("--override",
                        help="Fail-open justification; logged to stderr for audit trail.")
    args = parser.parse_args()

    result = {
        "jsonl_path": args.jsonl_path,
        "field_names": [],
        "sample_size": args.sample_size,
        "records_sampled": 0,
        "fields_found": [],
        "fields_missing": [],
        "would_block": False,
        "reason": "",
    }

    # Parse field names
    field_names = [f.strip() for f in args.field_names.split(",") if f.strip()]
    result["field_names"] = field_names
    if not field_names:
        result["reason"] = "no field names provided — nothing to check (pass)"
        print(json.dumps(result))
        sys.exit(0)

    # Override: emit audit line, pass
    if args.override:
        print(f"[audit-schema-gate] override applied: {args.override}", file=sys.stderr)
        result["reason"] = f"override: {args.override}"
        print(json.dumps(result))
        sys.exit(0)

    # File presence (fail-open if missing).
    # normalize_msys_path FIRST (): callers interpolate "$WORLD_DIR/..."
    # into argv, and on Git Bash that is MSYS-flavored (/c/...). Windows Python
    # reads the leading "/" as absolute-on-current-drive, so is_file() returned
    # False for a file that plainly existed and this gate fail-opened at EVERY
    # $WORLD_DIR call site — verifying nothing, silently, for the two rb-245
    # audits it exists to protect. No-op on POSIX and on already-Windows paths.
    jsonl_path = Path(normalize_msys_path(args.jsonl_path))
    if not jsonl_path.is_file():
        result["reason"] = "file not found — fail-open (gate does not block on missing file)"
        print(f"[audit-schema-gate] fail-open: {jsonl_path} not found", file=sys.stderr)
        print(json.dumps(result))
        sys.exit(0)

    records, warnings = _sample_records(jsonl_path, args.sample_size)
    result["records_sampled"] = len(records)
    for w in warnings:
        print(f"[audit-schema-gate] warning: {w}", file=sys.stderr)

    if not records:
        result["reason"] = "no records sampled (empty or all-unparseable file) — fail-open"
        print(json.dumps(result))
        sys.exit(0)

    # For each field, check presence across all sampled records
    found = []
    missing = []
    for name in field_names:
        present_in_any = False
        for rec in records:
            ok, _val = _get_dotted(rec, name)
            if ok:
                present_in_any = True
                break
        if present_in_any:
            found.append(name)
        else:
            missing.append(name)

    result["fields_found"] = found
    result["fields_missing"] = missing

    if missing:
        result["would_block"] = True
        result["reason"] = (
            f"fields absent or always-null across {len(records)} sampled record(s): "
            f"{', '.join(missing)}. Before aggregating, verify the field name against "
            f"the actual schema in {jsonl_path.name}. See rb-245."
        )
        print(json.dumps(result))
        sys.exit(1)

    result["reason"] = f"all {len(field_names)} field(s) observed in ≥1 of {len(records)} sampled record(s)"
    print(json.dumps(result))
    sys.exit(0)


if __name__ == "__main__":
    # Fail-open crash handler (). main() promises fail-open in its
    # docstring, but an OSError escaping path resolution (Path.is_file() and
    # Path.exists() both re-raise non-ignored OSError) exited NON-zero -- i.e.
    # exit 1, the SAME code as an intentional block. A caller told to halt
    # aggregation could not tell a real missing-field verdict from a crash.
    # This mirrors the sibling jsonl-field-probe.py, whose handler states the
    # shared contract verbatim: "diagnostic tools must never block their
    # caller." Both now record the exception in the emitted JSON and exit 0.
    #
    # SystemExit derives from BaseException, NOT Exception, so the deliberate
    # sys.exit(1) block verdict passes through untouched -- only an unhandled
    # crash is converted. guard-3803 (a fail-open handler also covers its own
    # deny-message construction, silently turning a refusal into an allow) was
    # considered: the only code this shadows past the verdict is the refusal
    # f-string, which interpolates already-computed locals and does no I/O.
    # The real crash surface is path resolution, which runs before any verdict.
    try:
        main()
    except Exception as exc:
        print(json.dumps({
            "would_block": False,
            "reason": "gate crashed; failing open so a crash is never mistaken "
                      "for a missing-field block",
            "gate_error": f"{type(exc).__name__}: {exc}",
        }))
        sys.exit(0)
