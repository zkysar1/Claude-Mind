---
name: aspirations-execute
description: "Phase 4 of the aspirations loop: executes a selected goal end-to-end with precondition checks, LLM-driven intelligent retrieval, memory deliberation, subagent delegation, primary execution, fail-fast cascade, experience archival, context-utilization feedback, domain post-execution steps (commit/push per world/conventions), knowledge reconciliation, and batch execution. Fires only when called from inside the /aspirations orchestrator — after /aspirations-select has chosen a goal and /decompose has broken it into primitives. Never invoke directly from reader or assistant mode."
user-invocable: false
parent-skill: aspirations
triggers:
  - "Phase 4"
conventions: [aspirations, pipeline, experience, tree-retrieval, retrieval-escalation, goal-schemas, infrastructure, reasoning-guardrails, agent-spawning]
minimum_mode: autonomous
revision_id: "skill-bootstrap-aspirations-execute-0cf4df"
previous_revision_id: null
---

# Phase 4: Goal Execution

Invoked as Phase 4 of the aspirations loop after goal selection (Phase 2) and decomposition (Phase 3). Covers the full execution pipeline: precondition checking, intelligent LLM-driven retrieval, memory deliberation, agent delegation, primary execution, fail-fast cascade, experience archival, context utilization feedback, domain post-execution steps, knowledge reconciliation, and batch execution.

## Inputs (from orchestrator)

- `goal`: Selected goal object from Phase 2
- `aspiration_id`: Parent aspiration ID
- `source`: Queue origin (`"world"` or `"agent"`) — pass `--source {source}` to all `aspirations-*.sh` calls
- `batch_mode`: Boolean (from Phase 2)
- `outcome_class`: Set by Phase 4-post after execution

---

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Execution Autonomy Rule

The agent makes ALL decisions autonomously during goal execution — never
stops to ask "should I push?" or "what next?". Phase 4.2 domain steps handle
push/deploy; the loop handles selection.

For significant judgment calls (architecture choices, deploy strategy,
trade-offs), pick the safer/simpler option when unsure, then log one
`pq-NNN` entry in `agents/<agent>/session/pending-questions.yaml` framing it as
"I decided {X} because {Y} — override if you disagree" with
`default_action: "Already executed: {what}"` + `status: pending`. User
reviews retroactively via `/respond` or session recap. Continue immediately.

---

## Cognitive Primitives (Always Available)

During ANY phase — goal execution, error handling, reflection, spark checks —
the agent can create goals from things it notices. Five types: **Unblock**
(CREATE_BLOCKER only — see its digest), **Investigate**, **Idea**,
**Maintain** (inline framework fix, `status: completed` on creation),
**Cross-Agent Insight** (posted to findings board).

Full schemas, the Maintain inline-fix rationale, and the Cross-Agent Insight
tag spec live in `core/config/cognitive-primitives-digest.md`. Load on-demand:

```
Bash: load-cognitive-primitives.sh → IF path returned: Read it
# Then file the primitive per the digest schema.
# See guard-148 (Maintain) and core/config/conventions/board.md (Cross-Agent Insight).
```

A single event can spawn all five primitives simultaneously — they are NOT
mutually exclusive. Dedup before filing: check pending/in-progress goals for
similar titles first.

---

## Phase 4 Setup: Cross-Agent Env Routing (g-115-978 Option 3)

When the selector pulled this goal from a sibling agent's queue,
`aspirations-select` Phase 2.95 split `source='cross-agent:<sib>'` into
`effective_source='agent'` + `cross_agent_owner='<sib>'` and wrote the latter
into `iteration-checkpoint.json` (validated by `loop-state-save.py` SCHEMA).
This setup fires BEFORE the precondition re-check so every subprocess call
in Phase 4 — including pre-claim update-goal and depth-estimate — routes
through the owner's identity. Without it, calls write to THIS agent's queue
and the cross-pulled goal never lands back in the sibling's `aspirations.jsonl`.

```
# Read cross_agent_owner from the iteration checkpoint. Empty/absent means
# this is a normal (non-cross-agent) execution; ENV_PREFIX stays empty and
# every downstream call behaves exactly as before.
Bash: cross_agent_owner=$(py -3 -c "import json; d=json.load(open(r'agents/$MIND_AGENT/session/iteration-checkpoint.json',encoding='utf-8')); print(d.get('cross_agent_owner','') or '')" 2>/dev/null || echo "")

IF cross_agent_owner is non-empty:
    # ENV_PREFIX applies ONLY to subprocess calls whose AGENT_DIR resolves
    # via MIND_AGENT. Affected (write the owner's state):
    #   - aspirations-update-goal.sh --source agent <goal-id> <field> <value>
    #   - aspirations-complete-by.sh <goal-id> (when --source agent)
    #   - aspirations-release.sh <goal-id> (when --source agent)
    #   - iteration-close.sh --phase * (operations against the owner's queues)
    #   - recurring-close.sh <goal-id> (wraps iteration-close internally)
    # NOT affected (use the calling agent's identity):
    #   - team-state-in-flight.sh --agent <SELF>   (NEVER swap — this is the
    #     world-level liveness record; partner observers need to see ALPHA
    #     claimed BRAVO's goal, not BRAVO claimed it)
    #   - board-post.sh (board entries are authored by the calling agent)
    #   - heartbeat-tick.sh (ticks THIS runner's heartbeat)
    #   - aspirations-claim.sh (--source world is world-scoped — no swap)
    # Pattern: prefix the affected subprocess invocations with
    #   MIND_AGENT={cross_agent_owner} <command>
    # rather than the global env-prefix the PreToolUse[Bash] hook applies.
    # Explicit per-call prefix wins (`bash-agent-inject.sh` detects and
    # preserves user-supplied MIND_AGENT=*).
    ENV_PREFIX="MIND_AGENT=${cross_agent_owner}"
ELSE:
    ENV_PREFIX=""   # normal execution; no swap
```

`ENV_PREFIX` is the variable name every downstream Bash invocation in this
SKILL.md references when calling an affected script. A non-cross-agent
iteration sets it to empty string and the calls behave exactly as before.

