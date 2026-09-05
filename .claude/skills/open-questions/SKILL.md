---
name: open-questions
description: "Shows the user dashboard: pending questions the agent has logged for the user, goals with participants including user, and blocked goals grouped by blocker reason. Use whenever the user says \"what do you need from me\", \"what's blocked\", \"what questions do you have\", \"what am I on the hook for\", or invokes /open-questions directly. USER-ONLY — Claude must never invoke this autonomously. Primes context first so follow-up is knowledge-informed."
user-invocable: true
disable-model-invocation: true
triggers:
  - "/open-questions"
conventions: [aspirations]
minimum_mode: reader
revision_id: "skill-bootstrap-open-questions-f2715d"
previous_revision_id: null
---

# /open-questions — User Dashboard

Shows what needs the user's attention: pending questions the agent logged,
goals assigned to the user, and blocked goals grouped by reason. Primes context
first so follow-up discussion is informed by domain knowledge.

**USER-ONLY COMMAND.** Claude MUST NEVER invoke this skill autonomously.
Valid from ANY state (RUNNING, IDLE, UNINITIALIZED).

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Phase 1: Prime Context

```
1. Invoke /prime
   — Loads Self, guardrails, reasoning bank, category-specific knowledge
   — If UNINITIALIZED and /prime outputs "Nothing to prime": SKIP (continue to Phase 2)
```

## Phase 2: Scan Pending Questions (FLEET-WIDE)

This dashboard answers "what does the USER owe / what is waiting on them" — a
question about the WHOLE FLEET, not the bound agent. Both steps below are
required: fixing either alone still leaves the user blind (g-115-3074).

```
1. Refresh every agent's mirror (DATA leg — defeats stale/absent peer files):
   Bash: `bash core/scripts/owncloud-pull.sh --all-agents --only pending-questions.yaml`
   → ~1s fleet-wide. `--only` is load-bearing: the unfiltered fleet pull is a
     full continuity sweep at ~59s/agent (~5min), which is not a viable cost for
     an interactive dashboard. Fleet roster comes from team-state.
   → Best-effort: on a non-own-cloud backend this is a no-op, and a per-agent
     failure is isolated (the sweep continues). NEVER block the dashboard on it —
     a failed refresh degrades to reading whatever mirrors exist, which is
     strictly better than showing nothing. Proceed to step 2 regardless.

2. Read every agent's questions (SKILL leg — defeats bound-agent-only scope):
   Bash: `bash core/scripts/pending-questions-read.sh --all-agents --status pending`
   → JSON array of pending entries, each tagged with an `agent` key. Store as
     pending_questions list; group by `agent` for output.
   This reader is shape-tolerant (flattens the dict-wrapper / list-with-wrapper /
   bare / mixed on-disk shapes via the same _load_questions logic the sweep
   sibling uses, rb-1786) and applies that SAME flattener per agent file. Do NOT
   hand-roll a naive top-level `status == "pending"` scan of the raw YAML — it
   silently SKIPS entries nested inside a `{questions: [...]}` wrapper
   (g-115-3039) — and do NOT hand-roll a fleet walk either: the flatten body is
   kept byte-faithful across three lock-step copies, and a fourth copy in skill
   pseudocode is the least likely to be kept in sync. A missing or malformed
   per-agent file contributes [] without failing the others.
```

**Why fleet-wide** (user-surfaced 2026-07-25): the user ran `/open-questions`
expecting the fleet backlog and effectively saw nothing. Two independent defects
produced that. DATA — `owncloud-pull.sh` was `--agent`-scoped, so an alpha-bound
session never refreshed PEER files; on cc-04 bravo's copy was 18 days stale and
echo/foxtrot/zeta's were ABSENT entirely. SKILL — this phase read only the bound
agent's path, so even with perfect mirrors an alpha-bound run structurally could
not surface the other agents' questions. Measured after the fix: 13 → 30
questions visible, matching a direct authoritative-store read exactly
(alpha 10, bravo 2, echo 4, foxtrot 7, zeta 7).

## Phase 3: Scan User-Participant Goals

```
1. Bash: `aspirations-query.sh --goal-field participants user --full`
   → `--full` is LOAD-BEARING: the default projection omits `user_leg_scope`
     entirely (verified 2026-08-04: 0 of 160 rows carry the key without it,
     45 with it), and the bucket split below is driven by that field.
   → Filter returned goals where status NOT in ("completed", "skipped", "expired")

2. Bash: aspirations-read.sh --archive
   → Same filter (catch goals in archived aspirations that are still open)
   Append to the surviving list

3. Classify every surviving goal into exactly one bucket. The classifier is
   `is_decision_like()` in `core/scripts/gates/user_leg_scope.py` — the SSOT
   for the scope vocabulary and its decision/action split. Apply its logic:

   BUCKET A — "Decisions Needed" (NEVER compressible):
     is_decision_like(user_leg_scope, title) is True — i.e. scope in
     DECISION_LIKE_SCOPES (architecture-decision, deployment-approval,
     credential-grant), OR a free-text scope containing decision/approval/
     grant, OR a title starting with a DECISION_TITLE_PREFIXES marker
     ("Decide:", "USER DIRECTIVE:", "USER:", "PARKED tracker:").

   BUCKET B — "Actions Needed":
     Not bucket A, AND (user_leg_scope is set (action-like: commit, push,
     data-provision, new-resource, or other non-decision free text)
     OR participants == ["user"] (user-only with no agent leg)).

   BUCKET C — "Reviewer / Improvement Work":
     Everything else: unscoped, participants include agent, no decision
     marker. Collapsed BY SPEC in Phase 4 — the LLM never invents its own
     compression criterion (2026-08-04 incident: ad-hoc LLM triage of a flat
     42-row table hid an architecture-decision goal from the user).
```

