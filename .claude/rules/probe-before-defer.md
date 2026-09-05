---
description: "Before setting defer_reason run the canonical companion script; defer only on a non-recoverable failure, naming the real external signal."
---

# Probe Before Defer

## Principle

Before setting `defer_reason` on a goal — freezing the goal for hours on the
premise that an external signal is required — actually attempt the thing.
"Blocked on external signal" is a conclusion, not a default. A defer that
names an agent-provisionable action (commit-and-push, start a service,
reconnect to a remote store, run a sub-command, etc.) is a
capability-routing violation just as surely as `participants: [user]` would
have been.

Enforcement mechanics, the Layer-D auto-conversion invariants, and the
incidents live in `core/config/conventions/defer-routing.md`
(`load-conventions.sh defer-routing`). This file keeps the imperatives.

## Relationship to Other Rules

`.claude/rules/capability-before-user.md` gates `participants: [user]`
(via `capability-gate.py` at CREATE_BLOCKER and blocker-recheck);
`.claude/rules/probe-with-canonical-code-path.md` gates diagnostic probes.
`defer_reason` is a third door with the same effect (work doesn't happen, the
user is implicitly on the hook); this rule plus its enforcement in
`core/scripts/aspirations.py cmd_update_goal` and `aspirations-precheck`
Phase 0.5b closes it.

## Rules

1. **Probe before deferring.** Before calling
   `aspirations-update-goal.sh <goal-id> defer_reason "<narrative>"`:
   a. Read the goal's verification criteria and primary action.
   b. If the action is listed in `world/conventions/capability-routing.md`
      under agent-provisionable, OR named in `agents/<agent>/self.md`
      "Agent-Provisionable Actions", run its canonical companion script with
      a trivial-success argument (health/probe sub-command, `/health`
      endpoint, remote-shell wrapper with `echo ok`).
   c. Only if the probe fails AND the failure is not self-recoverable
      (restart, reconnect, retry with backoff) is a defer appropriate.
   d. The defer_reason must then name the specific external signal that
      genuinely cannot be provisioned — not the action the agent routed
      around.
   e. When that signal is a HUMAN gate specifically (an approval click,
      outside counsel, a credential only a person can grant), write it with
      the `human_blocked:` structured prefix — a `STRUCTURED_DEFER_PREFIXES`
      member that never auto-clears (suppressed from the selector, counted as
      blocked, surfaced by the precheck age-escalation). The prefix attests
      that the routing was FORMATTED correctly, not that it is still correct:
      rule 1 must be satisfied first, and `reclaim-routed-work.md` rule 2
      applies with full force. Never use it for a time or measurement
      window — that is `precondition_unmet:`, which re-probes.

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
   cleared and the goal returns to pending. Never treat a deferred goal as
   permanently blocked.

5. **Expect two independent refusals at write time, and never launder the
   evidence to pass them.** `capability-gate.py` keyword-matches the
   `defer_reason` TEXT and, on an agent-provisionable match, refuses the
   defer AND files an `Unblock:` goal atomically (Layer D auto-conversion —
   see the convention). A defer that honours rule 1 CITES its probe output,
   which is exactly where service/tool/command names live, so the
   best-probed defers are the ones most likely to trip it (guard-3882). Read
   the gate's own sub-analysis: when `unblock_suppressed_reason` names a
   bare-token noun match, `--force-defer "<justification>"` is the sanctioned
   response — never delete the probe citation to get past the gate. Then
   expect `blocker_ref_required`, which `--force-defer` does NOT satisfy; only
   a `STRUCTURED_DEFER_PREFIXES` member bypasses it (`gates/defer_classifier.py`
   is the SSOT).

## Enforcement (summary — mechanism in the convention)

| Moment | Mechanism |
|---|---|
| Defer-time write | `capability-gate.py` from `cmd_update_goal` when `field == defer_reason`; refuses + auto-files the Unblock (Layer D). Override: `--force-defer` |
| Re-entry sweep | `aspirations-precheck` Phase 0.5b re-probe with the canonical script; clears stale defers |
| Notification time | `/notify-user` Step 1.5 gate on approval-request patterns |
| Chat reply (handing the user a command block) | **NONE — honor-system by construction**; a post-hoc transcript detective is the only possible mechanism. See `capability-before-user.md` § "The Fourth Surface". Do not file work to "add the missing gate". |

## Anti-patterns

- Writing `defer_reason: "blocked on user-initiated <service> session"`
  without checking whether the canonical sub-command can start it headlessly.
- Writing `defer_reason: "awaiting user approval to commit and push"` —
  `world/conventions/post-execution.md` Step 2 makes commit-and-push the
  agent's responsibility.
- Writing `defer_reason: "<remote store> unreachable"` after one failed raw
  shell command, without invoking the canonical wrapper script.
- Re-using a defer narrative across iterations without re-probing.
- Emailing the user to request something the agent can do, to avoid writing
  the defer at all (caught by the notify-user Step 1.5 gate).
- Deleting the probe citation from a defer_reason to get past the gate.

## Cross-references

- `core/config/conventions/defer-routing.md` — enforcement table (incl. why
  the chat lane cannot be gated), Layer D auto-conversion invariants and
  override, the guard-3882 gradient, `human_blocked:` caveats, traceability
  (g-257-02..05), the `capability-routing-enforcement` tree node
- `.claude/rules/capability-before-user.md` — sister rule for
  `participants: [user]`; `.claude/rules/probe-with-canonical-code-path.md`
  — canonical-script probing discipline
- `core/scripts/capability-gate.py` — shared gate implementation;
  `world/conventions/capability-routing.md` — agent-provisionable vs
  human-only catalog; `world/conventions/post-execution.md` — commit/push
  autonomy
- rb-246, guard-147 — synthetic-probe false-positive blockers