## Phase 4 Preamble: Cost-Ordered Precondition Checking

Before expensive data retrieval (SSH, large files, APIs), check local/cheap
preconditions first: timestamps, git log, file existence, metadata.
See: guard-009

### Pre-Claim Structured Precondition Re-Check

Catches the selector→claim race. The selector already filtered goals with
failing structured preconditions at COLLECT time, but state may have flipped
between selection and now (another agent completed a dependency, a pipeline
produced new output, an upstream process restarted, etc.). Re-evaluate the
goal's structured preconditions cheaply here — before any SSH / API / large
retrieval costs are incurred.

```
Bash: predicate-eval.sh --goal {goal.id} --types structured
parsed = JSON output from predicate-eval.sh
exit_code = $?

IF exit_code == 1:
    # At least one structured precondition failed.
    failed_ids = [r.predicate_id or r.type for r in parsed.results if not r.passed]
    Bash: ${ENV_PREFIX} aspirations-update-goal.sh --source {source} {goal.id} \
        defer_reason "precondition_unmet:{','.join(failed_ids)}"
    Bash: ${ENV_PREFIX} aspirations-update-goal.sh --source {source} {goal.id} \
        defer_reason_set_at "$(date +%Y-%m-%dT%H:%M:%S)"
    Bash: ${ENV_PREFIX} aspirations-release.sh {goal.id}     # release the claim
    Journal: "pre-claim precondition unmet for {goal.id}: {failed_ids}"
    GOTO Phase 7 (select next goal)
# Exit 0 = all passed (vacuous empty-list included). Exit 2 = id not found, warn and proceed.
```

**Distinct from CREATE_BLOCKER**: preconditions are *declared expected
dependencies*, blockers are *unexpected infrastructure failures*. A failed
precondition re-check defers the goal via `defer_reason` (auto-clears when
the predicate flips). It does NOT create a blocker, does NOT cascade-block
same-skill goals, does NOT notify the user.

### Phase 4-pre: Target-State Probe (advisory, gated)

Before spending retrieval + skill-invocation tokens, cheaply grep the
target file(s) named in the goal description to check whether the
identifiers-to-implement are already present. No-op unless the goal's
`title + description` yields both a file path and an identifier.

Origin: rb-382 / g-115-141 (fix implemented by g-115-137 predated goal
filing; grep would have caught the already-done state at execution-time).
Sibling to the filing-time check in `goal-duplication-gate.py` — same
extractor (`core/scripts/_target_state.py`), different chokepoint.

```
Bash: bash core/scripts/target-state-probe.sh --goal-id {goal.id} --output json
parsed = JSON output
exit_code = $?   # always 0 unless the probe crashed or got no goal data

# Probe is ADVISORY. Phase 5 verification remains the ground truth —
# this only short-circuits retrieval when evidence says work is done.
# The probe writes NOTHING to disk: its stdout JSON is the single
# source of truth. The orchestrator journals the summary for audit.
IF parsed.probe.verdict == "already_present":
    Journal: "target-state-probe: " + parsed.summary
    # Downstream skill invocation MAY branch into verify-only semantics
    # based on the summary. Never auto-skip — Phase 5 verification
    # still runs and closes the goal if and only if state actually
    # matches the verification criteria.
IF parsed.probe.verdict in ("partially_present", "absent", "unknown"):
    # Proceed with normal execution. No journal entry — noise.
    pass
```

Guard: the probe is fail-open. A crash, a missing `aspirations-read.sh`,
unreadable target files, or an extraction yielding zero identifiers all
produce `verdict="unknown"` and a no-op — never a false block on real work.

## Phase 3.9: Pre-Execution Domain Steps

```
Bash: load-conventions.sh pre-execution → IF path returned: Read it
# Procedural convention — gate on file EXISTENCE, not load status.
Bash: test -f "$WORLD_DIR/conventions/pre-execution.md"
IF exists: follow each step; any step returning SKIP → mark goal skipped, GOTO Phase 7.
```

## Phase 3.95: Depth Estimate

Pre-commit to an expected depth tier so post-hoc rationalization can't drift the
classification. `reflect-on-outcome` logs mismatches to `meta/depth-calibration.jsonl`
so the next estimate can be better than the last.

```
estimated_depth =
  routine  — recurring AND verification is simple presence check (file/count/status)
  standard — not recurring AND category in last-50 histogram AND ≤2 checks
  deep     — unfamiliar category OR >2 checks OR cross-process OR research/design/investigate

estimated_seconds = midpoint of tier range (routine 30-120s, standard 120-600s, deep 600-3600s);
                    narrow if prior pattern-signature evidence exists.

Bash: ${ENV_PREFIX} aspirations-update-goal.sh --source {source} {goal.id} estimated_depth {tier}
Bash: ${ENV_PREFIX} aspirations-update-goal.sh --source {source} {goal.id} estimated_seconds {sec}
```

## Phase 3.97: Inbound Signal Sweep (G6 / R10)

Between goal selection and execution, inbound signals may have arrived —
board posts from partner agents or partner team-state shifts. Without an
explicit sweep, execution proceeds on stale assumptions.

