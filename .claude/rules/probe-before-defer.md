# Probe Before Defer

## Principle

Before setting `defer_reason` on a goal — freezing the goal for hours on the
premise that an external signal is required — actually attempt the thing.
"Blocked on external signal" is a conclusion, not a default. A defer that
names an agent-provisionable action (commit-and-push, start a service,
reconnect to a remote store, run a sub-command, etc.) is a
capability-routing violation just as surely as `participants: [user]` would
have been.

## Relationship to Other Rules

This rule is the missing chokepoint between two existing rules:

- **`.claude/rules/capability-before-user.md`** gates `participants: [user]`.
  Enforced by `capability-gate.py` at CREATE_BLOCKER and blocker-recheck.
- **`.claude/rules/probe-with-canonical-code-path.md`** gates diagnostic
  probes (use the skill's companion_scripts, not synthetic equivalents).

Together they close most routing leaks — but `defer_reason` is a third door
with the same effect (work doesn't happen, user is implicitly on the hook)
and no gate. This rule plus its enforcement in
`core/scripts/aspirations.py cmd_update_goal` and
`.claude/skills/aspirations-precheck/SKILL.md` Phase 0.5b closes that door.

## Rules

1. **Probe before deferring.** Before calling
   `aspirations-update-goal.sh <goal-id> defer_reason "<narrative>"`:
   a. Read the goal's verification criteria and primary action.
   b. If the action is listed in `world/conventions/capability-routing.md`
      under agent-provisionable, OR named in `agents/<agent>/self.md` "Agent-Provisionable
      Actions", run its canonical companion script with a trivial-success
      argument (e.g., the skill's documented health/probe sub-command, the
      service's `/health` endpoint, or the remote-shell wrapper with a
      trivial `echo ok` payload).
   c. Only if the probe fails AND the failure is not self-recoverable
      (restart, reconnect, retry with backoff) is a defer appropriate.
   d. The defer_reason must then name the specific external signal that
      genuinely cannot be provisioned — not the action the agent routed
      around.
   e. When that signal is a HUMAN gate specifically — an approval click,
      outside counsel, a credential only a person can grant — write it with
      the `human_blocked:` structured prefix (added 2026-06-25, g-115-1646).
      It is a member of `STRUCTURED_DEFER_PREFIXES`, so it bypasses the
      capability-gate and the blocker_ref gate, and it is the ONE member
      that never auto-clears: `goal-selector` exempts it from the 120h
      fall-through, keeping the goal suppressed-from-selector but
      counted-in-blocked so quiescence can fire during a human-gated
      plateau. Visibility comes from the precheck `human_blocked`
      age-escalation, not from re-selection.

      Using the prefix does NOT establish that the gate is real. It says
      "I have concluded this is human-only" in machine-readable form; rule 1
      still has to be satisfied first, and `reclaim-routed-work.md` rule 2
      applies with full force — a structured prefix attests that the routing
      was FORMATTED correctly and is not evidence it is still correct.
      Because this prefix never expires on its own, it is the defer most
      exposed to the RULE axis: a standing grant can retire the reason while
      the condition stays perfectly true, and nothing will re-surface the
      goal on its own. Schema detail: `core/config/conventions/goal-schemas.md`
      § Structured-prefix bypass.

2. **Narrative defers are suspect.** A defer_reason that reads like an
   excuse ("blocked on user-initiated X", "waiting for user to run Y",
   "user must approve Z") almost always fails rule 1. If the narrative
   names an action the agent can perform, execute the action instead of
   deferring.

3. **Defer is expensive.** Default fail-open TTL is 120 hours. A goal
   deferred on a wrong premise freezes real work for ~5 days. The cost of
   a 30-second probe is almost always lower than the cost of a 5-day
   frozen goal.

4. **Re-probe on re-entry.** Every iteration of the aspirations loop,
   Phase 0.5b re-probes deferred goals whose defer_reason names an
   agent-provisionable capability. If the probe succeeds, the defer is
   cleared and the goal returns to pending. Agents must not treat a
   deferred goal as permanently blocked — re-probe catches drift where
   the original defer was wrong or the blocker has resolved.

## Enforcement

Three layers catch this at different moments:

| Moment | Mechanism | What it catches |
|---|---|---|
| Defer-time write | `capability-gate.py` invoked from `cmd_update_goal` when `field == defer_reason` and value is non-null. On match, ALSO files an Unblock goal atomically — see "Auto-conversion" below. | Prevents the wrong defer from being written in the first place AND queues the action the agent should perform instead. Override flag: `--force-defer "<justification>"`. |
| Re-entry sweep | `aspirations-precheck` Phase 0.5b re-probe with canonical script | Catches defers that slipped through or whose premise has expired. Clears them automatically. |
| Notification time | `/notify-user` Step 1.5 gate on approval-request patterns | Prevents the third leak path: agent bypasses the defer write and just emails the user asking for approval. |

## Auto-conversion at Defer Time (Layer D)

Refusing a defer is half a solution — the original action that motivated the
defer still has to be performed by *someone*. When the gate matches an
agent-provisionable capability, it ALSO emits a structured suggestion
(matched capability + action verb + Unblock title/description), and
`cmd_update_goal` files an `Unblock:` goal into `asp-001` BEFORE refusing the
defer. Both writes happen under the same `aspirations.jsonl` lock, so queue
state stays consistent: either both land or neither does. The original goal
stays `pending` (its `defer_reason` was never written), but the action it
depended on is now visibly queued — refusal alone would leave the agent
stuck at the same decision point on the next iteration.

Four invariants govern the auto-conversion:

1. **Atomic with the refusal.** Filing happens inside the same lock as the
   defer write. If filing fails, the failure reason is surfaced in the
   BLOCKED message, but the refusal still stands. The original write is
   never committed either way.
2. **Idempotent across retries.** Three OR-ed dedup strategies prevent a
   defer re-attempt from filing duplicate Unblocks:
   `origin_signal` exact match, title-regex `Unblock:.*for {G}`, and
   description-proximity (verb + goal-id within 80 chars). Cross-queue
   scan covers world + agent. Resolved/skipped Unblocks do NOT block
   re-filing.
3. **Title describes the action, not the match.** The Unblock title uses
   the failure_reason's first action verb (e.g. `"deploy"`), not the
   matched-capability keyword (e.g. `"human"` matched from a skill name
   that happens to contain that token but whose actual action is unrelated).
   The title tells the agent what to DO; `matched_capability` is preserved
   separately for callers needing the gate's signal independent of the
   title. (rb-574)
4. **Single-writer responsibility.** `capability-gate.py` emits the SPEC
   for the Unblock; `cmd_update_goal` does the filing. The gate never
   spawns a subprocess that would deadlock on the same lock. (rb-403)

Override: `--force-defer "<justification>"` bypasses the entire gate (no
refusal, no Unblock filed, defer applies). Use only for genuine false
positives — the override is echoed to stderr for audit and disqualifies
the goal from quiescence eligibility.

Implementation traceability: g-257-02 (gate `--suggest-unblock` flag),
g-257-03 (atomic in-process filing under existing lock), g-257-04 (3-strategy
dedup helper with cross-queue scan), g-257-05 (6-case integration test
matrix + 1-command test-suite aggregator). Knowledge-tree node
`world/knowledge/tree/system/system-constraints-loop/capability-routing-enforcement/`
documents this as **Layer D** of the 4-Layer enforcement pattern (A:
tactical disambiguation, B: automated gate, C: aged-decision recheck,
D: constructive auto-routing at block time).

## Anti-patterns

- Writing `defer_reason: "blocked on user-initiated <service> session"`
  without checking whether the canonical sub-command can start that session
  headlessly.
- Writing `defer_reason: "awaiting user approval to commit and push"` —
  `world/conventions/post-execution.md` Step 2 makes commit-and-push the
  agent's responsibility, not a user-approval step.
- Writing `defer_reason: "<remote store> unreachable"` after one failed raw
  shell command, without invoking the canonical wrapper script (which
  typically carries StrictHostKeyChecking flags, credentials loading, or
  auth headers the raw command misses).
- Re-using a defer narrative across iterations without re-probing — the
  original premise may have been wrong, or the blocker may have resolved.
- Emailing the user to request something the agent can do, as a way to
  avoid writing the defer at all (caught by notify-user Step 1.5 gate).

## Cross-references

- `.claude/rules/capability-before-user.md` — sister rule for
  `participants: [user]`
- `.claude/rules/probe-with-canonical-code-path.md` — canonical-script
  probing discipline
- `core/scripts/capability-gate.py` — shared gate implementation
- `world/conventions/capability-routing.md` — agent-provisionable vs
  human-only catalog
- `world/conventions/post-execution.md` — commit/push autonomy (the
  canonical example of a capability that kept being wrongly deferred)
- rb-246, guard-147 — prior lessons on synthetic-probe false-positive
  blockers