## Phase 3.5: Scan Blocked Goals

```
1. Bash: goal-selector.sh blocked
   → Parse JSON output → store as blocked_data
   → blocked_goals = blocked_data.blocked_goals
   → by_reason = blocked_data.by_reason
   → summary = blocked_data.summary
```

## Phase 4: Output Summary

```
═══ OPEN ITEMS ════════════════════════════════

IF pending_questions is non-empty:
  ## Pending Questions ({total} across {N} agents)
  Group by the `agent` key, agents sorted alphabetically, and emit one
  sub-section per agent so the user can see who is waiting on what:

  ### {agent} ({count})
  | ID | Date | Question | Default Action |
  |---|---|---|---|
  {for each of that agent's entries: id, date, question (truncated to ~100 chars), default_action}

IF any user-goal bucket is non-empty:
  ## User Goals ({total across all three buckets})

  IF bucket_A is non-empty:
    ### Decisions Needed ({count})
    DO NOT COMPRESS, SUMMARIZE, OR OMIT ANY ROW IN THIS SECTION — every goal
    here wants the user's judgment; hiding one is a critical error (the
    2026-08-04 g-115-4225 incident). Per guard-1066, re-verify each row's
    premise against current state before presenting — but a stale premise
    means SURFACE IT WITH THE STALE-PREMISE FINDING (so the user can close
    it), never silently drop the row. Over-surfacing is the safe direction.
    | Goal | Aspiration | Title | Scope | Priority | Status |
    |---|---|---|---|---|---|
    {each: goal_id, aspiration_id, title (full, NO truncation),
     user_leg_scope or "unscoped (title-match)", priority, status}

  IF bucket_B is non-empty:
    ### Actions Needed ({count})
    | Goal | Aspiration | Title | Scope | Priority | Status |
    |---|---|---|---|---|---|
    {each: goal_id, aspiration_id, title, user_leg_scope or "unscoped", priority, status}
    IF count > 15: render the 15 highest-priority rows, then
      "... plus {N} more action goals (IDs: {comma list})"

  IF bucket_C is non-empty:
    ### Reviewer / Improvement Work ({count} goals — collapsed by spec)
    {count} agent-led improvement goals carry you as reviewer only.
    IDs: {comma-separated goal_ids, grouped by aspiration}
    IF any have user_leg_scope unset:
      ({N} of these declare no user_leg_scope — candidates for scope
      backfill or participant drop via the reclaim-routed-work lane P.)

IF blocked_goals is non-empty:
  ## Blocked Goals ({summary.total_blocked} of {summary.total_active_goals} active goals)

  IF by_reason.infrastructure.count > 0:
    ### Infrastructure ({count})
    | Goal | Aspiration | Title | Blocked By |
    |---|---|---|---|
    {for each infrastructure goal: goal_id, aspiration_id, title (truncated ~50 chars), block_detail}

  IF by_reason.dependency.count > 0:
    ### Dependency Chain ({head_count} heads, {downstream_count} downstream)
    | Goal | Aspiration | Title | Waiting On |
    |---|---|---|---|
    {for each HEAD goal only: goal_id, aspiration_id, title (truncated ~50 chars), unmet dep IDs}
    IF downstream_count > 0:
      "... plus {downstream_count} downstream goals in dependency chains"

  IF by_reason.deferred.count > 0:
    ### Deferred ({count})
    | Goal | Aspiration | Title | Until | Reason |
    |---|---|---|---|---|
    {for each: goal_id, aspiration_id, title (truncated ~50 chars), deferred_until, defer_reason}

  IF by_reason.hypothesis_gate.count > 0:
    ### Hypothesis Gate ({count})
    | Goal | Aspiration | Title | Not Before |
    |---|---|---|---|
    {for each: goal_id, aspiration_id, title (truncated ~50 chars), block_detail}

  IF by_reason.explicit_status.count > 0:
    ### Explicitly Blocked ({count})
    | Goal | Aspiration | Title | Reason |
    |---|---|---|---|
    {for each: goal_id, aspiration_id, title (truncated ~50 chars), block_detail}

IF all three empty (pending_questions, all user-goal buckets, blocked_goals):
  Nothing requires your attention. All questions answered, no user goals open, no blocked goals.

───────────────────────────────────────────────
Summary: {N} pending questions across {A} agents, {M} user goals ({count(bucket_A)} decisions needed), {B} blocked goals
═══════════════════════════════════════════════
```

## Chaining

- **Called by**: User only. NEVER by Claude.
- **Calls**: `/prime` (read-only context loading),
  `owncloud-pull.sh --all-agents --only pending-questions.yaml` (Phase 2 mirror
  refresh), `pending-questions-read.sh --all-agents` (Phase 2 fleet read),
  `aspirations-query.sh`, `aspirations-read.sh`, `goal-selector.sh blocked`
- **Modifies**: No agent state. The Phase 2 refresh writes peer
  `session/pending-questions.yaml` mirrors from the authoritative store — a
  cache fill, freshness-gated and never clobbering unpushed local writes (the
  manifest baseline gates every overwrite). The dashboard itself is read-only.