Per `.claude/rules/retrieve-before-deciding.md` decision point 6 ("acting on
an inbound signal"). Note: the access-email skill handles email-class inbound
signals via its own G6/R10 retrieval (Inbound Signal Retrieval section); this
sweep covers the board + team-state surface that the executor sees.

```
# Cheap probes — signal-presence checks only. The 30m lookback covers
# typical claim→execute latency (goal-selector pick + Phase 4 retrieval).
# This is fixed-window for simplicity; board-read uses duration filters
# (`--since 30m`), not ISO timestamps.
inbound_signals = []

# 1. Coordination board posts since approximate claim time
Bash: posts=$(bash core/scripts/board-read.sh --channel coordination --since 30m --json 2>/dev/null || echo "")
IF posts is non-empty AND posts contains entries authored by the partner agent
   (NOT self — filter on `author != MIND_AGENT`):
    inbound_signals.append({"type": "board_post", "data": posts})

# 2. Partner agent team-state shift (claimed a goal that overlaps mine)
Bash: partner_in_flight=$(bash core/scripts/team-state-read.sh --field "agent_status" --json 2>/dev/null || echo "{}")
IF partner_in_flight contains an entry for any agent != self AND that
   entry's in_flight.goal_id matches this goal's category OR blocked_by chain:
    inbound_signals.append({"type": "partner_overlap", "data": partner_in_flight})

# If any signals fired, retrieve context BEFORE acting on them
IF inbound_signals is non-empty:
    # Build a query from the inbound signal content
    signal_summary = concatenate first 200 chars of each signal's body/text
    Bash: bash core/scripts/retrieve.sh --category "{goal.category} {signal_summary[:200]}" --depth shallow --read-only

    Use the returned JSON to decide:
      - If a signal indicates the goal should defer (e.g., partner claimed a
        prerequisite): defer with reason and exit to Phase 7
      - If a board post warns about an in-flight conflict: re-check the claim
        and consider release-and-reclaim

    Diary breadcrumb:
      echo '{"entry_type":"observation","goal_id":"<goal.id>","content":"Inbound signal sweep: <count> signals, retrieved context"}' | bash core/scripts/execution-diary.sh append

# Fail-open: any of the probe scripts erroring is no-op. The sweep must not
# block execution. Phase 4 proceeds with whatever inbound_signals collected.
```

## Phase 4: Execute (with intelligent retrieval)

```
Bash: ${ENV_PREFIX} aspirations-update-goal.sh --source {source} <goal-id> status in-progress
Bash: ${ENV_PREFIX} aspirations-update-goal.sh --source {source} <goal-id> started <today>

# ── Intelligent Retrieval Protocol ──────────────────────────────────
# Full pseudocode lives in the execute-protocol digest. Load on-demand:
Bash: load-execute-protocol.sh → IF path returned: Read it
# The digest covers Steps 1-5c (tree summary → primary nodes → supplementary
# → memory deliberation → codebase/web escalation → deliberation-on-hypothesis
# → retrieval influence articulation). Follow the digest's Retrieval Protocol
# section inline; the SKILL.md no longer duplicates it.
# Key side effects to remember:
#   - retrieve.sh --goal auto-writes retrieval-session.json (utilization tracking)
#   - The utilization-gate.sh hook guarantees feedback runs even if Phase 4.26 skips
#   - Step 4b (strategy-apply.sh) closes the meta-strategy → execution loop
#   - Step 5b.1 persists deliberation onto the linked hypothesis record when present
# ── End Intelligent Retrieval ───────────────────────────────────────

# Team-Based Research Delegation (optional — tool, not rule)
#
# The host MAY dispatch read-only team agents via TeamCreate + Agent
# (team_name={team}, run_in_background=true) to pre-fetch research. Full
# protocol + context-injection: core/config/conventions/agent-spawning.md.
#
# Team agents do READ-ONLY research. They MUST NOT invoke skills, write/edit
# files, call state-mutating scripts, or make git commits. Use
# build-agent-context.sh to inject primed context at spawn time; register via
# pending-agents.sh BEFORE dispatch (crash-safe staleness timeout).
#
# Curriculum gate: curriculum-contract-check.sh --action allow_multi_goal_parallelism
#   exit 0 → dispatch allowed; exit 1 → sync research instead.
# Prompt MUST specify: 10-minute hard limit (stop new work at 8m), read-only
# constraint, structured-findings report format.

# Misroute guard: catch skill-creation goals that arrived with skill: null
IF goal.skill is null AND goal.title matches (forge|create.*skill|make.*skill|skill.*creation):
    goal.skill = "/forge-skill"; goal.args = "list"

# Capture start time so Phase 4.05 can detect mid-execution drift
phase_4_started_at = "$(date +%s)"

# Pre-apply consult gate (g-115-826, rb-987 / g-115-796 incident):
# When this goal is an inherited cross-agent Apply touching framework files
# (core/, .claude/, world/conventions/, core/config/, SKILL.md, CLAUDE.md),
# emit an advisory-loud directive BEFORE the first Edit to surface
# guardrails/reasoning-bank entries that may contradict the inherited spec.
# Triggers only when goal.handoff_from is set AND handoff_from != $MIND_AGENT
# AND title/description references a framework-file path. Own-authored goals
# and non-framework Applies skip silently. Fail-open on env/path errors.
# The gate exits 0 unconditionally — it shifts the consult-before-edit
# discipline from after-the-fact learning-gate audit to before-the-fact
# directive but does not block the loop.
Bash: bash core/scripts/pre-apply-consult-gate.sh <goal.id>

# Execute primary goal inline (host does ALL writing)
result = invoke goal.skill with goal.args
```

## Phase 4.04: Decision-Rule Application Counter (E8)

Tree nodes carry `## Decision Rules` lines (`- IF X THEN Y — source: ...`)
that the retrieval lane surfaces alongside the node body. When execution
actually relied on one of those rules, that fact must be encoded — otherwise
`reflect-maintain` Step 1d cannot distinguish dead rules from fresh ones, and
the active-forgetting pass leaves stale rules in place. Counter format:
`— applied: N (YYYY-MM-DD)` suffix appended to the rule line on first use,
incremented on subsequent uses. The increment is idempotent within a call
but NOT across calls (every cited use bumps the counter once).

```
# Self-report: which Decision Rules in the retrieval manifest informed
# this execution? "Informed" means the agent reasoned about the rule
# BEFORE deciding the next step — not "the rule was visible in
# retrieval output." The encoded counter reflects ACTIONS taken on
# rules, not READS of them.

cited_rules_by_node = {}  # {node.file: ["IF X THEN Y", ...], ...}
For each tree node in the retrieval manifest:
    For each Decision Rule the agent CITED as informing execution:
        cited_rules_by_node.setdefault(node.file, []).append(rule_text)

IF cited_rules_by_node is non-empty:
    For each node_file, rule_list in cited_rules_by_node.items():
        Bash: echo '{"rules": <rule_list as JSON array>}' \
              | bash core/scripts/decision-rules-increment.sh \
                  --node-path <node_file>
        # Fail-open: a non-zero exit means no rule matched (the agent's
        # self-report misquoted the rule body). Log to execution-diary
        # but never block Phase 4.05.

# Skip entirely IF the agent did not cite any rule. Empty self-report is
# legitimate — many goals are mechanical and run without rule citation.
```

## Phase 4.05: Mid-Execution Drift Check (G4 / R11)

Phase 4's retrieval snapshot is taken before goal invocation. For goals that
take 30+ minutes wall-clock OR produce result text exceeding 4000 chars, the
world may have changed during execution — partner agents may have completed
related goals, new board posts may have arrived, the agent's own writes may
have invalidated retrieved nodes. Without a drift check, Phase 4.1's
guardrail consultation and Phase 4.2's domain steps run on stale context.

Per `.claude/rules/retrieve-before-deciding.md`: every consequential decision
should retrieve in the same turn. A long execution effectively spans multiple
decision points; the snapshot ages out.

```
phase_4_duration_sec = $(($(date +%s) - phase_4_started_at))
result_size_chars = len(result.text if result.text else "")

IF phase_4_duration_sec > 1800 OR result_size_chars > 4000:
    # Re-retrieve at the same depth as Phase 4's initial snapshot
    # Use goal.category as the query — the retrieval that informed Phase 4
    Bash: retrieve.sh --category "{goal.category}" --depth shallow --read-only

    From the returned JSON, compare to Phase 4's snapshot:
      - New reasoning_bank entries added since phase_4_started_at? (check `created` field)
      - New guardrails added that constrain remaining cognitive phases?
      - Tree nodes touched since phase_4_started_at? (check `last_updated`)

    IF any drift detected:
      Log: "MID-EXECUTION DRIFT: {phase_4_duration_sec}s elapsed, {N} new entries"
      Pass the drift summary into Phase 4.1's guardrail consultation context
      so guardrail evaluation uses the fresh set, not the stale snapshot.

      Diary breadcrumb:
        echo '{"entry_type":"observation","goal_id":"<goal.id>","content":"Mid-exec drift: <N> new RB/G entries detected"}' | bash core/scripts/execution-diary.sh append

    ELSE:
      # No drift — proceed with Phase 4's snapshot for downstream phases
      pass

ELSE:
    # Execution was short enough that drift is unlikely. Skip.
    pass
```

Fail-open: if retrieve.sh errors, log and proceed with Phase 4's snapshot.
The drift check must not block post-execution phases.

### Phase 4.05 Chunked-Encoding Producer (E13)

When this branch fires (long execution), the result.text often contains
multiple distinct learnings — different files modified, different probes
run, different conclusions reached. Bundling all of those into ONE Phase 8
encoding payload collapses them into a single Key Insight paragraph; each
finding scores against the gate as a single average rather than on its
own merit.

The shared chunk schema (Section F of
`core/config/encoding-protocol-digest.md`) lets Phase 8 process N distinct
encoding decisions instead of one bundle. Producer here, consumer in
Phase 8.

```
IF phase_4_duration_sec > 1800 OR result_size_chars > 4000:
    # Continue past the drift check above. Add CHUNKING below.

    # Segment result.text by natural boundaries:
    #   - ### Markdown headings
    #   - distinct tool-output blocks (e.g., "▸" prefixes)
    #   - paragraph breaks where topic shifts
    # Cap at 5 chunks (above that, the encoding-pass cost > value).
    # Skip if result.text has no natural splits (single coherent finding).

    chunks = split_result_by_boundaries(result.text, max_chunks=5)
    IF len(chunks) <= 1: SKIP rest of chunked-encoding producer
                          (one bundled payload is fine)

    For each chunk_idx, chunk_text in enumerate(chunks):
        # Score this chunk independently per Section A of the digest
        scores = {
          "novelty":            <agent self-rates 0-1>,
          "outcome_impact":     <agent self-rates 0-1>,
          "surprise":           <agent self-rates 0-1>,
          "goal_relevance":     <agent self-rates 0-1>,
          "repetition_strength": <agent self-rates 0-1>
        }
        # Classify content_type by chunk inspection
        content_type = "finding" | "decision" | "code-change" | "observation"
        # Identify target node (or null) by topic
        target_article = <node_key from tree-find-node, or null>

        Bash: echo '{
          "source_goal": "<goal.id>",
          "chunk_idx": <idx>,
          "chunk_total": <len(chunks)>,
          "chunk_text": "<chunk_text[:2000]>",
          "content_type": "<type>",
          "scores": <scores>,
          "target_article": <key or null>,
          "replay_priority": "<replay-priority>"
        }' | bash core/scripts/wm-append.sh sensory_buffer

    Bash: echo '{"entry_type":"observation","goal_id":"<goal.id>","content":"E13 chunked encoding: <N> chunks emitted from <result_size> chars"}' | bash core/scripts/execution-diary.sh append
```

Fail-open: chunk-append errors log to execution-diary but never block
Phase 4-post. Phase 8 consumes whatever chunks landed.

## Phase 4-post: Outcome Classification

Binary: routine (recurring + no findings) or deep (everything else). Gates
post-execution cognitive phases; does NOT affect execution itself.

```
outcome_class = "deep"   # default
IF goal.recurring AND goal_succeeded AND result produced no actionable items
   AND no new information:
    outcome_class = "routine"

# SEMANTIC OVERRIDE — knowledge-debt closure (rb-245):
# A routine goal that clears a declared knowledge_debt entry OR freshly
# touches the tree node pointed at by goal.closes_knowledge_debt becomes
# "deep". Without this override, debt-closing goals silently skip encoding.
IF outcome_class == "routine" AND goal.closes_knowledge_debt is non-empty:
    Check wm-read.sh knowledge_debt + tree node last_updated == today.
    IF debt_cleared OR node_touched_today: outcome_class = "deep"

# SAFETY: Non-recurring, failed, recurring-with-findings, or debt-closing
# goals ALWAYS remain "deep". Bias toward full treatment on any uncertainty.
```

## Phase 4-chain: Episode Chain Protocol (MR-Search)

After Phase 4-post outcome classification, before proceeding to Phase 4.0/4.1,
check if this goal should be retried with accumulated reflection context.
Inspired by MR-Search (arXiv 2603.11327): chaining N attempts with structured
self-reflection between each episode enables the agent to learn *through*
failure within the same problem context.

Full pseudocode (chain_trigger determination, context-zone-overridden
max_episodes, structured mini-reflection schema, goal-state rewiring for
re-execution, and breadcrumb emission) lives in its own digest. Load
on-demand when a deep-outcome failure is the classification:

```
IF outcome_class == "deep" AND NOT goal_succeeded AND result NOT in (INFRASTRUCTURE_UNAVAILABLE, RESOURCE_BLOCKED):
    Bash: load-episode-chain-protocol.sh → IF path returned: Read it
    Follow the digest's chain_trigger + max_episodes + reflection steps inline.
ELSE:
    # No chaining — clear any stale episode_chain WM slot if it matches this goal.id
    Bash: wm-read.sh episode_chain --json
    IF exists AND episode_chain.goal_id == goal.id: echo 'null' | Bash: wm-set.sh episode_chain
```

GUARD: Never chain infrastructure failures — Phase 4.0 owns the blocker protocol.

## Phase 4.0: Structured SKIP Fast-Path (with Recovery Attempt)

Skills that SKIP at preflight return INFRASTRUCTURE_UNAVAILABLE or
RESOURCE_BLOCKED. `skip-fastpath-eval.sh` maps `goal.skill` to its
component via `agents/<agent>/infra-health.yaml`, runs ONE recovery probe via
`infra-health check`, and returns the decision.

```
Bash: bash core/scripts/skip-fastpath-eval.sh \
         --goal-skill {goal.skill} --skip-result {result} \
         [--retry-attempted]
Read JSON result:
  next_action = "RETRY"          → re-execute skill → return to Phase 4.0
  next_action = "PROVISION"      → invoke probe_data.provision_skill →
                                   branch on same_skill_recovery:
                                     same-skill + success → result = provision_result → Phase 5
                                     cross-skill + success → retry_attempted = true → re-execute
                                     fail → follow CREATE_BLOCKER protocol
  next_action = "CREATE_BLOCKER" → follow CREATE_BLOCKER protocol below
  next_action = "NO_COMPONENT"   → no skill_mapping entry; CREATE_BLOCKER
```

The script enforces the verify-before-assuming multi-signal requirement
internally (`signal_count` and `flags` fields). When it returns
CREATE_BLOCKER with `signal_count < 2`, add an alternative probe
(different tool / endpoint) before proceeding.

Execution-diary breadcrumbs (`decision`, `observation`, `failure` entry
types) are written at each branch — they surface in postcompact-restore
for debugging.

Execution-diary breadcrumbs (`decision`, `observation`, `failure` entry types)
are written at each branch — they surface in postcompact-restore for debugging.

## CREATE_BLOCKER Protocol

Single source of truth for blocker creation. Invoked by Phase 4.0 (fast-path
SKIP), Phase 4.1e (unfixable infrastructure failure), and Phase 0.5b
(pre-selection sweep).

Orchestrated by `create-blocker.sh`, which runs (in order) wm-read dedup →
blocker-create-gate → conclusion-record → capability-gate →
aspirations-add-goal (unblocking goal) → wm-set(known_blockers). The LLM
still handles notification (forged-skill call) and the journal entry —
those actions are emitted in the script's `next_steps_for_llm` array.

```
Bash: bash core/scripts/create-blocker.sh \
         --failure-skill <skill> --failure-reason "<reason>" \
         --goal-id <id> --aspiration-id <asp-id> \
         --evidence '[...]' --probe-command "<exact>" \
         [--infra-health-check '{...}'] \
         [--schema-probe-evidence '{...}'] \
         --diagnostic-context '{...}' \
         --intended-participants agent
Read JSON: act on flags; for each entry in next_steps_for_llm, perform
the LLM-level action (notify user / journal entry).
```

Why two gates are non-negotiable: `blocker-create-gate.py` rejects four
structural failure modes — synthetic probe, single-signal negation,
statistical-without-schema-probe, infrastructure-without-health-check.
`capability-gate.py` matches `failure_reason` against agent-provisionable
capabilities to prevent unjustified `participants:[user]` routing.
Skipping either has historically produced false-positive blockers that put
the agent to sleep on non-problems.

Cross-references: `.claude/rules/capability-before-user.md`,
`.claude/rules/verify-before-assuming.md`,
`.claude/rules/probe-with-canonical-code-path.md`, `rb-226/246/258/245`.

## Phase 4.1: Post-Execution Guardrail Consultation + Error Response

After goal execution, consult learned guardrails and reasoning bank for
checks relevant to this goal's outcome. This is how the agent applies
lessons from experience — the specific checks are learned behaviors stored
in world/ (guardrails, reasoning bank), not hardcoded here.

For infrastructure goals, this enables learned behaviors to fire even when
the goal appeared to succeed. A "successful" goal can mask real infrastructure
errors that only guardrails know to check for.

Phase 4.1 does NOT fire guardrail checks on local/tooling errors: script
validation rejections, file not found in world/ or agent dir, build/compile errors during
code editing, or git failures.

Full pseudocode (guardrail-check for infrastructure + testing contexts,
error-alert cascade detection, severity classification, inline-fix attempt,
fallback to CREATE_BLOCKER) lives in `core/config/execute-protocol-digest.md`
under "Phase 4.1: Post-Execution Guardrails + Error Response". Load
on-demand; the summary below captures the control flow.

```
goal_succeeded = (result achieved verification.outcomes AND no errors/timeouts)
involved_infrastructure = (goal.skill in agents/<agent>/infra-health.yaml skill_mapping
                           OR goal.category in category_mapping)
involved_testing = (goal.category contains "test" OR goal.title contains "test"/"verify")

# Consultation: run guardrail-check.sh for each applicable context (infrastructure
# and/or testing). Execute every matched guardrail's action_hint. If output
# reveals issues (non-empty error alerts, health-check failures, testing-rule
# violations): guardrail_found_issues = true.

# Error-response protocol fires when: guardrail_found_issues OR (failed AND infrastructure).
IF guardrail_found_issues OR (NOT goal_succeeded AND involved_infrastructure):
    # 4.1a SEEK ERROR ALERTS: sleep 45 to catch async delivery (failure-only, skip
    #      if guardrails already confirmed). Read via infra-health.yaml error_check.
    # 4.1b CASCADE DETECTION: sort alerts by timestamp; earliest = root cause;
    #      build cascade_report {root_cause, cascade_effects, agent_observed_symptom, chain_summary}.
    # 4.1c SEVERITY: alerts present → "confirmed_infrastructure";
    #      structured failure markers → "explicit_failure"; else "soft_failure" (no block).
    # 4.1d TRY FIX INLINE: search tree/reasoning-bank/experience; ONE attempt, no loops.
    #      Success → log + optional Investigate/Idea goals (via cognitive-primitives digest).
    # 4.1e COULDN'T FIX → load-create-blocker-protocol.sh and invoke the protocol
    #      with diagnostic_context = {error_alerts count, cascade_chain, attempted_fix}.

    # Exit paths (do NOT merge — different semantics):
    #   NOT goal_succeeded: revert to status pending; continue (skip Phases 4.25-9).
    #   goal_succeeded + guardrail issue: fall through to Phase 4.25+ (goal completes).

# SAFETY: Guardrail findings override routine classification.
# Phase 4-post classified before guardrails ran — if guardrails found
# real issues, this IS new information regardless of skill result.
IF guardrail_found_issues:
    outcome_class = "deep"  # guardrail issues → override to deep
```

## Phase 4.2: Post-Execution Domain Steps

```
# Load domain convention into context if not yet loaded (dedup).
Bash: paths=$(bash core/scripts/load-conventions.sh post-execution 2>/dev/null)
IF paths is non-empty:
    Read the file at the returned path

# Follow domain post-execution steps if convention exists.
# CRITICAL: Gate on file existence, NOT on load status. The convention is procedural —
# it must run every goal, not just the first time it's loaded into context.
Bash: source core/scripts/_paths.sh && test -f "$WORLD_DIR/conventions/post-execution.md" && echo "exists"
IF exists:
    Follow each Step in the convention, evaluating conditions against current goal context
    Collect results (external_changes, behavioral_observations)
    Pass collected results to Phase 4.5 for knowledge reconciliation
ELSE:
    # No domain post-execution convention exists (fresh agent). Nothing to do.
    external_changes = null
    behavioral_observations = null
```

## Phase 4.25: Archive Goal Execution Trace

SKIP if outcome_class == "routine". Otherwise:

**NOTE (rb-428 compliance gate)**: If you skip this step, `experience-staleness-check.sh`
will set `force_experience_archival` in working memory. The next iteration's precheck
Phase 0-pre2 (`aspirations-precheck/SKILL.md`) reads that sentinel and BLOCKS goal
selection until you compose the record retroactively. Drift is self-correcting within
one iteration — but composing it now, while context is fresh, avoids the retro-compose
tax.


```
# Bash-enforced write path (rb-428 / guard-365). The wrapper does the
# .md placement, JSONL append, schema validate, dedup, meta refresh,
# and emits stderr WARN on short trace (<200B) or short summary (<20
# chars) as drift signals. LLM residue: the reasoning trace content.

Write a reasoning-trace .md file to any path <trace-path>. Include tool outputs,
decisions, outcome, verification, surprises, and verbatim anchors (exact
technical values — error codes, limits, timeouts, paths+lines+commits,
latencies, API responses).

echo '<stdin-json>' | bash core/scripts/experience-archive-goal.sh \
  --goal {goal.id} --skill-slug {goal.skill_name_slug} \
  --category {goal.category} --summary "<one-line summary>" \
  --trace-file <trace-path>
# <stdin-json> may include any subset of:
#   verbatim_anchors [{key, content}], tree_nodes_related[],
#   retrieval_audit{manifest_present, nodes_count, active_count, skipped_count,
#     utilization_fired, influence}, enabled_by[] (filled by 4.27 — leave []
#     here), reasoning_chain[], hypothesis_id, temporal_credit.
# The wrapper moves <trace-path> to canonical
# agents/<agent>/experience/exp-{goal.id}-{goal.skill_name_slug}.md.

echo '{"experience_refs":["exp-{goal.id}-{goal.skill_name_slug}"]}' | Bash: wm-set.sh active_context.experience_refs
```

## Phase 4.26: Context Utilization Feedback

```
IF outcome_class != "routine":
    helpful_items = items that met structural helpfulness (referenced in
      execution commands/decisions/output OR matched in Phase 4.1)
    Bash: utilization-feedback.sh --goal {goal.id} --helpful "{comma-separated IDs}"
ELSE:
    Bash: utilization-feedback.sh --goal {goal.id} --all-noise
```

BACKSTOP: `utilization-gate.sh` PreToolUse hook auto-applies `--all-unknown`
before `aspirations-state-update` if this phase is skipped. (Pre-2026-05-07
the backstop was `--all-noise`, which silently poisoned times_noise on
unattested-but-relevant nodes — see audit notes in utilization-gate.sh.)
The system NEVER has zero utilization data; phase-4-26-gate still blocks
goal completion when only the backstop ran, forcing the LLM to attest or
pass `--no-retrieval-applicable`.

MR-Search reflection-quality tracking: helpful items with `source_reflection_id`
write positive downstream signal to `meta/reflection-strategy.yaml →
reflection_quality_log`.

## Phase 4.27: Causal Enabler Scan (MR-Search Temporal Credit)

```
IF outcome_class != "routine" AND goal_succeeded:
    FOR EACH active item from retrieval_manifest that met structural helpfulness:
        item_source_goal = item.source_goal or item.source
        IF item_source_goal:
            goals_between = count goals completed between then and now
            Bash: experience-update-field.sh exp-{goal.id}-* enabled_by \
                '<append {experience_id: "exp-{item_source_goal}", relationship: "provided_foundation", temporal_distance: goals_between}>'
```

## Phase 4.28: Skill Co-Invocation Logging

```
invoked_skills = [goal.skill stripped of "/" and params]
Append any auxiliaries invoked during execution (decompose, tree, research-topic, etc.)
IF len(invoked_skills) >= 2:
    Bash: skill-relations.sh co-invoke --goal {goal.id} --skills {comma_separated}
```

## Phase 4.5: Knowledge Reconciliation Check

After executing a goal, check if the knowledge that informed it needs updating.
This closes the loop: knowledge -> action -> knowledge update.

### Cooperative Stop-Check (runs first)

If `/stop` fired during primary execution, commit/push (Phase 4.2) has already
completed — no uncommitted changes at risk. Reconciliation is defer-safe:
stale nodes get caught by the next session's reconciliation pass or by
`/reflect --curate-memory`. Skip this phase so the iteration reaches
Phase 8-stop faster. `iteration-close.sh --phase verify` and `--phase
state-update` still run afterwards in the orchestrator — they are the
mandatory obligations, they are fast, and Phase -1.4 of the next iteration
then invokes graceful-stop for clean finalization.

INVARIANT (do not reorder): this check must stay ABOVE the Reconciliation
block. The whole point is to skip reconciliation under stop. Do NOT also
skip Phase 4.6 — board post is one shell call and is worth keeping.

```
Bash: `session-signal-exists.sh stop-requested`
stop_pending = (exit 0)
IF stop_pending:
    Bash: echo "COOP_STOP: Phase 4.5 reconciliation skipped — /stop pending; verify + state-update still run via iteration-close"
    echo '{"entry_type":"observation","goal_id":"<goal.id>","content":"coop-stop: Phase 4.5 reconciliation skipped; commit/push already done in 4.2; graceful-stop finalizes on next iteration"}' | bash core/scripts/execution-diary.sh append
    PROCEED directly to Phase 4.6 — skip the Reconciliation block below
```

### Reconciliation (runs when stop NOT pending)

```
# Freshness prioritization: check most-retrieved nodes first
# High-retrieval nodes have more impact if they're wrong
Bash: experience-read.sh --most-retrieved 10
high_retrieval_nodes = extract tree_nodes_related from top experiences
Prioritize these nodes in the reconciliation scan below

IF external_changes:  # Set by Phase 4.2 domain steps (concrete detection, not assumed)
    tree_nodes_used = primary_nodes read during intelligent retrieval (from Phase 4)
    IF tree_nodes_used is non-empty:
        For each node_key in tree_nodes_used:
            Read the node's .md file (brief scan, not deep read)
            Ask: "Does this node still accurately reflect reality after what I just changed?"
            IF node is stale or contradicted:
                IF quick fix (< 3 sentences): update node now, set last_update_trigger:
                    {type: "reconciliation", source: goal.id, session: N}
                ELSE: echo '{"node_key": "<node_key>", "reason": "<reason>", "source_goal": "<goal.id>", "priority": "medium", "created": "<today>"}' | Bash: wm-append.sh knowledge_debt

ELIF goal resolved a hypothesis with outcome CORRECTED:
    # Corrections mean our knowledge was WRONG — high-priority reconciliation
    affected_nodes = nodes from retrieval context matching goal.category
    For each affected node:
        IF outcome contradicts node content → reconcile immediately or log HIGH debt
        IF outcome refines understanding → update confidence, add compressed insight
```

### Probe-Outcome Surprise Detection (E7)

Runs IN ADDITION to the two branches above — they cover different signals.
external_changes covers world-changing actions the agent performed. CORRECTED
covers hypothesis outcomes. Probe-outcome surprise covers a third case:
Phase 4's primary action invoked a canonical probe (any script that reads
external system state — `infra-health.sh`, the agent's domain probes like
`efs-ssh.sh`, `operator-api.sh`, `state-replay`, `aws-exec.sh`, etc.) and the
probe output DIVERGES from documented expected values.

