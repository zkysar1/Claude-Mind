# Audit-Before-Concluding Gate

## Rule

Before aggregating counts or drawing conclusions from a JSONL store, confirm that
every field you are about to aggregate actually exists (with a non-null value) in
at least one sampled record. If a named field is absent across N sampled records,
the audit is almost certainly wrong — either the field name is stale, the field
is nested under a different path, or the store hasn't been populated yet.

Gate: `core/scripts/audit-schema-gate.py` (wrapper: `audit-schema-gate.sh`).

## Rationale

This gate exists because of rb-245 in the reasoning bank — a lesson captured after
session-47 iter 51/52. The agent audited `times_triggered` (a legacy, unwritten
top-level field) across 114 guardrails and concluded "98% zero utilization." The
real counter was `utilization.times_active` — nested, actively written, and
populated on 113/114 records. One sampled record at audit-start would have caught
the mistake; instead, it shipped a wrong audit that needed a full retraction the
next iteration.

The gate's core feature is **dotted-path support**: it walks `utilization.times_active`
through nested dicts and treats "absent" and "always-null" as equivalent — catching
both the "wrong spelling" and "wrong nesting level" variants of this bug.

## When to Call

Before any audit/aggregation loop that counts records matching a field condition.
Typical call sites:
- Skills that do `for record in records: if record[field] ...` aggregation
- Reflection skills computing utilization statistics
- Verification skills cross-referencing stored counters
- Any one-off investigation goal that runs `aspirations-query.sh` or
  `pipeline-read.sh` followed by counts

Do NOT call for:
- Single-record reads (the gate is about aggregation correctness, not reads)
- Writes — this is a pre-read gate
- Retrieval for execution context (retrieve.sh has its own correctness model)

## Contract

```
audit-schema-gate.py
  --jsonl-path <file>      required
  --field-names <csv>      required (dotted paths allowed)
  --sample-size <int>      default 3
  --override "<text>"      fail-open with stderr audit line

Exit codes:
  0: pass  (every named field present in ≥1 sample; OR fail-open path; OR override)
  1: block (at least one field absent from ALL samples)

Output: JSON with jsonl_path, field_names, sample_size, records_sampled,
        fields_found, fields_missing, would_block, reason.
```

## Example (blocks the iter-51 mistake)

```bash
$ bash core/scripts/audit-schema-gate.sh \
    --jsonl-path world/guardrails.jsonl \
    --field-names "times_triggered,utilization.times_active"
{"...", "fields_missing": ["times_triggered"], "would_block": true,
 "reason": "fields absent or always-null across 3 sampled record(s): times_triggered..."}
$ echo $?
1
```

## Fail-Open Cases

These return exit 0 with a stderr warning (the gate does not block):
- `--jsonl-path` points to a missing file (probably a dry-run or first-session state)
- The file is empty or all lines are JSON-unparseable
- `--override "<reason>"` is supplied (audit line echoed to stderr)

The design principle: false-positive blocks from gate bugs are worse than the bug
the gate prevents. When evidence is insufficient to conclude either way, pass.

## Override Guidance

Override is appropriate when you are intentionally auditing a legacy field to
confirm its unused status — for example, "audit times_triggered to validate my
deprecation plan." In that case:

```bash
bash core/scripts/audit-schema-gate.sh \
  --jsonl-path world/guardrails.jsonl \
  --field-names "times_triggered" \
  --override "Intentional deprecation audit — confirming field is unwritten across all records"
```

The override justification is echoed to stderr and picked up in audit logs.
