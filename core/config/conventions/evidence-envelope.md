# Evidence Envelope

Canonical shape for agent self-approval evidence passed to gates that support
structured overrides (currently `capability-gate.py --evidence`).

## Schema

Evidence is a JSON array where each entry is:

```json
{
  "type": "rb|pipeline|metric|goal|guardrail|tree|experience",
  "id":   "<identifier>",
  "claim": "<one sentence explaining why this evidence is relevant>"
}
```

All three fields are REQUIRED. Arrays with zero entries fail validation.

## Field semantics

- **`type`** — the store the evidence lives in. One of the allow-listed values
  in `capability-gate.py::_VALID_EVIDENCE_TYPES`. Adding a new type is a
  schema change: update that set, update this file, update all gate consumers.
- **`id`** — the store-local identifier (e.g., `rb-302`, `g-115-99`,
  `2026-04-19_decompose-threshold`). Gates validate shape, not existence —
  fabrication is mitigated by backpressure + dead-ends + user ledger review,
  not by schema.
- **`claim`** — free-form one-liner. Used for human review in the approval
  ledger. Keep under 200 characters.

## What gates do with evidence

1. Validate each entry has all three fields and a known `type`.
2. If the gate would otherwise block (e.g., `participants:[user]` would be
   unrouted-to-agent-capable), the presence of ≥1 valid entry flips the
   decision to pass.
3. Append an `"action": "evidence-approval"` record to the approval ledger
   (`world/blocker-gate-overrides.jsonl`) — but **only if the approval
   actually averted a block**. No block averted → no ledger entry. See
   `capability-gate.py::_log_evidence_approval` for the invariant.

## Precedence

When both `--evidence` and `--override-agent-match <string>` are passed,
`--evidence` wins. The free-text override is a fallback for cases where no
structured evidence exists. Structured beats free-text every time — it's
machine-readable and ledger-auditable.

## Design constraints

- **Shape over content**: gates do not dereference `id` to confirm the
  record exists. Adding that check is a deferred hardening step — ship it
  only after observing fabrication in production.
- **Allow-list is load-bearing**: `_VALID_EVIDENCE_TYPES` is the single
  source of truth for legal values. Tests, docs, and examples must match.
- **Additive-only**: the envelope may grow new OPTIONAL fields (e.g.,
  `confidence`, `observed_at`), never rename required ones.

## Consumers

Current:
- `core/scripts/capability-gate.py` (`--evidence` flag)
- `core/scripts/audit-user-to-agent.py` (promotes `[user]` → `[agent, user]`
  when the gate matches and evidence is implicit — the audit itself is the
  evidence)

Future: any gate that wants structured agent self-approval should adopt
this envelope and write to the same ledger. When the second gate migrates,
rename `blocker-gate-overrides.jsonl` → `approvals-ledger.jsonl` (noted in
the MVP plan `curious-sparking-simon.md` as deferred work).

## Anti-patterns

- Passing a single string instead of a JSON array (gate rejects)
- Using an unregistered `type` (gate rejects with the allow-list)
- Passing empty `claim` as a placeholder (gate accepts but the ledger
  record becomes useless for human audit)
- Fabricating `id` values to get past the gate (backpressure + dead-ends
  catch the regression that results; rb-302 and g-115-99 are the test
  cases that motivated this whole pattern)