Without this check, drift between a script's hardcoded defaults and the
tree's documented values (the canonical drift class — rb-334 / guard-308 /
rb-389) sits in the execution archive only and never reaches the tree. The
next session re-discovers the same drift.

```
1. Detect probe execution: did the result text contain canonical-probe
   output? Look for command output blocks tagged with rc/exit codes,
   structured JSON responses, or output prefixed with a script name from
   `world/scripts/` or `core/scripts/`. Use judgment — this is LLM-driven
   detection, not regex.

   IF no probe was invoked this Phase 4: SKIP this sub-section entirely.

2. For each probe identified:
   a. What was probed? Short description (e.g., "service health endpoint",
      "remote filesystem listing", "API response shape").
   b. Find candidate documenting tree nodes:
        Bash: bash core/scripts/tree-find-node.sh --text "<probe topic>" --top 3
   c. For each candidate node, read briefly (front matter + Verified Values
      section + Key Insights). Extract documented:
        - Expected ports / URLs / endpoints
        - Documented field names / schema shape
        - Expected output format / sentinel values
        - Documented error patterns

3. Compute surprise per probe — pick the highest:
     port/URL/endpoint mismatch  → surprise 8 (HIGH)
     schema/field-shape divergence → surprise 7 (HIGH)
     missing documented field    → surprise 6 (MEDIUM-HIGH)
     silent empty output          → surprise 5 (MEDIUM — per rb-389,
                                    silent failure is non-zero signal but
                                    requires the two-probe rule before
                                    high-conviction conclusion)
     output matches documented   → surprise 0 (skip — no encoding needed)

   IF max surprise < 6: SKIP — drift not significant enough to encode.

4. File a knowledge_debt entry. node_key is the best-matching candidate
   node from step 2c (NOT null — we have a concrete target):

   echo '{
     "node_key": "<best-matching node key>",
     "reason": "probe-outcome-divergence: <probed thing> — <what diverged>",
     "source_goal": "<goal.id>",
     "priority": "HIGH",
     "created": "<today ISO>",
     "surprise_score": <integer 6-8>,
     "probe_script": "<canonical probe name>",
     "divergence_summary": "<one-line: documented X, observed Y>"
   }' | bash core/scripts/wm-append.sh knowledge_debt

5. Also queue an encoding observation in sensory_buffer so Phase 8's
   encoding pipeline picks it up via the high_surprise replay priority.
   Shape matches `core/config/memory-pipeline.yaml` `encoding_observation`
   template. Surprise score normalized to 0-1 (divide step-3 score by 10):

   echo '{
     "source_goal": "<goal.id>",
     "observation": "Probe drift detected: <divergence_summary>",
     "encoding_score": 0.0,
     "scores": {
       "novelty": 0.7,
       "outcome_impact": 0.6,
       "surprise": <surprise_score/10>,
       "goal_relevance": 0.5,
       "repetition_strength": 0.2
     },
     "target_article": "<best-matching node file>",
     "replay_priority": "high_surprise"
   }' | bash core/scripts/wm-append.sh sensory_buffer

6. Diary breadcrumb:
   echo '{"entry_type":"observation","goal_id":"<goal.id>","content":"E7 probe-outcome surprise: <summary>"}' | bash core/scripts/execution-diary.sh append

Fail-open: tree-find-node errors, wm-append errors, or empty candidate
lists → log to execution-diary, do not block downstream phases. The
debt-and-observation queue is a best-effort signal; missing one drift
detection is recoverable, blocking the loop is not.
```

