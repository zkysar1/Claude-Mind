---
created: "2026-05-07"
last_updated: "2026-05-07"
last_update_trigger: "g-275-03 — rb-707 verify-real cluster Candidate 2: codify the bidirectional set-difference rule for paired-data audits"
source: "agent"
---

# Paired-Data Audits Convention

## Principle

When auditing two stores expected to be 1-to-1 (e.g., `experience.jsonl`
records ↔ `experience/<id>.md` files; `journal/index.jsonl` ↔ journal entries;
`pipeline.jsonl` ↔ archived pipeline files), the **net delta**
`len(A) - len(B)` is a surrogate for "in sync." The system actually requires
**bidirectional set difference**: both `(A − B)` AND `(B − A)`.

`len(A) - len(B) ≈ 0` can hide arbitrarily large bilateral leakage. The
canonical rb-707 incident found 54 actual mismatches behind `net = -2` — a
27× mask factor.

## Rule

Any audit comparing two stores expected to be 1-to-1 MUST emit BOTH
`(A − B)` AND `(B − A)` as **separate** counts (or lists). Net delta
alone is forbidden as the "in sync" signal.

The canonical helper is `core/scripts/audit_helpers/_paired_diff.py`:

```python
from audit_helpers._paired_diff import paired_diff

result = paired_diff(set_a, set_b)
# result is a dict:
#   {
#     "a_minus_b":     [<elements in A not in B>],
#     "b_minus_a":     [<elements in B not in A>],
#     "a_minus_b_count": int,
#     "b_minus_a_count": int,
#     "in_sync":       bool,  # True iff BOTH counts are 0
#     "total_mismatches": int,  # a_minus_b_count + b_minus_a_count
#   }
```

A wrapper script (`audit-experience.py`, `audit-journal.py`,
`pipeline-archive-audit.py`, etc.) that consumes this helper trivially
satisfies the rule. The helper is the surface that documents bidirectional
intent.

## Override Semantics

This convention has **no programmatic override**. A genuinely one-way
audit (e.g., "every `.md` MUST have a jsonl record" but jsonl-only is
permitted) is permitted, but the script MUST cite that asymmetry in its
docstring AND state which direction it intentionally measures.

Example acceptable docstring:

```python
"""audit-orphan-md — find .md files missing a jsonl record.

This audit is INTENTIONALLY ONE-WAY: it measures (md_stems − jsonl_ids)
because the inverse direction (jsonl with no .md) is handled by
experience-reconcile.py's missing_md branch. The asymmetry is by design;
do NOT switch to paired_diff.
"""
```

Without such documentation, an audit that measures only `len(A) - len(B)`
or only one set difference fails this convention.

## Verify-on-Insert

When this convention ships, all existing `audit-*.py` and similar paired-data
scripts MUST be grep-checked:

```bash
grep -rn "len(.*) - len(" core/scripts/audit-*.py
```

Each match resolves one of three ways:
1. The match is the rb-707 anti-pattern → refactor to use
   `audit_helpers._paired_diff.paired_diff()`.
2. The match is a within-list count subtraction (NOT paired audit) →
   leave as-is; document the non-paired purpose in a nearby comment.
3. The audit is intentionally one-way → leave as-is; ensure the docstring
   states the asymmetry per the override section above.

Initial grep at convention-ship time (g-275-03 retrofit, 2026-05-07): the
core/scripts/ codebase contains **zero rb-707 violations** in
`audit-*.py`. One unrelated match in `blocker-create-gate.py:294`
(`silent_count = len(evidence) - len(valid_entries)` — within-list filter,
not paired audit). One existing paired audit (`experience-reconcile.py`)
already uses correct set difference for the orphan_md / missing_md pair.

## Cross-References

- `core/scripts/audit_helpers/_paired_diff.py` — canonical helper.
- `core/scripts/experience-reconcile.py` — pre-existing paired-data audit
  that already follows the rule (uses `md_stems - set(jsonl_ids.keys())`
  for orphan_md and the inverse for missing_md).
- `world/reasoning-bank.jsonl` rb-707 (the original incident — net delta
  hid 27× more breakage).
- verify-real-cluster-catalog Candidate 2 (catalog entry for this
  structural-guard ship; git-archived).
- Sibling structural guards: deploy-state-verification (rb-711),
  loop-state-merge-gate (rb-715), pre-completion uncommitted-work-gate
  (commit `7ed1414`).

## Evolution

When new paired-data stores are added, edit (not rewrite) this file.
Record changes in `world/conventions/convention-changes.jsonl` per the
standard convention mutation audit trail.
