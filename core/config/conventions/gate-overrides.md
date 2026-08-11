# Gate Overrides

How to bypass gates when you have a legitimate reason. Single source of truth
for the override discipline added in Phase 4 of the gate audit/retirement plan.

## The Problem It Solves

Filing a single goal can hit 4+ gates (origin-signal, duplication, capability,
operator-offload, blocker-create depending on path). Each historically had its
own bypass flag name (`--override-signal`, `--override-duplication`,
`--override-agent-match`, `--override-blocker-gate`, `--override-offload`,
plus the bare `--override` on standalone gates).
Remembering five different flag names per goal is the cognitive load tax the
agent's Session-56 audit named explicitly.

## Two Forms

### `--override-all "<justification>"` — bulk bypass

The agent-friendly path. Supplied ONCE on the orchestrator script (aspirations.py
add / add-goal / cmd_add, create-blocker.py main). The orchestrator fans the
SAME justification into every per-gate slot that wasn't individually set.

Use when one rationale applies cleanly to ALL gates the operation invokes:

```bash
echo '{...goal json...}' | aspirations-add-goal.sh asp-115 \
    --override-all "splitting decomposed parent into 3 children — duplication and origin-signal both apply"
```

### Per-gate flags — fine-grained bypass

Still supported, still the right call when reasons differ per gate. Per-gate
flags WIN over `--override-all` — supplying both means the per-gate one is
applied for that specific gate and the bulk justification fills the remaining
unset slots.

```bash
echo '{...}' | aspirations-add-goal.sh asp-115 \
    --override-duplication "intentional overlap with g-115-22 — different scope" \
    --override-signal "decomposition-parent (no other origin signal applicable)"
```

## Decision Rule

1. **One reason for all gates** → `--override-all`
2. **Different reasons per gate** → per-gate flags (use `--override-all`
   alongside ONLY for slots not covered by your specific reasons)
3. **Single-gate scripts called directly** (positive-state-gate.py,
   verify-before-assuming-gate.py, exhaustive-search-gate.py, etc.) → use
   that gate's own `--override` flag; `--override-all` does not apply because
   there's only one gate to bypass.

## Auditing

Every `--override-all` use writes ONE record to
`world/override-bypass-ledger.jsonl` capturing:
- `override_token` — short hash of the justification (cross-references the
  same justification's appearance as `decision=override` records in
  `meta/gate-firings.jsonl`)
- `slots_filled` — exact list of per-gate slots the bulk justification
  populated (the BLAST RADIUS — how many gates one rationale silenced)
- `agent`, `session_id` — attribution
- `context.caller`, `context.goal_id`, `context.asp_id` — pivot keys

Per-gate overrides continue to land in their gate-specific ledger files
(`world/goal-duplication-overrides.jsonl`, etc.) AND in `gate-firings.jsonl`
as `decision=override`. The bulk-override ledger is additive — it correlates
the per-gate firings under one token, not replaces them.

**Not every ledger record comes from a CLI flag.** Daemon-side gates have no
argv to carry a flag, so they are bypassed by an ENV VAR and write to the same
`override-bypass-ledger.jsonl` under their own `gate` value, with `slots_filled`
absent (nothing was slot-filled — there is exactly one gate). Reading a record
with no `slots_filled` as malformed is the mistake to avoid; check `gate` first.
Current members: `claim-sid-gate` (`MIND_CLAIM_ALLOW_NO_SID`, refuses sid-less
world-goal claims — g-306-132-b) and `capability-route-gate` (cross-lane claim
justification, passed as the `cross_lane` query param rather than an env var).
A daemon gate SHOULD fail open on its own dependency errors and log a
distinguishable sentinel justification when it does, so a rash of
gate-dependency failures is visible in this ledger rather than silent
(guard-142).

## Anti-patterns

- **Vague bulk justification**: `--override-all "I know what I'm doing"`
  silences 2-4 gates with zero useful audit signal. The justification should
  name WHY each gate's concern doesn't apply, even when the same wording
  covers all of them.
- **Bulk-overriding a gate that should be tightened**: if `--override-all`
  is being used routinely to bypass a specific gate, that's a signal the
  gate is FP-dominant and needs its trigger pattern tightened. The Phase 5
  retirement evaluator surfaces these from the firing log.
- **Bulk override on infrastructure-blocking failures**: when a gate refuses
  because evidence is genuinely missing (e.g., `blocker-create-gate` saying
  "no canonical_probe ran"), `--override-all` writes a justification but
  the underlying problem (untested probe) doesn't go away. Run the probe.

## Cross-references

- `core/scripts/_override_helpers.py` — the helper (`apply_override_all`,
  `audit_bulk_override`); single source of truth for both behaviors
- `core/config/gates.yaml` → `override_flag` field per gate; null = gate
  intentionally cannot be bypassed (notify-user-approval-gate, cargo-cult,
  read-intent, prose-verification, agent-action, etc.)
- `meta/gate-firings.jsonl` — per-gate `decision=override` entries
- `world/override-bypass-ledger.jsonl` — bulk-override blast radius records