**Why HIGH priority + dual-write to knowledge_debt + sensory_buffer**:
probe drift is the specific failure mode behind multi-hour false-blocker
incidents (rb-334 14-hour stall, rb-389 silent ssh failure, rb-246
synthetic probe). Two surfaces ensure either Phase 8 (sensory_buffer →
encoding gate → tree update via high_surprise priority) OR
consolidation's Step 2.25 (knowledge_debt sweep) catches it; one of the
two will fire on any session-end path.

**Cross-reference**: `core/config/conventions/encoding-triggers.md` E7 row.

### Phase 4.6: Post Findings to Board

After goal execution and knowledge reconciliation, post notable findings:

```
IF goal produced actionable findings OR hypothesis was resolved:
    summary = one-line summary of what was learned or accomplished
    echo "${summary}" | Bash: board-post.sh --channel findings --type finding --tags <goal.category>
```

Skip for routine/maintenance goals that produce no new knowledge.

### Phase 4.7: Full-Suite Test Recommender (g-115-858)

Advisory banner that detects code changes from this goal (Mind framework
under `core/scripts/`, `mind_api/src/`, `.claude/skills/`, `.claude/rules/`,
`core/config/`, AND product workspace under `AGENT_WRITE_PATH`) and
recommends the appropriate full-suite test commands BEFORE the orchestrator
hands control to Phase 5 verify.

