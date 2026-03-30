# Skill Failure Return Contract

Skills detecting infrastructure unavailability MUST return a structured marker:
- `INFRASTRUCTURE_UNAVAILABLE` — required service/server not running
- `RESOURCE_BLOCKED` — required resource inaccessible
- `SKIP_REASON: {explanation}`

Phase 4.0 handles structured SKIP markers (INFRASTRUCTURE_UNAVAILABLE, RESOURCE_BLOCKED)
via the fast-path CREATE_BLOCKER protocol — no email check, no inline fix attempt, immediate
cascade block + unblocking goal. Phase 4.1 handles goals that ran and failed — full diagnosis,
inline fix attempt, then CREATE_BLOCKER if unfixable. Phase 0.5a catches issues between
iterations via pre-selection guardrail sweep. Local/tooling errors (script validation, file not found, build failures) skip Phase 4.1 entirely.

---

# Error Response Protocol

## Guardrail-Driven Enforcement

The specific checks (what to run, when, how) are learned behaviors stored
in `world/guardrails.jsonl`. The core skill (Phase 4.1) consults guardrails
after every infrastructure goal — it does not hardcode which checks to run.
The guardrails tell it.

This separation means the agent can learn NEW post-execution checks without
modifying the cognitive core. The core provides the mechanism ("consult what
you've learned"); the mind provides the content ("check error alerts").

## The Root Cause Principle

What the agent sees is often the LAST link in a cascade chain. A service "hung
initializing" because a dependency failed, which happened because a configuration
broke during startup. The agent sees symptom #3 and must look for causes #1 and
#2 in the error alerts.

## Two Enforcement Points

### 1. Phase 4.1 (Per-Goal, in aspirations-execute)
- Consults guardrails after EVERY infrastructure goal (success or failure)
- Guardrails prescribe specific checks (via `action_hint` commands)
- If checks reveal issues, the full error response protocol fires

### 2. Phase 0.5a (Per-Iteration, in aspirations loop)
- Consults guardrails before EVERY goal selection
- Catches cascade errors between iterations and external failures
- Unblocking goals created via CREATE_BLOCKER are HIGH priority, ensuring errors are addressed first

## Rules

1. **Check error alerts after EVERY infrastructure interaction** — not just
   failures. "The server ran fine" is an assumption. Error alerts are evidence.
2. **Try fix before blocking** — the agent attempts one inline fix before creating
   a blocker. Not a retry loop — one attempt, then either fixed or blocked.
3. **Look for cascade patterns** — sort by timestamp (oldest first). The earliest
   alert is most likely the root cause.
4. **Alert the user** — about the blocker + unblocking goal + cascade chain.
5. **Unblocking goals are HIGH priority** — goal-selector ranks them above routine work.
6. **Dedup** — before creating any goal (unblocking, investigation, or idea), check
   for existing similar goals. Update existing rather than duplicating.
7. **Never defer without checking** — check error alerts before deferring. Try to
   fix inline before creating a blocker.
8. **Never trust superficial success** — the ONLY evidence is: error alerts were checked and none were found.
9. **Non-error blockers are first-class** — missing resources, preflight SKIPs,
   user-action dependencies all create blockers via CREATE_BLOCKER protocol.
10. **The unblocking goal IS the resolution path** — blocker and goal are coupled.
    When the goal completes, Phase 0.5b clears the blocker.
11. **Ideas and investigations are encouraged** — create investigation/idea goals
    anytime. These are separate from unblocking goals. A single event can spawn all three.
12. **Framework bugs go to the Framework Improvements aspiration** — when encountering
    bugs in read-only framework files (`core/`, `.claude/`), add a goal with
    `participants: ["user"]` to the "Framework Improvements" world aspiration (asp-093)
    via `aspirations-add-goal.sh`. Include: file path, line numbers, observed behavior,
    expected behavior, and any workaround in use. Also create a reasoning bank entry
    for the workaround (existing practice). Do NOT silently work around framework bugs
    without logging them.

## Protocol Steps

**Phase 4.0 (fast-path)**: Skill returns INFRASTRUCTURE_UNAVAILABLE or RESOURCE_BLOCKED →
invoke CREATE_BLOCKER directly (no email check, no inline fix — problem is at preflight).

**Phase 4.1 (full diagnosis)**: Goal ran and failed or guardrail found issues →
1. **SEEK ERROR ALERTS** — via `error_check` config in `<agent>/infra-health.yaml`
2. **CASCADE DETECTION** — sort alerts by timestamp ascending, earliest = root cause
3. **DETERMINE SEVERITY** — confirmed_infrastructure, explicit_failure, or soft_failure
4. **TRY FIX INLINE** — search knowledge tree, reasoning bank, experience for solutions. One attempt.
5. **If can't fix → CREATE_BLOCKER** — blocker entry + unblocking goal + cascade + alert (atomic protocol)

**Phase 0.5a (pre-selection)**: Guardrail sweep before each goal selection → if issues found, CREATE_BLOCKER.

**CREATE_BLOCKER protocol** (single source of truth, invoked by 4.0, 4.1e, and 0.5a):
Dedup existing blocker → create unblocking goal (HIGH) → create blocker entry → cascade-block same-skill goals → alert user → write working memory → journal.

## Reference
Full protocol: Phase 4.0 + Phase 4.1 + CREATE_BLOCKER in `.claude/skills/aspirations-execute/SKILL.md`
Broad sweep: Phase 0.5a + blocker resolution: Phase 0.5b in `.claude/skills/aspirations/SKILL.md`
Script: error check script configured in `<agent>/infra-health.yaml` `error_check` section

---

# Error Alerts Configuration

Error alert checking is configured in `<agent>/infra-health.yaml` under `error_check`:
```yaml
error_check:
  script: <path to error check script>
  check_args: "<args for listing recent alerts>"
  read_args: "<args for reading individual alerts>"
```

Fresh agents start with `error_check: null` (no alert checking configured).
Domain-specific scripts (e.g., email-based, webhook-based) are placed in `<agent>/scripts/`.

---

# Infrastructure Health Tracking

Infrastructure health state is tracked in `<agent>/infra-health.yaml`.
Updated automatically by `core/scripts/infra-health.sh` on every probe.

| Script | Purpose | Stdin |
|--------|---------|-------|
| `infra-health.sh check <component>` | Probe component health (auto-records result) | — |
| `infra-health.sh check-all` | Probe all components | — |
| `infra-health.sh status` | Current state from <agent>/infra-health.yaml | — |
| `infra-health.sh stale [--hours N]` | Components not checked within N hours (default: 2) | — |

Components are domain-specific — defined in `<agent>/infra-health.yaml` under `components:`.

Schema per component:
```yaml
last_success: "2026-03-14T15:30:00"  # or null
last_failure: null                    # or ISO timestamp
last_failure_reason: null             # or error string
consecutive_failures: 0
session_last_checked: 40              # or null
```

Side effects: `check` and `check-all` automatically update `<agent>/infra-health.yaml`
with success/failure timestamps. No separate record call needed from the agent.

Skill-to-component mapping lives in `<agent>/infra-health.yaml` (`skill_mapping` + `category_mapping`).
Used by Phase 0.5b/2.5b blocker gates and Phase 4.2 domain post-execution steps.

All backed by `core/scripts/infra-health.py` (Python 3, PyYAML).

---

# Verify Before Assuming — Infrastructure Rules
# These are the infrastructure-specific rules. The broader principle (multi-signal
# requirement for all negative conclusions) is in core/config/conventions/negative-conclusions.md.

1. **Probe before concluding** — Before saying "X is down/unreachable/unavailable",
   run `infra-health.sh check <component>`. SSH timeout, curl failure,
   connection refused — these are evidence. Assumptions are not.
2. **Check recency** — Read `<agent>/infra-health.yaml` for last successful contact.
   If a component succeeded recently (current session or last 2 hours), it
   likely still works. Probe anyway if about to skip a goal over it.
3. **Stale host keys are not outages** — SSH "REMOTE HOST IDENTIFICATION HAS
   CHANGED" is a one-command fix, not an infrastructure failure.
4. **Never rationalize without a probe** — "Service is down",
   "unreachable", "server is not running" are PROHIBITED unless preceded by
   a failed command in the same session.

When this applies:
- Phase 2.5b blocker gate (before accepting a blocker as still valid)
- Any moment the agent considers deferring a goal due to infrastructure
- Any moment the agent is about to declare infrastructure unavailable

Reference: `core/scripts/infra-health.sh`, `<agent>/infra-health.yaml`

---

# Knowledge Reconciliation — Detailed Protocol

## When to Reconcile

After ANY of these events, ask: "Which knowledge nodes informed this action, and
do they need updating?"

1. **External code change** — editing files outside world/<agent>/meta/ (bug fix, feature, refactor)
2. **Hypothesis resolution** — an outcome contradicts or refines what a node says
3. **User correction** — the user provides information that supersedes stored knowledge
4. **Research discovery** — new research reveals a node's content is outdated or wrong
5. **Deployment or verification** — confirming a change works means the old state is gone

## How to Reconcile

- Identify which tree nodes were consulted or are affected
- Read those nodes
- If the node content no longer reflects reality: update it (Edit, not Write)
- Update the node's `last_updated` and `last_update_trigger` front matter
- If significant: propagate up the parent chain via tree-update.sh

The `last_update_trigger` front matter field tracks WHY a node was last updated:

| Type | Meaning |
|------|---------|
| `research` | New research findings encoded |
| `consolidation` | Session-end encoding |
| `reconciliation` | Action revealed stale content |
| `debt-reconciliation` | Deferred reconciliation resolved |
| `user-correction` | User provided authoritative correction |
| `post-reflection-reconciliation` | Reflection lesson updated the node |
| `capability_change` | Accuracy/capability level changed |
| `self_evolution` | Self evolved via sq-012 spark |
| `initial_creation` | First-time creation during /start |

## Knowledge Debt

When reconciliation cannot happen immediately (mid-goal, complex update needed),
record the debt in working memory's `knowledge_debt` slot:

```yaml
knowledge_debt:
  - node_key: "some-service"
    reason: "Fixed null handling in timeout logic — node doesn't document this mechanism"
    source_goal: "g-001-03"
    priority: HIGH
    created: "2026-03-10"
```

Debts are swept during session-end consolidation. Debts older than 2 sessions
are promoted to HIGH priority. HIGH debts are resolved before new goals start.
