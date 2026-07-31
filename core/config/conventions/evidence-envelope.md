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

## NOT the same as CREATE_BLOCKER's multi-signal evidence

Two different arrays both called "evidence" are consumed by two ADJACENT steps
of the SAME protocol, and conflating them is the single measured failure mode
of this envelope (g-115-3094):

| | this envelope | multi-signal probe evidence |
|---|---|---|
| consumer | `capability-gate.py --evidence` (CREATE_BLOCKER step 2.6) | `gates/blocker_create.py` (CREATE_BLOCKER step 2.55) |
| shape | `{type, id, claim}` | `{tool, command, output, evidence_type}` |
| discriminator key | **`type`** | **`evidence_type`** |
| what it means | WHICH STORE the evidence lives in (`rb`, `tree`, ...) | WHAT KIND OF PROBE produced it (`command_exit`, `http_status`) |
| what it is for | justifying an agent self-approval override | proving 2+ INDEPENDENT signals (`verify-before-assuming.md`) |

They are not a naming inconsistency to be reconciled — they are distinct
concepts that happen to share the word "evidence", so `guard-1565`'s
"change the minority" does NOT apply here. A caller that builds ONE array and
passes it to both gets a confusing asymmetry: step 2.55 accepts it, step 2.6
refuses with `evidence[0].type=''` (empty because the payload said
`evidence_type`).

Measured incident: alpha session `aae8287f`, 2026-07-19 07:57:17-07:58:04 —
one payload refused 3x in 47s, corrected on the 4th attempt. The retries were
blind because the gate's diagnostic was STDOUT-only at the time (fixed under
the same goal; it is now mirrored to stderr).

## Anti-patterns

- Passing a single string instead of a JSON array (gate rejects)
- Using an unregistered `type` (gate rejects with the allow-list)
- Passing CREATE_BLOCKER step-2.55 probe evidence (`{tool, command, output,
  evidence_type}`) to `--evidence` — see the table above; the gate rejects
  with `evidence[0].type=''`
- Passing empty `claim` as a placeholder (gate accepts but the ledger
  record becomes useless for human audit)
- Fabricating `id` values to get past the gate (backpressure + dead-ends
  catch the regression that results; rb-302 and g-115-99 are the test
  cases that motivated this whole pattern)