Origin: g-115-744 / g-115-746 incident — a deep code closure narrated
"All tests pass" based on the targeted new test only, missing a
`testSymmetry` regression that the full Java suite would have caught.
g-115-858 is the Idea that surfaced the gap; this phase is its Apply.

Posture: ADVISORY, fail-open, always exits 0. The gate's value is the
visible stdout banner; the LLM is expected to act on it (run the
recommended commands) BEFORE narrating "all tests pass" in Phase 5.
Mirrors the pre-apply consult gate (Phase 4, line 274) — visibility
beats hard-block, because (a) suite runs are 30s–5min wall-clock and
should be a deliberate LLM decision, and (b) some deep closures are
documentation-only where the suite adds no signal.

```
# Skip for routine outcomes — rule scope is deep code closures only.
# The script accepts --outcome-class and short-circuits internally on
# routine, but emit the conditional here so the intent is visible in
# pseudocode and the diary breadcrumb is consistent across goals.
IF outcome_class != "routine":
    Bash: bash core/scripts/full-suite-recommender.sh <goal.id> --outcome-class <outcome_class>
    # Banner names recommended invocations per detected file category.
    # The LLM SHOULD run the recommended commands before Phase 5 verify
    # if "all tests pass" is going to appear in the verify narrative.
    # Skip only if the changes are clearly non-behavioral (pure narrative
    # in a SKILL.md, rule wording without companion script wiring, etc).
ELSE:
    # Quiet skip — the script self-skips on routine; no banner emitted.
    Pass
```

Companion rule: `.claude/rules/run-full-suite-after-deep-code.md` defines
what "full-suite" means per code area (Mind: `python -m pytest
core/scripts/tests -q`; product Java: `./gradlew test --no-daemon`;
product Node: `npm test`).

Cross-reference: `world/conventions/post-execution.md` Step 2.b.1 ALSO
mandates the product-repo full-suite as a pre-push build gate — but
that fires AFTER commit, when Phase 5 already claimed "all tests pass."
This Phase-4.7 advisory fires BEFORE Phase 5, closing the window where
false claims would land in the verify narrative.

## Batched Execution

Batched execution (RARE — only when batch_mode is true).
Default is single-goal. Batch only fires for trivially small second goals.
Sequential batch execution (non-delegated path).
When using agent delegation, parallel dispatch is handled in Phase 2.6.
Each batched goal MUST complete full Phase 5-8 before the next starts.

```
IF batch_mode AND more goals in batch:
    Execute next goal in batch (reuse retrieval context, skip Phase 2)
    Classify outcome_class for batched goal (same Phase 4-post rules)
    MANDATORY per-goal phases (in order, gated by outcome_class):
    - Phase 5: Verify completion (always runs)
    - Phase 6: Spark check (SKIP if routine)
    - Phase 7: Aspiration-level check (always runs)
    - Phase 8: State Update Protocol — full steps with immediate tree encoding if deep,
      Steps 1-4 + abbreviated Step 7 if routine
    Complete ALL phases for this goal before starting the next batched goal.
Bash: echo "aspirations-execute phase documented"
```

## Chaining

- **Called by**: `/aspirations` orchestrator (Phase 4, every iteration)
- **Calls**: `aspirations-update-goal.sh --source`, `aspirations-add-goal.sh --source`, `load-conventions.sh`, `load-tree-summary.sh`, `retrieve.sh`, `tree-update.sh`, `guardrail-check.sh`, `infra-health.sh`, `experience-add.sh`, `wm-set.sh`, `wm-read.sh`, `board-post.sh`, `skill-relations.sh`, `build-agent-context.sh`, `curriculum-contract-check.sh`, `pending-agents.sh`

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
Phase 4.6 ends with tool calls (aspirations-update-goal.sh, experience-add.sh). Never
end with a text "Output:" block; the final Bash or Skill invocation must be last.
